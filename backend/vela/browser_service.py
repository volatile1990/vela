"""Private Instagram browser sidecar. The only control listener is a Unix socket."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import stat
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .instagram_auth import InstagramAuthState, MESSAGES
from .source_sessions import SourceSessionError, SourceSessionStore, validate_cookie_text


LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"
VIEWPORT = {"width": 1000, "height": 760}
ALLOWED_KEYS = frozenset({"Enter", "Tab", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape", "Delete", "Home", "End", "Space"})
PASSWORD_SELECTOR = 'input[type="password"],input[name="pass" i],input[name*="password" i],input[name*="code" i],input[autocomplete="current-password"],input[autocomplete="new-password"],input[autocomplete="one-time-code"]'
USERNAME_SELECTOR = 'input[name="username"]:visible,input[name="email"]:visible,input[autocomplete="username"]:visible,input[type="email"]:visible'
LOGIN_PASSWORD_SELECTOR = 'input[name="password"]:visible,input[name="pass"]:visible,input[type="password"]:visible'
SUBMIT_SELECTOR = 'button[type="submit"]:visible,input[type="submit"]:visible'
LOGIN_SUBMIT_LABELS = ("Anmelden","Log in","Log In","Einloggen")


class BrowserControlError(ValueError):
    def __init__(self, message="Die Browseraktion konnte nicht ausgeführt werden.", status_code=409):
        super().__init__(message)
        self.status_code = status_code


class AccountStorageError(ValueError):
    def __init__(self):
        super().__init__("Der geschützte Instagram-Zugang konnte nicht verarbeitet werden.")


def validate_account(username,password):
    if not isinstance(username,str) or not isinstance(password,str) or not username.strip() or len(username)>254 or any(ord(c)<32 for c in username) or not password or len(password)>4096:
        raise BrowserControlError("Bitte einen gültigen Instagram-Zugang eingeben.",422)


async def drain_thread(function,*args,**kwargs):
    """Cancellation must not let a secret write outlive account/profile deletion."""
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    cancelled=False
    while True:
        try:
            result=await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled=True
        except Exception:
            if cancelled:raise asyncio.CancelledError from None
            raise
    if cancelled:raise asyncio.CancelledError
    return result


@dataclass(frozen=True)
class BrowserSettings:
    state_dir: Path = Path("/state")
    credentials_dir: Path = Path("/credentials")
    secrets_dir: Path = Path("/browser-secrets")
    profile_dir: Path = Path("/browser-profile")
    control_socket: Path = Path("/browser-control/control.sock")
    executable: str = "/usr/bin/chromium"
    poll_interval: float = 1.0
    login_timeout: float = 60.0
    challenge_timeout: float = 600.0
    idle_timeout: float = 120.0

    def __post_init__(self):
        paths = [self.state_dir, self.credentials_dir, self.secrets_dir, self.profile_dir, self.control_socket.parent]
        absolute = [path.absolute() for path in paths]
        if len(set(absolute)) != len(absolute) or any(len(path.parts) < 2 for path in absolute):
            raise ValueError("Browser directories must be separate, explicitly named locations")
        if any(a in b.parents or b in a.parents for index,a in enumerate(absolute) for b in absolute[index+1:]):
            raise ValueError("Browser directories must not contain each other")
        if min(self.poll_interval,self.login_timeout,self.challenge_timeout,self.idle_timeout) <= 0:
            raise ValueError("Browser time limits must be positive")

    @classmethod
    def from_env(cls):
        return cls(state_dir=Path(os.environ.get("VELA_STATE_DIR", "/state")),
            credentials_dir=Path(os.environ.get("VELA_CREDENTIALS_DIR", "/credentials")),
            secrets_dir=Path(os.environ.get("VELA_BROWSER_SECRETS_DIR", "/browser-secrets")),
            profile_dir=Path(os.environ.get("VELA_BROWSER_PROFILE_DIR", "/browser-profile")),
            control_socket=Path(os.environ.get("VELA_BROWSER_CONTROL_SOCKET", "/browser-control/control.sock")),
            executable=os.environ.get("VELA_CHROMIUM_EXECUTABLE", "/usr/bin/chromium"))


@contextmanager
def private_directory(path: Path):
    descriptor = None
    try:
        if path.is_symlink():
            raise AccountStorageError()
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.geteuid())):
            raise AccountStorageError()
        if os.name == "posix":
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        yield descriptor
    except FileNotFoundError:
        raise
    except OSError:
        raise AccountStorageError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class AccountVault:
    """Encrypted account file and independent random key, both private to this sidecar."""
    _KEY = "instagram.key"
    _ACCOUNT = "instagram.account.enc"
    _AAD = b"vela.instagram.account.v1"

    def __init__(self, directory: Path):
        self.directory = directory

    def _read(self, name: str, descriptor) -> bytes:
        path = name if descriptor is not None else self.directory / name
        info = os.stat(path, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32768 or (os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.geteuid())):
            raise AccountStorageError()
        fd = os.open(path, os.O_RDONLY | getattr(os,"O_NOFOLLOW",0) | getattr(os,"O_NONBLOCK",0), dir_fd=descriptor)
        with os.fdopen(fd,"rb") as stream:
            actual = os.fstat(stream.fileno())
            if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1 or (actual.st_dev,actual.st_ino) != (info.st_dev,info.st_ino):
                raise AccountStorageError()
            data = stream.read(32769)
        if len(data)>32768:
            raise AccountStorageError()
        return data

    def _write(self, name: str, data: bytes, descriptor) -> None:
        path = name if descriptor is not None else self.directory / name
        try:
            info = os.stat(path, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AccountStorageError()
        except FileNotFoundError:
            pass
        temporary = ".account-"+secrets.token_hex(16)+".tmp"
        temp_path = temporary if descriptor is not None else self.directory / temporary
        created = False
        try:
            fd=os.open(temp_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=descriptor)
            created=True
            with os.fdopen(fd,"wb") as stream:
                if os.name=="posix": os.fchmod(stream.fileno(),0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if descriptor is not None:
                os.replace(temporary,name,src_dir_fd=descriptor,dst_dir_fd=descriptor)
                os.fsync(descriptor)
            else:
                os.replace(self.directory/temporary,self.directory/name)
        finally:
            if created:
                with suppress(FileNotFoundError): os.unlink(temp_path,dir_fd=descriptor)

    def configured(self) -> bool:
        try:
            with private_directory(self.directory) as descriptor:
                self._read(self._ACCOUNT,descriptor)
            return True
        except FileNotFoundError:
            return False

    def save(self, username: str, password: str) -> None:
        validate_account(username,password)
        with private_directory(self.directory) as descriptor:
            try: key=self._read(self._KEY,descriptor)
            except FileNotFoundError:
                key=AESGCM.generate_key(bit_length=256)
                self._write(self._KEY,key,descriptor)
            if len(key)!=32: raise AccountStorageError()
            nonce=secrets.token_bytes(12)
            data=json.dumps({"username":username.strip(),"password":password},ensure_ascii=False).encode("utf-8")
            self._write(self._ACCOUNT,b"VELAIG1"+nonce+AESGCM(key).encrypt(nonce,data,self._AAD),descriptor)

    def load(self) -> dict:
        try:
            with private_directory(self.directory) as descriptor:
                key=self._read(self._KEY,descriptor)
                data=self._read(self._ACCOUNT,descriptor)
            if len(key)!=32 or not data.startswith(b"VELAIG1") or len(data)<36: raise AccountStorageError()
            result=json.loads(AESGCM(key).decrypt(data[7:19],data[19:],self._AAD))
            if set(result)!={"username","password"} or not all(isinstance(value,str) and value for value in result.values()): raise AccountStorageError()
            return result
        except Exception:
            raise AccountStorageError() from None

    def delete(self) -> None:
        with private_directory(self.directory) as descriptor:
            for name in (self._ACCOUNT,self._KEY):
                try:
                    self._read(name,descriptor)
                    os.unlink(name if descriptor is not None else self.directory/name,dir_fd=descriptor)
                except FileNotFoundError: pass
            if descriptor is not None: os.fsync(descriptor)


class ProfileOwnerLock:
    def __init__(self,path:Path): self.path,self.stream=path,None
    def acquire(self):
        if self.stream is not None:return
        self.path.parent.mkdir(mode=0o700,exist_ok=True)
        if self.path.is_symlink(): raise AccountStorageError()
        fd=os.open(self.path,os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0),0o600)
        self.stream=os.fdopen(fd,"a+b")
        try:
            if os.name=="posix":
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1): self.stream.write(b"0"); self.stream.flush()
                self.stream.seek(0); msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        except OSError:
            self.release()
            raise BrowserControlError("Ein anderer Browserdienst verwendet dieses Profil.",503) from None
    def release(self):
        if self.stream: self.stream.close(); self.stream=None


def allowed_browser_request(url: str, navigation: bool) -> bool:
    try:
        parsed=urlsplit(url)
        if parsed.scheme!="https" or parsed.username or parsed.password or parsed.port not in {None,443}: return False
        host=(parsed.hostname or "").lower().rstrip(".")
        domains=("instagram.com",) if navigation else ("instagram.com","cdninstagram.com","fbcdn.net","fbsbx.com")
        return any(host==domain or host.endswith("."+domain) for domain in domains)
    except ValueError: return False


def cookie_text_from_browser(cookies: list[dict]) -> str:
    lines=["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain=cookie.get("domain","")
        if not isinstance(domain,str) or not (domain.lstrip(".")=="instagram.com" or domain.lstrip(".").endswith(".instagram.com")): continue
        expiration=cookie.get("expires",-1)
        expiry=str(int(expiration)) if isinstance(expiration,(int,float)) and expiration>0 else ""
        fields=[domain,"TRUE" if domain.startswith(".") else "FALSE",cookie.get("path","/"),"TRUE" if cookie.get("secure") else "FALSE",expiry,cookie.get("name",""),cookie.get("value","")]
        if not all(isinstance(field,str) for field in fields): raise AccountStorageError()
        lines.append(("#HttpOnly_" if cookie.get("httpOnly") else "")+"\t".join(fields))
    return validate_cookie_text("instagram","\n".join(lines)+"\n").text


class BrowserController:
    def __init__(self,settings:BrowserSettings,*,browser_factory=None):
        self.settings=settings
        self.state=InstagramAuthState(settings.state_dir)
        self.vault=AccountVault(settings.secrets_dir)
        self.sessions=SourceSessionStore(settings.credentials_dir)
        self.owner=ProfileOwnerLock(settings.secrets_dir/"browser.lock")
        self.control_lock=asyncio.Lock()
        self.operation_lock=asyncio.Lock()
        self.browser_factory=browser_factory
        self.context=self.page=self.playwright=None
        self.interaction_id=None
        self.last_activity=0.0
        self.last_account_write=0.0
        self.login_task=self.poll_task=None
        self.stopping=False

    async def start(self):
        self.state.initialize()
        with private_directory(self.settings.secrets_dir): pass
        with private_directory(self.settings.profile_dir): pass
        self.owner.acquire()
        try:
            configured=await drain_thread(self.vault.configured)
            previous=self.state.status()
            if configured!=previous["configured"]: self.state.configure(configured)
            self.state.publish(browser_available=False,interaction_id=None)
            self.poll_task=asyncio.create_task(self._poll())
        except Exception:
            self.owner.release()
            raise

    async def close(self):
        self.stopping=True
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task,return_exceptions=True)
        await self._cancel_login()
        async with self.control_lock: await self._close_browser()
        self.state.publish(browser_available=False,interaction_id=None)
        self.owner.release()

    async def _cancel_login(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            await asyncio.gather(self.login_task,return_exceptions=True)
        self.login_task=None

    async def _poll(self):
        while not self.stopping:
            try:
                async with self.operation_lock:
                    if self.login_task and self.login_task.done():
                        await asyncio.gather(self.login_task,return_exceptions=True)
                        self.login_task=None
                    if not self.login_task:
                        request_id=await drain_thread(self.state.claim_refresh)
                        if request_id: self.login_task=asyncio.create_task(self._run_login(request_id))
                        elif self.context and time.monotonic()-self.last_activity>self.settings.idle_timeout:
                            async with self.control_lock:
                                # Launch, screen or an action may have refreshed activity
                                # while the poller was waiting for the browser lock.
                                if self.context and time.monotonic()-self.last_activity>self.settings.idle_timeout:
                                    await self._close_browser()
                    await drain_thread(self.state.publish,browser_available=self.page is not None,interaction_id=self.interaction_id)
            except asyncio.CancelledError: raise
            except Exception:
                with suppress(Exception): self.state.publish(state="error",browser_available=False,interaction_id=None)
            await asyncio.sleep(self.settings.poll_interval)

    async def _route(self,route,request):
        if request.resource_type=="media" or not allowed_browser_request(request.url,request.is_navigation_request()):
            await route.abort()
        else: await route.continue_()

    async def _open_browser(self):
        if self.page is not None: return
        if self.context is not None: await self._close_browser()
        self.last_activity=time.monotonic()
        # Direct operator/fake entry points obey the same single-owner rule as start().
        with private_directory(self.settings.secrets_dir):pass
        self.owner.acquire()
        with private_directory(self.settings.profile_dir): pass
        self._clear_stale_singletons()
        # Disable password saving in this dedicated profile; the account is kept in AES-GCM only.
        default=self.settings.profile_dir/"Default"
        if default.is_symlink(): raise AccountStorageError()
        default.mkdir(mode=0o700,exist_ok=True)
        preferences=default/"Preferences"
        if preferences.is_symlink(): raise AccountStorageError()
        try: content=json.loads(preferences.read_text()) if preferences.exists() else {}
        except (ValueError,OSError): content={}
        content["credentials_enable_service"]=False
        content.setdefault("profile",{})["password_manager_enabled"]=False
        preferences.write_text(json.dumps(content))
        preferences.chmod(0o600)
        if self.browser_factory:
            self.context=await self.browser_factory(self.settings)
        else:
            from playwright.async_api import async_playwright
            self.playwright=await async_playwright().start()
            self.context=await self.playwright.chromium.launch_persistent_context(
                str(self.settings.profile_dir),executable_path=self.settings.executable,headless=True,
                chromium_sandbox=False,viewport=VIEWPORT,accept_downloads=False,service_workers="block",
                permissions=[],locale="de-DE",args=["--disable-background-networking","--disable-component-update","--disable-sync","--no-first-run","--disable-save-password-bubble","--disk-cache-size=67108864","--media-cache-size=1048576"])
        await self.context.route("**/*",self._route)
        if hasattr(self.context,"route_web_socket"):
            await self.context.route_web_socket("**/*",lambda route:route.close())
        for old_page in list(self.context.pages): await old_page.close()
        self.page=await self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.set_default_navigation_timeout(30000)
        self.context.on("page",lambda page:asyncio.create_task(page.close()) if page is not self.page else None)
        self.page.on("download",lambda download:asyncio.create_task(download.cancel()))
        self.page.on("dialog",lambda dialog:asyncio.create_task(dialog.dismiss()))
        current_page=self.page
        self.page.on("close",lambda:self._page_closed(current_page))
        self.interaction_id=secrets.token_hex(24)
        self.last_activity=time.monotonic()
        self.state.publish(browser_available=True,interaction_id=self.interaction_id)

    def _profile_in_use(self,proc_dir:Path=Path("/proc")):
        """Inspect the current PID namespace without signalling any process."""
        if os.name!="posix":return False
        if not proc_dir.is_dir():raise BrowserControlError("Der Browserprofilzustand konnte nicht sicher geprüft werden.",503)
        wanted=str(self.settings.profile_dir.absolute()).encode()
        for index,entry in enumerate(proc_dir.iterdir()):
            if index>4096:raise BrowserControlError("Der Browserprofilzustand konnte nicht sicher geprüft werden.",503)
            if not entry.name.isdigit():continue
            try:arguments=(entry/"cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:continue  # A process may exit during inspection.
            except PermissionError:raise BrowserControlError("Der Browserprofilzustand konnte nicht sicher geprüft werden.",503) from None
            except OSError:continue
            for position,argument in enumerate(arguments):
                if argument==b"--user-data-dir="+wanted or (argument==b"--user-data-dir" and position+1<len(arguments) and arguments[position+1]==wanted):
                    return True
        return False

    def _clear_stale_singletons(self):
        """Remove only Chromium's fixed link names after exclusive ownership.

        A recreated container cannot see its predecessor's PIDs. Our shared flock
        serializes Vela owners; local processes using this profile are checked
        separately. Link targets are never followed, removed, or used to kill a PID.
        """
        if self.owner.stream is None:raise BrowserControlError("Das Browserprofil ist nicht exklusiv gesperrt.",503)
        with private_directory(self.settings.profile_dir) as descriptor:
            links=[]
            lock_target=None
            for name in ("SingletonLock","SingletonCookie","SingletonSocket"):
                try:info=os.stat(name if descriptor is not None else self.settings.profile_dir/name,dir_fd=descriptor,follow_symlinks=False)
                except FileNotFoundError:continue
                if not stat.S_ISLNK(info.st_mode):raise BrowserControlError("Das Browserprofil enthält unerwartete Sperrdateien.",503)
                links.append(name)
                if name=="SingletonLock":lock_target=os.readlink(name if descriptor is not None else self.settings.profile_dir/name,dir_fd=descriptor)
            if not links:return
            if self._profile_in_use():raise BrowserControlError("Das Browserprofil wird noch von einem laufenden Browser verwendet.",503)
            if lock_target is not None:
                try:
                    hostname,pid_text=lock_target.rsplit("-",1)
                    if not hostname or len(hostname)>255 or not pid_text.isdecimal() or not 1<int(pid_text)<2**31:raise ValueError
                except ValueError:raise BrowserControlError("Die Browserprofilsperre konnte nicht sicher geprüft werden.",503) from None
                if hostname==socket.gethostname():
                    try:os.kill(int(pid_text),0)
                    except ProcessLookupError:pass
                    except (PermissionError,OSError):raise BrowserControlError("Das Browserprofil könnte noch verwendet werden.",503) from None
                    else:raise BrowserControlError("Das Browserprofil wird noch von einem laufenden Prozess verwendet.",503)
            for name in links:
                try:os.unlink(name if descriptor is not None else self.settings.profile_dir/name,dir_fd=descriptor)
                except FileNotFoundError:pass
            if descriptor is not None:os.fsync(descriptor)

    def _page_closed(self,page):
        if page is self.page:
            self.page=None
            self.interaction_id=None
            self.state.publish(browser_available=False,interaction_id=None)

    async def _close_browser(self):
        context,self.context=self.context,None
        self.page=None
        self.interaction_id=None
        if context:
            with suppress(Exception): await asyncio.wait_for(context.close(),15)
        if self.playwright:
            with suppress(Exception): await self.playwright.stop()
            self.playwright=None
        self.state.publish(browser_available=False,interaction_id=None)

    def _clear_profile(self):
        root=self.settings.profile_dir.absolute()
        with private_directory(root) as descriptor:
            for child in root.iterdir():
                if child.parent!=root: raise AccountStorageError()
                if descriptor is not None:
                    info=os.stat(child.name,dir_fd=descriptor,follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):shutil.rmtree(child.name,dir_fd=descriptor)
                    else:os.unlink(child.name,dir_fd=descriptor)
                elif child.is_symlink() or not child.is_dir(): child.unlink()
                else: shutil.rmtree(child)

    async def save_account(self,username,password):
        async with self.operation_lock:
            validate_account(username,password)
            if self.last_account_write and time.monotonic()-self.last_account_write<10:
                raise BrowserControlError("Bitte kurz warten, bevor der Zugang erneut gespeichert wird.",429)
            await self._cancel_login()
            async with self.control_lock:
                await self._close_browser()
                await drain_thread(self.sessions.delete,"instagram")
                await drain_thread(self._clear_profile)
                await drain_thread(self.vault.save,username,password)
                self.state.configure(True)
                self.state.request_refresh(force=True)
                self.last_account_write=time.monotonic()
        return self.state.status()

    async def delete_account(self):
        async with self.operation_lock:
            await self._cancel_login()
            async with self.control_lock:
                await self._close_browser()
                await drain_thread(self.sessions.delete,"instagram")
                await drain_thread(self._clear_profile)
                await drain_thread(self.vault.delete)
                self.state.configure(False)
        return self.state.status()

    async def login(self):
        async with self.operation_lock:
            if not self.state.status()["configured"]:
                raise BrowserControlError("Bitte zuerst den Instagram-Zugang speichern.",409)
            if not self.state.request_refresh(force=True):
                raise BrowserControlError("Bitte kurz warten, bevor die Anmeldung erneut gestartet wird.",429)
        return self.state.status()

    async def _authenticated(self):
        if self.page is None or not allowed_browser_request(self.page.url,True): return False
        path=urlsplit(self.page.url).path.lower()
        if any(part in path for part in ("/accounts/login","/challenge","/checkpoint","/two_factor")): return False
        cookies=await self.context.cookies("https://www.instagram.com/")
        valid=any(cookie.get("name")=="sessionid" and cookie.get("value") and (cookie.get("expires",-1)<=0 or cookie["expires"]>time.time()) for cookie in cookies)
        if not valid:return False
        marker=self.page.locator('a[href^="/direct/inbox"],a[href="/accounts/edit/"],svg[aria-label="Home"],svg[aria-label="Startseite"]')
        return bool(await marker.count())

    async def _export_ready(self,request_id):
        if not await self._authenticated(): return False
        text=cookie_text_from_browser(await self.context.cookies())
        await drain_thread(self.sessions.import_text,"instagram",text)
        self.state.finish(request_id,"ready",browser_available=True,interaction_id=self.interaction_id)
        self.last_activity=time.monotonic()
        return True

    async def _try_submit_once(self,request_id):
        await self._decline_optional_cookies()
        username=self.page.locator(USERNAME_SELECTOR).first
        password=self.page.locator(LOGIN_PASSWORD_SELECTOR).first
        if not await username.is_visible() or not await password.is_visible():
            # A persisted profile can show an account chooser instead of input
            # fields. Open the explicit alternate-account form; never continue
            # a remembered identity or remove browser profiles implicitly.
            for label in ("Anderes Profil verwenden","Anderes Konto verwenden","Mit einem anderen Konto anmelden",
                          "Use another profile","Use another account","Log into another account"):
                button=self.page.get_by_role("button",name=label,exact=True)
                if await button.is_visible():
                    await button.click(timeout=3000)
                    break
            return False  # Recheck the rendered form in the bounded outer loop.
        submit=self.page.locator(SUBMIT_SELECTOR).first
        if not await submit.is_visible():
            submit=None
            for label in LOGIN_SUBMIT_LABELS:
                button=self.page.get_by_role("button",name=label,exact=True)
                if await button.is_visible():
                    submit=button
                    break
            if submit is None:return False
        # The real login action may be an aria-disabled DIV until both fields
        # are filled. Verify presence now; click waits for enabled state later.
        if not self.state.consume_login_attempt(request_id): return False
        account=await drain_thread(self.vault.load)
        await username.fill(account["username"])
        await password.fill(account["password"])
        del account
        await submit.click(timeout=5000)
        return True

    async def _decline_optional_cookies(self):
        for label in ("Decline optional cookies","Optionale Cookies ablehnen","Nur erforderliche Cookies zulassen"):
            button=self.page.get_by_role("button",name=label,exact=True)
            if await button.is_visible():
                await button.click(timeout=2000)
                return

    async def _run_login(self,request_id):
        try:
            async with self.control_lock:
                current=self.state.status()
                if not current["configured"] or current["request_id"]!=request_id or current["completed_request_id"]>=request_id:return
                await self._open_browser()
                await self.page.goto(HOME_URL,wait_until="domcontentloaded",timeout=30000)
                if await self._export_ready(request_id): return
                await self.page.goto(LOGIN_URL,wait_until="domcontentloaded",timeout=30000)
            automatic_deadline=time.monotonic()+self.settings.login_timeout
            attempted=False
            while time.monotonic()<automatic_deadline:
                async with self.control_lock:
                    if await self._export_ready(request_id):return
                    if not attempted:
                        with suppress(Exception): attempted=await self._try_submit_once(request_id)
                await asyncio.sleep(min(1,self.settings.poll_interval))
            self.state.finish(request_id,"needs_attention",browser_available=True,interaction_id=self.interaction_id)
            deadline=time.monotonic()+self.settings.challenge_timeout
            while time.monotonic()<deadline:
                async with self.control_lock:
                    if await self._export_ready(request_id):return
                await asyncio.sleep(min(1,self.settings.poll_interval))
            async with self.control_lock: await self._close_browser()
            self.state.finish(request_id,"error",message="Die Bestätigungszeit ist abgelaufen. Bitte die Anmeldung erneut öffnen.")
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self.control_lock: await self._close_browser()
            self.state.finish(request_id,"error")

    async def open_login_preview(self):
        """Operator smoke test: open the login page without reading or submitting an account."""
        async with self.control_lock:
            await self._open_browser()
            await self.page.goto(LOGIN_URL,wait_until="domcontentloaded",timeout=30000)
            with suppress(Exception):await self._decline_optional_cookies()
        return self.state.status()

    async def screen(self):
        async with self.control_lock:
            if self.page is None:raise BrowserControlError("Es ist kein Instagram-Browserfenster geöffnet.",409)
            try:
                masks=[frame.locator(PASSWORD_SELECTOR) for frame in self.page.frames]
                data=await self.page.screenshot(type="png",mask=masks,mask_color="#111827",animations="disabled",caret="hide",timeout=5000)
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data)>8*1024**2:raise ValueError
                self.last_activity=time.monotonic()
                return data
            except Exception:raise BrowserControlError("Das Browserbild konnte nicht geladen werden.",503) from None

    async def action(self,action):
        async with self.control_lock:
            if self.page is None or not self.interaction_id or not secrets.compare_digest(action.interaction_id,self.interaction_id):
                raise BrowserControlError("Dieses Browserfenster ist nicht mehr aktuell. Bitte erneut öffnen.",409)
            try:
                if action.kind=="click":await self.page.mouse.click(action.x*VIEWPORT["width"],action.y*VIEWPORT["height"])
                elif action.kind=="text":await self.page.keyboard.insert_text(action.text)
                elif action.kind=="key":await self.page.keyboard.press(action.key)
                elif action.kind=="scroll":await self.page.mouse.wheel(0,action.delta)
                self.last_activity=time.monotonic()
                pending=self.state.pending_request()
                if pending:await self._export_ready(pending)
                return self.state.status()
            except Exception:raise BrowserControlError() from None


class AccountInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    username:str=Field(min_length=1,max_length=254,repr=False)
    password:str=Field(min_length=1,max_length=4096,repr=False)


class ActionInput(BaseModel):
    model_config=ConfigDict(extra="forbid",allow_inf_nan=False)
    interaction_id:str=Field(min_length=48,max_length=48,pattern=r"^[a-f0-9]{48}$")
    kind:str
    x:float|None=Field(default=None,ge=0,le=1)
    y:float|None=Field(default=None,ge=0,le=1)
    text:str|None=Field(default=None,max_length=4096,repr=False)
    key:str|None=None
    delta:int|None=Field(default=None,ge=-1000,le=1000)

    @model_validator(mode="after")
    def check_action(self):
        if self.kind=="click" and self.x is not None and self.y is not None:return self
        if self.kind=="text" and self.text is not None and "\0" not in self.text:return self
        if self.kind=="key" and self.key in ALLOWED_KEYS:return self
        if self.kind=="scroll" and self.delta is not None:return self
        raise ValueError("Unsupported browser action")


def create_app(settings:BrowserSettings|None=None,controller:BrowserController|None=None):
    settings=settings or BrowserSettings.from_env()
    controller=controller or BrowserController(settings)

    @asynccontextmanager
    async def lifespan(app):
        await controller.start()
        try:yield
        finally:await controller.close()

    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.controller=controller

    @app.middleware("http")
    async def limits(request,call_next):
        if request.method in {"PUT","POST"}:
            try:
                if int(request.headers.get("content-length","0"))>16384:return JSONResponse({"detail":"Die Anfrage ist zu groß."},413)
            except ValueError:return JSONResponse({"detail":"Die Anfrage ist ungültig."},400)
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>16384:return JSONResponse({"detail":"Die Anfrage ist zu groß."},413)
            request._body=bytes(body)
        response=await call_next(request)
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Content-Type-Options"]="nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation(request,error):return JSONResponse({"detail":"Die Browseranfrage ist ungültig."},422)
    @app.exception_handler(BrowserControlError)
    async def control_error(request,error):return JSONResponse({"detail":str(error)},error.status_code)
    @app.exception_handler(AccountStorageError)
    async def storage_error(request,error):return JSONResponse({"detail":"Der geschützte Zugangsspeicher ist nicht verfügbar."},503)

    @app.get("/status")
    async def status_endpoint():return controller.state.status()
    @app.put("/account")
    async def account_endpoint(account:AccountInput):return await controller.save_account(account.username,account.password)
    @app.delete("/account")
    async def delete_endpoint():return await controller.delete_account()
    @app.post("/login")
    async def login_endpoint():return await controller.login()
    @app.post("/action")
    async def action_endpoint(action:ActionInput):return await controller.action(action)
    @app.get("/screen")
    async def screen_endpoint():return Response(await controller.screen(),media_type="image/png",headers={"Cache-Control":"no-store"})
    return app


def main():
    import uvicorn
    settings=BrowserSettings.from_env()
    socket=settings.control_socket
    with private_directory(socket.parent):pass
    if socket.is_symlink() or (socket.exists() and not stat.S_ISSOCK(socket.stat().st_mode)):
        raise SystemExit("Browser control socket is not safely available")
    previous=os.umask(0o077)
    try:uvicorn.run(create_app(settings),uds=str(socket),access_log=False,log_level="warning",proxy_headers=False)
    finally:os.umask(previous)


if __name__=="__main__":main()
