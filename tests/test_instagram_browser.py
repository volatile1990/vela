"""Synthetic credentials and fake browser only; no external login is performed."""
import asyncio
import json
import os
import stat
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vela.browser_service import (AccountStorageError,AccountVault,ActionInput,BrowserController,
    BrowserControlError,BrowserSettings,HOME_URL,LOGIN_URL,LOGIN_PASSWORD_SELECTOR,PASSWORD_SELECTOR,
    SUBMIT_SELECTOR,USERNAME_SELECTOR,OTP_INPUT_SELECTOR,AUTHENTICATOR_CHOICE_SELECTOR,allowed_browser_request,cookie_text_from_browser,create_app)
from vela.instagram_auth import InstagramAuthState
from vela.instagram_api import InstagramApiClient


@pytest.fixture(autouse=True)
def sqlite_for_tests(monkeypatch):monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE","1")


def options(tmp_path,**overrides):
    return BrowserSettings(state_dir=tmp_path/"state",credentials_dir=tmp_path/"credentials",
        secrets_dir=tmp_path/"secrets",profile_dir=tmp_path/"profile",control_socket=tmp_path/"control"/"control.sock",
        poll_interval=.01,login_timeout=.04,challenge_timeout=.3,idle_timeout=.1,**overrides)


class FakeLocator:
    def __init__(self,page,selector):self.page,self.selector=page,selector
    @property
    def first(self):return self
    async def is_visible(self):
        if self.selector=="Decline optional cookies":return self.page.context.overlay
        if self.selector in (USERNAME_SELECTOR,LOGIN_PASSWORD_SELECTOR):return self.page.url==LOGIN_URL and (not self.page.context.chooser or self.page.context.form_also_visible)
        if self.selector==SUBMIT_SELECTOR:return self.page.url==LOGIN_URL and self.page.context.native_submit
        if self.selector==self.page.context.role_submit_label:return self.page.url==LOGIN_URL
        if self.selector==self.page.context.chooser_label:return self.page.context.chooser
        return False
    async def is_enabled(self):return not self.page.context.submit_disabled_until_fill or len(self.page.fills)>=2
    async def fill(self,value):self.page.fills.append((self.selector,value))
    async def click(self,**kwargs):
        self.page.clicked_controls.append(self.selector)
        if self.selector=="Decline optional cookies":self.page.context.overlay=False;return
        if self.selector==self.page.context.chooser_label:
            self.page.context.chooser=False;self.page.context.chooser_clicks+=1;return
        if not await self.is_enabled():raise TimeoutError("Synthetic login control still disabled")
        self.page.submits+=1
        if self.page.context.result=="ready":self.page.context.authenticated=True;self.page.url=HOME_URL
        else:self.page.url="https://www.instagram.com/challenge/synthetic/"
    async def count(self):return int(self.page.context.authenticated)


class FakeKeyboard:
    def __init__(self,page):self.page=page;self.texts=[];self.keys=[]
    async def insert_text(self,text):self.texts.append(text)
    async def press(self,key):self.keys.append(key)


class FakeMouse:
    def __init__(self,page):self.page=page;self.clicks=[];self.scrolls=[]
    async def click(self,x,y):self.clicks.append((x,y))
    async def wheel(self,x,y):self.scrolls.append((x,y))


class FakePage:
    def __init__(self,context):
        self.context=context;self.url="about:blank";self.fills=[];self.submits=0;self.events={};self.frames=[self]
        self.clicked_controls=[]
        self.keyboard=FakeKeyboard(self);self.mouse=FakeMouse(self);self.screenshot_options=None;self.closed=False
    def set_default_timeout(self,value):pass
    def set_default_navigation_timeout(self,value):pass
    def on(self,name,handler):self.events[name]=handler
    async def goto(self,url,**kwargs):self.url=url
    def locator(self,selector):return FakeLocator(self,selector)
    def get_by_role(self,role,*,name,exact):return FakeLocator(self,name)
    async def screenshot(self,**kwargs):self.screenshot_options=kwargs;return b"\x89PNG\r\n\x1a\nsynthetic-png"
    async def close(self):
        self.closed=True
        if "close" in self.events:self.events["close"]()


class FakeContext:
    def __init__(self,result="ready",authenticated=False):
        self.result=result;self.authenticated=authenticated;self.overlay=True;self.pages=[];self.events={};self.closed=False
        self.chooser=False;self.chooser_label="Anderes Profil verwenden";self.chooser_clicks=0;self.form_also_visible=False
        self.native_submit=True;self.role_submit_label=None;self.submit_disabled_until_fill=False
    async def route(self,pattern,handler):self.route_handler=handler
    async def route_web_socket(self,pattern,handler):self.ws_handler=handler
    def on(self,name,handler):self.events[name]=handler
    async def new_page(self):self.page=FakePage(self);self.pages.append(self.page);return self.page
    async def close(self):
        self.closed=True
        for page in self.pages:await page.close()
    async def add_cookies(self,cookies):self.added_cookies=cookies
    async def cookies(self,*args):
        return [{"domain":".instagram.com","path":"/","secure":True,"httpOnly":True,"expires":time.time()+3600,
            "name":"sessionid" if self.authenticated else "csrftoken","value":"synthetic-session-value"}]


def api_result(state="ready",url=None):
    return SimpleNamespace(state=state,reason="synthetic_static",checkpoint_url=url,user_agent="synthetic-api-agent",
        cookies=[{"domain":".instagram.com","path":"/","secure":True,"httpOnly":True,"expires":-1,
            "name":"sessionid","value":"synthetic-api-session"}])


class FakeApiClient:
    def __init__(self,result="ready",reused=False):self.result=result;self.reused=reused;self.calls=[];self.adopted=[]
    def refresh(self,account,request_id,state,*,stop_event=None):
        self.calls.append(request_id)
        consumed=self.reused or state.consume_login_attempt(request_id)
        if self.result=="ready" and consumed:return api_result()
        return api_result("needs_attention","https://www.instagram.com/challenge/synthetic/")
    def accept_browser_session(self,account,cookies,*,stop_event=None):
        self.adopted.append(cookies)
        return api_result()


def controller(tmp_path,result="ready",authenticated=False,api_client=None):
    contexts=[]
    async def factory(settings):
        context=FakeContext(result,authenticated);contexts.append(context);return context
    return BrowserController(options(tmp_path),browser_factory=factory,api_client=api_client or FakeApiClient(result,authenticated)),contexts


async def wait_state(control,state):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if control.state.status()["state"]==state:return control.state.status()
        await asyncio.sleep(.01)
    raise AssertionError("Synthetic browser did not reach expected state")


def test_state_defaults_dedup_cooldown_and_challenge_completion(tmp_path):
    state=InstagramAuthState(tmp_path/"state")
    assert state.status()["state"]=="unconfigured"
    assert state.request_refresh()==0
    state.configure(True)
    with ThreadPoolExecutor(8) as executor:ids=list(executor.map(lambda _:state.request_refresh(),range(16)))
    assert set(ids)=={1}
    assert state.consume_login_attempt(1)
    assert not InstagramAuthState(tmp_path/"state").consume_login_attempt(1)
    state.finish(1,"needs_attention",browser_available=True,interaction_id="a"*48)
    assert state.status()["completed_request_id"]==0
    assert state.request_refresh(force=True)==1
    done=state.finish(1,"ready")
    assert done["completed_request_id"]==1 and done["session_generation"]==1
    assert state.finish(1,"ready")["session_generation"]==1
    assert state.request_refresh()==0
    state.configure(False);state.configure(True)
    assert state.request_refresh()==2


def test_state_rejects_dynamic_messages_and_stale_browser(tmp_path):
    state=InstagramAuthState(tmp_path/"state")
    with pytest.raises(ValueError):state.publish(message="synthetic-password")
    state.publish(browser_available=True,interaction_id="b"*48)
    with state._database.transaction() as connection:connection.execute("UPDATE instagram_auth SET browser_seen=0")
    assert not state.status()["browser_available"] and state.status()["interaction_id"] is None
    assert b"synthetic-password" not in state.path.read_bytes()


def test_vault_encrypts_roundtrips_deletes_and_rejects_tampering(tmp_path):
    vault=AccountVault(tmp_path/"secrets")
    assert not vault.configured()
    vault.save("synthetic-user","synthetic-password")
    assert vault.load()=={"username":"synthetic-user","password":"synthetic-password"}
    for path in vault.directory.iterdir():
        assert b"synthetic-password" not in path.read_bytes() and b"synthetic-user" not in path.read_bytes()
        if os.name=="posix":assert stat.S_IMODE(path.stat().st_mode)==0o600
    account=vault.directory/"instagram.account.enc"
    value=bytearray(account.read_bytes());value[-1]^=1;account.write_bytes(value)
    with pytest.raises(AccountStorageError) as error:vault.load()
    assert "synthetic-password" not in str(error.value)
    vault.delete();assert not vault.configured()


@pytest.mark.skipif(os.name!="posix",reason="Production Linux file guards")
def test_vault_rejects_symlinks_hardlinks_and_nonprivate_modes(tmp_path):
    outside=tmp_path/"outside";outside.mkdir(mode=0o700)
    linked=tmp_path/"linked";linked.symlink_to(outside,target_is_directory=True)
    with pytest.raises(AccountStorageError):AccountVault(linked).save("synthetic-user","synthetic-password")
    vault=AccountVault(tmp_path/"vault");vault.save("synthetic-user","synthetic-password")
    account=vault.directory/"instagram.account.enc";account.chmod(0o644)
    with pytest.raises(AccountStorageError):vault.load()
    account.chmod(0o600);os.link(account,tmp_path/"hardlink")
    with pytest.raises(AccountStorageError):vault.load()


@pytest.mark.parametrize("url,navigation,allowed",[
    ("https://www.instagram.com/accounts/login/",True,True),
    ("https://i.instagram.com/api/",False,True),
    ("https://static.cdninstagram.com/file.js",False,True),
    ("https://static.xx.fbcdn.net/file.js",False,True),
    ("https://static.xx.fbcdn.net/file.js",True,False),
    ("https://instagram.com.unrelated.invalid/",True,False),
    ("https://facebook.com/",True,False),
    ("http://www.instagram.com/",True,False),
    ("https://user@instagram.com/",True,False),
    ("https://www.instagram.com:8443/",True,False),
    ("file:///private",False,False),
])
def test_browser_network_boundary(url,navigation,allowed):assert allowed_browser_request(url,navigation) is allowed


def test_cookie_export_filters_and_keeps_session_and_httponly():
    text=cookie_text_from_browser([{"domain":".instagram.com","name":"sessionid","value":"synthetic-session","expires":-1,"httpOnly":True,"secure":True},
        {"domain":".unrelated.invalid","name":"other","value":"synthetic-foreign"}])
    assert "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t\tsessionid" in text
    assert "synthetic-foreign" not in text


def test_saved_account_uses_api_exports_without_browser_and_preview_masks_screen(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            ready=await wait_state(control,"ready")
            assert ready["session_generation"]==1 and ready["completed_request_id"]==1
            assert contexts==[] and control.api_client.calls==[1]
            assert not ready["browser_available"]
            await control.open_login_preview()
            assert contexts[0].page.submits==0 and contexts[0].page.fills==[]
            data=await control.screen();assert data.startswith(b"\x89PNG")
            screenshot=contexts[0].page.screenshot_options
            assert "path" not in screenshot and screenshot["mask"][0].selector==PASSWORD_SELECTOR
            assert PASSWORD_SELECTOR.startswith("input:") and "textarea" in PASSWORD_SELECTOR and '[contenteditable="true"]' in PASSWORD_SELECTOR
            assert "synthetic-password" not in json.dumps(control.state.status())
            assert (control.settings.credentials_dir/"instagram.cookies.txt").is_file()
        finally:await control.close()
    asyncio.run(run())


def test_persisted_api_session_is_reused_without_browser_or_password_submission(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,authenticated=True)
        control.vault.save("synthetic-user","synthetic-password")
        await control.start()
        try:
            await control.login();await wait_state(control,"ready")
            assert contexts==[]
            with control.state._database.connection() as connection:
                assert connection.execute("SELECT login_attempted FROM instagram_auth WHERE id=1").fetchone()[0]==0
        finally:await control.close()
    asyncio.run(run())


def test_challenge_to_ready_same_request_with_single_login_and_expiry(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,result="challenge")
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            attention=await wait_state(control,"needs_attention")
            assert attention["completed_request_id"]==0 and contexts[0].page.submits==0
            assert contexts[0].page.fills==[] and control._browser_ephemeral
            assert contexts[0].added_cookies[0]["name"]=="sessionid"
            contexts[0].authenticated=True;contexts[0].page.url=HOME_URL
            ready=await wait_state(control,"ready")
            assert ready["request_id"]==1 and ready["completed_request_id"]==1 and ready["session_generation"]==1
            assert contexts[0].page.submits==0 and len(control.api_client.adopted)==1
        finally:await control.close()
    asyncio.run(run())


def test_restart_never_repeats_consumed_automatic_password_attempt(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,result="challenge")
        control.vault.save("synthetic-user","synthetic-password")
        control.state.configure(True);request=control.state.request_refresh()
        assert control.state.consume_login_attempt(request)
        await control.start()
        try:
            await wait_state(control,"needs_attention")
            assert contexts[0].page.submits==0 and contexts[0].page.fills==[]
        finally:await control.close()
    asyncio.run(run())


def test_api_refresh_preserves_profile_but_explicit_account_mutations_clear_it(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        control.settings.profile_dir.mkdir(mode=0o700)
        sentinel=control.settings.profile_dir/"synthetic-profile-sentinel"
        sentinel.write_bytes(b"synthetic-retained-profile")
        control.vault.save("synthetic-user","synthetic-password")
        await control.start()
        try:
            await control.login()
            await wait_state(control,"ready")
            assert contexts==[] and sentinel.read_bytes()==b"synthetic-retained-profile"
            control.api_store.save("synthetic-user",cookies={"csrftoken":"synthetic"},pending_stage="checkpoint")
            await control.set_totp(SYNTHETIC_TOTP_KEY)
            await control.remove_totp()
            assert control.api_store.load("synthetic-user") is not None
            control.last_account_write=0
            await control.save_account("synthetic-user","synthetic-replacement")
            assert control.api_store.load("synthetic-user") is None
            assert not sentinel.exists()
            sentinel.write_bytes(b"synthetic-new-profile")
            await control.delete_account()
            assert not sentinel.exists()
            assert control.api_store.load("synthetic-user") is None
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("checkpoint",[None,"https://instagram.com.unrelated.invalid/challenge/synthetic/","https://www.instagram.com/"])
def test_pending_api_without_safe_challenge_waits_for_explicit_same_request_retry(tmp_path,checkpoint):
    class PendingApi(FakeApiClient):
        def refresh(self,account,request_id,state,*,stop_event=None):
            self.calls.append(request_id)
            if len(self.calls)==1:
                assert state.consume_login_attempt(request_id)
                return api_result("needs_attention",checkpoint)
            assert not state.consume_login_attempt(request_id)
            return api_result()
    async def run():
        api=PendingApi();control,contexts=controller(tmp_path,api_client=api)
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            attention=await wait_state(control,"needs_attention")
            assert not attention["browser_available"] and attention["interaction_id"] is None
            assert "direkt in Instagram" in attention["message"]
            await asyncio.sleep(.04)
            assert api.calls==[1] and contexts==[]
            await control.login()
            ready=await wait_state(control,"ready")
            assert ready["request_id"]==1 and ready["completed_request_id"]==1 and api.calls==[1,1]
        finally:await control.close()
    asyncio.run(run())


def test_human_confirmation_never_uses_automatic_dom_password_or_totp(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,result="challenge")
        async def forbidden(*args):raise AssertionError("No automatic DOM credentials")
        control._try_submit_once=control._try_totp_once=forbidden
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            await wait_state(control,"needs_attention")
            assert control._browser_ephemeral and contexts[0].page.url.endswith("/challenge/synthetic/")
            assert contexts[0].page.fills==[] and contexts[0].page.submits==0
            assert not (control.settings.profile_dir/"Default").exists()
            contexts[0].authenticated=True;contexts[0].page.url=HOME_URL
            await wait_state(control,"ready")
            assert len(control.api_client.adopted)==1 and len(control.api_client.calls)==1
        finally:await control.close()
    asyncio.run(run())


def test_challenge_browser_launch_is_ephemeral_scoped_and_uses_api_user_agent(tmp_path,monkeypatch):
    async def run():
        context=FakeContext();calls={}
        class Browser:
            async def new_context(self,**kwargs):calls["context"]=kwargs;return context
            async def close(self):
                assert os.path.isdir(calls["launch"]["env"]["HOME"])
                calls["browser_closed"]=True
        class Chromium:
            async def launch(self,**kwargs):calls["launch"]=kwargs;return Browser()
            async def launch_persistent_context(self,*args,**kwargs):raise AssertionError("No persistent profile for API challenge")
        class Playwright:
            chromium=Chromium()
            async def stop(self):
                assert os.path.isdir(calls["launch"]["env"]["HOME"])
                calls["stopped"]=True
        class Manager:
            async def start(self):return Playwright()
        monkeypatch.setitem(sys.modules,"playwright.async_api",SimpleNamespace(async_playwright=Manager))
        control=BrowserController(options(tmp_path),api_client=FakeApiClient())
        control.settings.profile_dir.mkdir(mode=0o700)
        sentinel=control.settings.profile_dir/"synthetic-original-profile";sentinel.write_bytes(b"retained")
        monkeypatch.setenv("HOME",str(control.settings.profile_dir))
        monkeypatch.setenv("XDG_CONFIG_HOME","synthetic-original-config")
        monkeypatch.setenv("XDG_CACHE_HOME","synthetic-original-cache")
        try:
            await control._open_browser(ephemeral=True,user_agent="synthetic-api-agent")
            assert calls["context"]["user_agent"]=="synthetic-api-agent"
            assert calls["context"]["service_workers"]=="block" and not calls["context"]["accept_downloads"]
            assert context.route_handler==control._route and context.ws_handler
            environment=calls["launch"]["env"]
            assert environment["HOME"]!=str(control.settings.profile_dir)
            assert environment["XDG_CONFIG_HOME"]==os.path.join(environment["HOME"],"config")
            assert environment["XDG_CACHE_HOME"]==os.path.join(environment["HOME"],"cache")
            if os.name=="posix":
                assert os.path.dirname(environment["HOME"])=="/tmp"
                assert stat.S_IMODE(os.stat(environment["HOME"]).st_mode)==0o700
            assert os.environ["HOME"]==str(control.settings.profile_dir)
            assert os.environ["XDG_CONFIG_HOME"]=="synthetic-original-config"
            assert os.environ["XDG_CACHE_HOME"]=="synthetic-original-cache"
            assert list(control.settings.profile_dir.iterdir())==[sentinel] and sentinel.read_bytes()==b"retained"
        finally:await control.close()
        assert calls["browser_closed"] and calls["stopped"]
        assert not os.path.exists(calls["launch"]["env"]["HOME"])
        await control.close()  # Reentry cannot remove any unrelated temp directory.
    asyncio.run(run())


@pytest.mark.parametrize("cancelled",[False,True])
def test_failed_or_cancelled_ephemeral_launch_cleans_only_its_private_home(tmp_path,monkeypatch,cancelled):
    async def run():
        calls={}
        class Chromium:
            async def launch(self,**kwargs):
                calls["home"]=kwargs["env"]["HOME"]
                assert os.path.isdir(calls["home"])
                if cancelled:raise asyncio.CancelledError
                raise RuntimeError("synthetic launch failure")
        class Playwright:
            chromium=Chromium()
            async def stop(self):
                assert os.path.isdir(calls["home"])
                calls["stopped"]=True
        class Manager:
            async def start(self):return Playwright()
        monkeypatch.setitem(sys.modules,"playwright.async_api",SimpleNamespace(async_playwright=Manager))
        control=BrowserController(options(tmp_path),api_client=FakeApiClient())
        retained=tmp_path/"synthetic-unrelated-temp";retained.write_bytes(b"retain")
        try:
            with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
                await control._open_browser(ephemeral=True,user_agent="synthetic-api-agent")
            assert calls["stopped"] and not os.path.exists(calls["home"])
            assert control._ephemeral_home is None and control.playwright is None
            assert retained.read_bytes()==b"retain"
        finally:await control.close()
    asyncio.run(run())


def test_api_verification_failure_never_publishes_browser_cookie_as_ready(tmp_path):
    class Unverified(FakeApiClient):
        def accept_browser_session(self,account,cookies,*,stop_event=None):
            self.adopted.append(cookies)
            return api_result("needs_attention")
    async def run():
        api=Unverified("challenge");control,contexts=controller(tmp_path,api_client=api)
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            await wait_state(control,"needs_attention")
            contexts[0].authenticated=True;contexts[0].page.url=HOME_URL
            await asyncio.sleep(.05)
            assert len(api.adopted)==1  # No continuous API polling after a network failure.
            assert control.state.status()["session_generation"]==0
            assert not (control.settings.credentials_dir/"instagram.cookies.txt").exists()
        finally:await control.close()
    asyncio.run(run())


def test_cancelled_api_thread_is_drained_before_session_and_account_deletion(tmp_path):
    async def run():
        entered=threading.Event();stopped=threading.Event()
        control,contexts=controller(tmp_path)
        class BlockingApi(FakeApiClient):
            def refresh(self,account,request_id,state,*,stop_event=None):
                entered.set()
                assert stop_event.wait(2)
                control.api_store.save(account["username"],cookies={"csrftoken":"synthetic"},pending_stage="checkpoint")
                stopped.set()
                return api_result()
        control.api_client=BlockingApi()
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            assert await asyncio.to_thread(entered.wait,1)
            await control.delete_account()
            assert stopped.is_set() and contexts==[]
            assert not control.vault.configured() and control.api_store.load("synthetic-user") is None
            assert not (control.settings.credentials_dir/"instagram.cookies.txt").exists()
        finally:await control.close()
    asyncio.run(run())


def test_controller_integrates_real_adapter_and_encrypted_session_store_offline(tmp_path):
    calls=[]
    class Context:
        user_agent="synthetic-api-agent"
        def __init__(self):self.values={}
        def save_session(self):return self.values
    class Loader:
        def __init__(self,**kwargs):self.context=Context()
        def login(self,username,password):
            calls.append("password")
            self.context.values={"sessionid":"synthetic-session","csrftoken":"synthetic-csrf"}
        def load_session(self,username,cookies):self.context.values=cookies
        def test_login(self):calls.append("verify");return "synthetic-user"
        def close(self):pass
    async def run():
        control,contexts=controller(tmp_path)
        control.api_client=InstagramApiClient(control.api_store,loader_factory=Loader)
        control.vault.save("synthetic-user","synthetic-password")
        await control.start()
        try:
            await control.login();await wait_state(control,"ready")
            assert calls==["password","verify"] and contexts==[]
            stored=control.api_store.load("synthetic-user")
            assert stored.pending_stage=="ready" and stored.cookies["sessionid"]=="synthetic-session"
            encrypted=(control.settings.secrets_dir/"instagram.api-session.enc").read_bytes()
            assert b"synthetic-session" not in encrypted
            control.state.configure(True)  # Explicitly end the cooldown for this offline reuse check.
            await control.login();await wait_state(control,"ready")
            assert calls==["password","verify","verify"] and contexts==[]
            assert control.state.status()["session_generation"]==2
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("label",["Anderes Profil verwenden","Anderes Konto verwenden","Mit einem anderen Konto anmelden","Use another profile","Use another account","Log into another account"])
def test_profile_chooser_opens_form_without_consuming_login_budget(tmp_path,label):
    async def run():
        control,contexts=controller(tmp_path)
        control.vault.save("synthetic-user","synthetic-password")
        control.state.configure(True);request=control.state.request_refresh()
        try:
            await control._open_browser();control.page.url=LOGIN_URL
            context=contexts[0];context.chooser=True;context.chooser_label=label
            assert not await control._try_submit_once(request)
            assert context.chooser_clicks==1 and context.page.fills==[] and context.page.submits==0
            with control.state._database.connection() as connection:
                assert connection.execute("SELECT login_attempted FROM instagram_auth WHERE id=1").fetchone()[0]==0
            assert await control._try_submit_once(request)
            assert context.page.submits==1 and len(context.page.fills)==2
            assert not await control._try_submit_once(request)
            assert context.page.submits==1
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("label",["Weiter","Profile aus diesem Browser entfernen"])
def test_profile_chooser_never_continues_or_removes_a_remembered_profile(tmp_path,label):
    async def run():
        control,contexts=controller(tmp_path)
        control.state.configure(True);request=control.state.request_refresh()
        try:
            await control._open_browser();control.page.url=LOGIN_URL
            contexts[0].chooser=True;contexts[0].chooser_label=label
            assert not await control._try_submit_once(request)
            assert contexts[0].chooser_clicks==0 and contexts[0].page.submits==0
            with control.state._database.connection() as connection:
                assert connection.execute("SELECT login_attempted FROM instagram_auth WHERE id=1").fetchone()[0]==0
        finally:await control.close()
    asyncio.run(run())


def test_existing_form_is_used_without_touching_an_alternate_profile_button(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        control.vault.save("synthetic-user","synthetic-password")
        control.state.configure(True);request=control.state.request_refresh()
        try:
            await control._open_browser();control.page.url=LOGIN_URL
            contexts[0].chooser=True;contexts[0].form_also_visible=True
            assert await control._try_submit_once(request)
            assert contexts[0].chooser_clicks==0 and contexts[0].page.submits==1
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("label",["Anmelden","Log in","Log In","Einloggen"])
def test_exact_role_submit_is_found_before_budget_and_can_enable_after_fill(tmp_path,label):
    async def run():
        control,contexts=controller(tmp_path)
        control.vault.save("synthetic-user","synthetic-password")
        control.state.configure(True);request=control.state.request_refresh()
        try:
            await control._open_browser();control.page.url=LOGIN_URL
            context=contexts[0];context.native_submit=False;context.role_submit_label=label;context.submit_disabled_until_fill=True
            assert await control.page.get_by_role("button",name=label,exact=True).is_visible()
            assert not await control.page.get_by_role("button",name=label,exact=True).is_enabled()
            assert await control._try_submit_once(request)
            assert context.page.submits==1 and context.page.clicked_controls[-1]==label
            assert len(context.page.fills)==2
        finally:await control.close()
    asyncio.run(run())


def test_native_submit_is_preferred_when_native_and_role_controls_are_present(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        control.vault.save("synthetic-user","synthetic-password")
        control.state.configure(True);request=control.state.request_refresh()
        try:
            await control._open_browser();control.page.url=LOGIN_URL
            contexts[0].role_submit_label="Anmelden"
            assert await control._try_submit_once(request)
            assert contexts[0].page.clicked_controls[-1]==SUBMIT_SELECTOR
            assert "Anmelden" not in contexts[0].page.clicked_controls
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("other_control",[None,"Mit Facebook anmelden","Weiter"])
def test_absent_submit_or_other_login_provider_never_spends_password_budget(tmp_path,other_control):
    async def run():
        control,contexts=controller(tmp_path)
        control.state.configure(True);request=control.state.request_refresh()
        try:
            await control._open_browser();control.page.url=LOGIN_URL
            context=contexts[0];context.native_submit=False;context.role_submit_label=other_control
            assert not await control._try_submit_once(request)
            assert context.page.fills==[] and context.page.submits==0
            assert other_control not in context.page.clicked_controls
            with control.state._database.connection() as connection:
                assert connection.execute("SELECT login_attempted FROM instagram_auth WHERE id=1").fetchone()[0]==0
        finally:await control.close()
    asyncio.run(run())


def test_profile_owner_only_one_sidecar_and_delete_does_not_touch_other_sources(tmp_path):
    async def run():
        first,_=controller(tmp_path);second,_=controller(tmp_path)
        await first.start()
        try:
            with pytest.raises(BrowserControlError):await second.start()
            first.settings.credentials_dir.mkdir(mode=0o700)
            other=first.settings.credentials_dir/"youtube.cookies.txt";other.write_text("synthetic-other-site")
            await first.save_account("synthetic-user","synthetic-password")
            await wait_state(first,"ready")
            result=await first.delete_account()
            assert not result["configured"] and not result["browser_available"] and result["interaction_id"] is None
            assert other.read_text()=="synthetic-other-site"
            assert list(first.settings.profile_dir.iterdir())==[]
            assert not (first.settings.credentials_dir/"instagram.cookies.txt").exists()
        finally:await first.close()
    asyncio.run(run())


def test_delete_holds_scheduler_operation_lock_until_account_is_unconfigured(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        control.vault.save("synthetic-user","synthetic-password");control.state.configure(True);control.state.request_refresh()
        entered=asyncio.Event();release=asyncio.Event();real_close=control._close_browser
        async def slow_close():entered.set();await release.wait();await real_close()
        control._close_browser=slow_close
        deleting=asyncio.create_task(control.delete_account());await entered.wait()
        control.poll_task=asyncio.create_task(control._poll())
        await asyncio.sleep(.03);assert not contexts
        release.set();result=await deleting;assert not result["configured"]
        await asyncio.sleep(.04);assert not contexts
        control._close_browser=real_close;await control.close()
    asyncio.run(run())


def test_cancelled_cookie_export_thread_is_drained_before_delete(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,authenticated=True)
        control.vault.save("synthetic-user","synthetic-password");control.state.configure(True);request=control.state.request_refresh()
        await control._open_browser(ephemeral=True,user_agent="synthetic-api-agent");control.page.url=HOME_URL
        entered=threading.Event();release=threading.Event();real_import=control.sessions.import_text
        def slow_import(*args):entered.set();release.wait(2);return real_import(*args)
        control.sessions.import_text=slow_import
        async def export():
            async with control.control_lock:await control._export_ready(request)
        control.login_task=asyncio.create_task(export())
        assert await asyncio.to_thread(entered.wait,1)
        deleting=asyncio.create_task(control.delete_account())
        await asyncio.sleep(.03);assert not deleting.done()
        release.set();await deleting
        await asyncio.sleep(.03)
        assert not (control.settings.credentials_dir/"instagram.cookies.txt").exists()
        assert not control.state.status()["configured"] and not control.state.status()["browser_available"]
        await control.close()
    asyncio.run(run())


def test_idle_decision_is_rechecked_after_waiting_for_browser_lock(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        control.state.initialize()
        context=FakeContext();control.context=context
        control.page=await context.new_page()
        control.interaction_id="a"*48
        control.last_activity=0
        await control.control_lock.acquire()
        control.poll_task=asyncio.create_task(control._poll())
        await asyncio.sleep(.02)
        assert control.operation_lock.locked()  # Poller parked behind the browser operation.
        control.last_activity=time.monotonic()
        control.control_lock.release()
        await asyncio.sleep(.03)
        assert control.page is not None and not context.closed
        await control.close()
    asyncio.run(run())


def test_gated_browser_launch_does_not_become_idle_before_preview_finishes(tmp_path):
    async def run():
        entered=asyncio.Event();release=asyncio.Event();context=FakeContext()
        original_route=context.route
        async def delayed_route(*args):
            entered.set()
            await release.wait()
            await original_route(*args)
        context.route=delayed_route
        async def factory(settings):return context
        control=BrowserController(options(tmp_path),browser_factory=factory)
        await control.start()
        try:
            opening=asyncio.create_task(control.open_login_preview())
            await entered.wait()
            assert control.context is context and control.last_activity>0
            await asyncio.sleep(.03)
            release.set();await opening
            await asyncio.sleep(.03)
            assert control.page is not None and not context.closed
            assert control.state.status()["browser_available"]
        finally:
            release.set()
            await control.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name!="posix",reason="Chromium Linux singleton links")
def test_recreated_container_singletons_are_unlinked_without_following_targets(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        control.settings.profile_dir.mkdir(mode=0o700)
        outside=tmp_path/"outside-kept";outside.write_text("synthetic-profile-sentinel")
        (control.settings.profile_dir/"SingletonLock").symlink_to("old-disposable-container-99999999")
        for name in ("SingletonSocket","SingletonCookie"):(control.settings.profile_dir/name).symlink_to(outside)
        retained=control.settings.profile_dir/"retained-profile-data";retained.write_text("synthetic-retained-profile")
        try:
            await control._open_browser()  # Lazy entry must acquire the owner lock too.
            assert control.owner.stream is not None
            assert not any(path.name.startswith("Singleton") for path in control.settings.profile_dir.iterdir())
            assert outside.read_text()=="synthetic-profile-sentinel" and retained.read_text()=="synthetic-retained-profile"
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name!="posix",reason="Chromium Linux singleton links")
def test_live_same_host_process_and_unexpected_singletons_are_preserved(tmp_path):
    control,contexts=controller(tmp_path)
    control.settings.secrets_dir.mkdir(mode=0o700);control.settings.profile_dir.mkdir(mode=0o700)
    control.owner.acquire()
    lock=control.settings.profile_dir/"SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    try:
        with pytest.raises(BrowserControlError,match="laufenden Prozess"):control._clear_stale_singletons()
        assert lock.is_symlink()
        lock.unlink();lock.symlink_to("old-disposable-container-99999999")
        unexpected=control.settings.profile_dir/"SingletonSocket";unexpected.write_text("synthetic-do-not-remove")
        with pytest.raises(BrowserControlError,match="unerwartete"):control._clear_stale_singletons()
        assert lock.is_symlink() and unexpected.read_text()=="synthetic-do-not-remove"
    finally:control.owner.release()


@pytest.mark.skipif(os.name!="posix",reason="Chromium Linux singleton links")
def test_singleton_cleanup_requires_owner_and_rejects_live_profile_process(tmp_path,monkeypatch):
    control,_=controller(tmp_path)
    control.settings.secrets_dir.mkdir(mode=0o700);control.settings.profile_dir.mkdir(mode=0o700)
    lock=control.settings.profile_dir/"SingletonLock";lock.symlink_to("old-disposable-container-99999999")
    with pytest.raises(BrowserControlError,match="exklusiv"):control._clear_stale_singletons()
    control.owner.acquire()
    try:
        monkeypatch.setattr(control,"_profile_in_use",lambda:True)
        with pytest.raises(BrowserControlError,match="laufenden Browser"):control._clear_stale_singletons()
        assert lock.is_symlink()
    finally:control.owner.release()


@pytest.mark.skipif(os.name!="posix",reason="Linux proc inspection")
def test_profile_process_detection_matches_only_exact_user_data_directory(tmp_path):
    control,_=controller(tmp_path)
    proc=tmp_path/"synthetic-proc";process=proc/"123";process.mkdir(parents=True)
    command=process/"cmdline"
    command.write_bytes(b"chromium\0--user-data-dir="+str(control.settings.profile_dir.absolute()).encode()+b"-different\0")
    assert not control._profile_in_use(proc)
    command.write_bytes(b"chromium\0--user-data-dir\0"+str(control.settings.profile_dir.absolute()).encode()+b"\0")
    assert control._profile_in_use(proc)


def test_action_bounds_stale_identity_and_preview_without_credentials(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        await control.start()
        try:
            status=await control.open_login_preview()
            assert not status["configured"] and status["browser_available"]
            assert contexts[0].page.fills==[] and contexts[0].page.submits==0
            await control.action(ActionInput(interaction_id=status["interaction_id"],kind="click",x=.5,y=.25))
            assert contexts[0].page.mouse.clicks==[(500,190)]
            with pytest.raises(BrowserControlError):await control.action(ActionInput(interaction_id="a"*48,kind="key",key="Enter"))
            for body in ({"kind":"key","key":"Control+L"},{"kind":"click","x":2,"y":0},{"kind":"scroll","delta":1001}):
                with pytest.raises(ValueError):ActionInput(interaction_id=status["interaction_id"],**body)
        finally:await control.close()
    asyncio.run(run())


def test_private_http_contract_has_static_errors_and_no_credential_echo(tmp_path):
    control,_=controller(tmp_path,result="challenge")
    with TestClient(create_app(control.settings,control)) as client:
        assert client.get("/status").json()["state"]=="unconfigured"
        response=client.put("/account",json={"username":"synthetic-user","password":"synthetic-password"})
        assert response.status_code==200 and "synthetic" not in response.text
        invalid=client.put("/account",json={"username":"synthetic-user","password":"p"*5000})
        assert invalid.status_code==422 and "synthetic-user" not in invalid.text and "pppppp" not in invalid.text
        assert client.post("/action",json={"interaction_id":"a"*48,"kind":"key","key":"Control+L"}).status_code==422
        deleted=client.delete("/account")
        assert deleted.status_code==200 and not deleted.json()["configured"]


SYNTHETIC_TOTP_KEY="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class OTPNode:
    def __init__(self,page,kind,count=1):self.page,self.kind,self.number=page,kind,count
    @property
    def first(self):return self
    async def count(self):return self.number
    async def is_visible(self):return bool(self.number)
    async def is_editable(self):return True
    async def is_checked(self):return self.page.context.app_selected
    def locator(self,selector):return OTPScope(self.page,picker=self.kind=="app-radio")
    async def fill(self,value,**kwargs):
        self.page.context.otp_value=value
        self.page.context.otp_fills.append(value)
    async def click(self,**kwargs):
        context=self.page.context
        context.method_clicks.append(self.kind)
        if self.kind=="other-method":context.picker_open=True
        elif self.kind=="app-radio":context.app_selected=True
        elif self.kind=="app-button":context.app_selected=True;context.picker_open=False;context.otp_text="Öffne deine Authentifizierungs-App"
        elif self.kind=="picker-forward":context.picker_open=False;context.otp_text="Öffne deine Authentifizierungs-App"
        elif self.kind=="otp-submit":context.otp_submits+=1


class OTPScope:
    def __init__(self,page,picker=False):self.page,self.picker=page,picker
    async def count(self):return 1
    def locator(self,selector):
        count=0 if self.picker else self.page.context.otp_selector_count(selector)
        return OTPNode(self.page,"input",count)
    def get_by_text(self,pattern,**kwargs):return OTPNode(self.page,"text",int(bool(pattern.search(self.page.context.otp_text))))
    def get_by_role(self,role,*,name,exact):
        if self.picker:
            return OTPNode(self.page,"picker-forward",int(name=="Weiter" and self.page.context.app_selected))
        return OTPNode(self.page,"otp-submit",int(name=="Weiter"))


class OTPPage(FakePage):
    def locator(self,selector):
        if selector==OTP_INPUT_SELECTOR:return OTPNode(self,"input",self.context.otp_selector_count(selector))
        return super().locator(selector)
    def get_by_role(self,role,*,name,exact):
        context=self.context
        if hasattr(name,"fullmatch"):context.pattern_lookups+=1
        matches_app=bool(name.fullmatch(context.app_method_label)) if hasattr(name,"fullmatch") else name==context.app_method_label
        if matches_app and role==context.app_method_role and context.picker_open:return OTPNode(self,"app-"+role)
        if name==context.other_method_label and context.allow_picker and not context.picker_open:return OTPNode(self,"other-method")
        return OTPNode(self,"absent",0)
    def get_by_text(self,text,*,exact):
        return OTPTitle(self,int(self.context.title_fallback and self.context.picker_open and text=="Authentifizierungs-App" and exact))


class OTPTitle(OTPNode):
    def __init__(self,page,count):super().__init__(page,"title",count)
    def locator(self,selector):return OTPTitleScope(self.page,selector.count(".."))
    async def click(self,**kwargs):
        self.page.context.method_clicks.append("app-title")
        self.page.context.app_selected=self.page.context.title_selects


class OTPTitleChoice(OTPNode):
    async def get_attribute(self,name):
        if name=="aria-checked" and self.page.context.title_aria_choice:return "true" if self.page.context.app_selected else "false"
        return None


class OTPTitleScope:
    def __init__(self,page,depth):self.page,self.depth=page,depth
    async def count(self):return 1
    def locator(self,selector):
        context=self.page.context
        if selector==AUTHENTICATOR_CHOICE_SELECTOR:return OTPTitleChoice(self.page,"radio",context.title_radio_count if self.depth>=2 else 0)
        if selector=="xpath=self::body | self::html":return OTPNode(self.page,"body",int(self.depth>=6))
        if selector in (OTP_INPUT_SELECTOR,LOGIN_PASSWORD_SELECTOR):return OTPNode(self.page,"form-field",int(context.title_has_code))
        return OTPNode(self.page,"absent",0)
    def get_by_role(self,role,*,name,exact):
        return OTPNode(self.page,"picker-forward",int(self.depth>=4 and self.page.context.app_selected and name=="Weiter"))


class OTPContext(FakeContext):
    def __init__(self):
        super().__init__()
        self.otp_text="Öffne deine Authentifizierungs-App";self.otp_count=1
        self.otp_value="999999";self.otp_fills=[];self.otp_submits=0;self.method_clicks=[]
        self.picker_open=False;self.allow_picker=False;self.app_selected=False;self.other_method_label="Andere Methode"
        self.app_method_label="Authentifizierungs-App";self.app_method_role="radio";self.pattern_lookups=0
        self.otp_placeholder_only=False
        self.title_fallback=False;self.title_radio_count=1;self.title_selects=True;self.title_aria_choice=False;self.title_has_code=False
    def otp_selector_count(self,selector):
        if selector!=OTP_INPUT_SELECTOR:return 0
        if self.otp_placeholder_only and 'input[placeholder="Code" i]:visible' not in selector:return 0
        return self.otp_count
    async def new_page(self):self.page=OTPPage(self);self.pages.append(self.page);return self.page


async def otp_controller(tmp_path,monkeypatch,*,configured=True):
    context=OTPContext()
    async def factory(settings):return context
    async def synthetic_code(secret):return "123456"
    monkeypatch.setattr("vela.browser_service.fresh_totp",synthetic_code)
    control=BrowserController(options(tmp_path),browser_factory=factory)
    control.vault.save("synthetic-user","synthetic-password")
    if configured:control.vault.set_totp(SYNTHETIC_TOTP_KEY)
    control.state.configure(True);control.state.publish(totp_configured=configured)
    request=control.state.request_refresh()
    await control._open_browser();control.page.url="https://www.instagram.com/accounts/login/two_factor/"
    return control,context,request


def test_otp_replaces_old_value_submits_once_and_budget_survives_restart(tmp_path,monkeypatch):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        try:
            assert await control._try_totp_once(request)
            assert context.otp_value=="123456" and context.otp_fills==["123456"] and context.otp_submits==1
            assert not await control._try_totp_once(request)
            assert not InstagramAuthState(control.settings.state_dir).consume_totp_attempt(request)
            assert context.otp_submits==1
            assert "123456" not in json.dumps(control.state.status())
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("description",["Code aus WhatsApp eingeben","Öffne deine Authentifizierungs-App. Alternativ: SMS-Code eingeben","Öffne deine Authentifizierungs-App. E-Mail-Code"])
def test_whatsapp_and_ambiguous_mixed_forms_never_receive_totp(tmp_path,monkeypatch,description):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text=description
        try:
            assert not await control._try_totp_once(request)
            assert context.otp_fills==[] and context.otp_submits==0
            assert control.state.totp_attempt_available(request)
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("app_prompt",[True,False])
def test_placeholder_only_code_field_requires_explicit_authenticator_form(tmp_path,monkeypatch,app_prompt):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        # This field has only placeholder=Code, without name/autocomplete/inputmode.
        context.otp_placeholder_only=True
        context.otp_text="Öffne deine Authentifizierungs-App" if app_prompt else "Code aus WhatsApp eingeben"
        try:
            assert await control._try_totp_once(request) is app_prompt
            assert context.otp_fills==(["123456"] if app_prompt else [])
            assert context.otp_submits==int(app_prompt)
            assert control.state.totp_attempt_available(request) is not app_prompt
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("other_method_label",["Andere Methode","Andere Methode nutzen"])
def test_otp_method_picker_chooses_only_app_radio_and_its_own_continue(tmp_path,monkeypatch,other_method_label):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.allow_picker=True;context.other_method_label=other_method_label
        try:
            assert not await control._try_totp_once(request)
            assert context.method_clicks==["other-method"] and control.state.totp_attempt_available(request)
            assert not await control._try_totp_once(request)
            assert context.method_clicks==["other-method","app-radio","picker-forward"]
            assert context.otp_fills==[] and control.state.totp_attempt_available(request)
            assert await control._try_totp_once(request)
            assert context.otp_submits==1
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("role",["radio","button"])
def test_app_method_accepts_only_the_complete_observed_description(tmp_path,monkeypatch,role):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.picker_open=True
        context.app_method_label="Authentifizierungs-App Du rufst den Code über deine Authentifizierungs-App ab.";context.app_method_role=role
        try:
            assert await control._choose_totp_method(request)
            assert context.method_clicks==(["app-radio","picker-forward"] if role=="radio" else ["app-button"])
            assert context.pattern_lookups>0 and context.otp_fills==[] and control.state.totp_attempt_available(request)
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("name",["Authentifizierungs-App synthetic suffix","Authentifizierungs-App Du rufst den Code über deine Authentifizierungs-App ab. synthetic suffix","synthetic prefix Authentifizierungs-App Du rufst den Code über deine Authentifizierungs-App ab."])
def test_app_method_rejects_unobserved_accessible_name_suffixes(tmp_path,monkeypatch,name):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.picker_open=True;context.app_method_label=name
        try:
            assert not await control._choose_totp_method(request)
            assert context.method_clicks==[] and context.otp_fills==[] and control.state.totp_attempt_available(request)
        finally:await control.close()
    asyncio.run(run())


def test_exact_app_method_title_has_priority_over_description_fallback(tmp_path,monkeypatch):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.picker_open=True
        try:
            assert await control._choose_totp_method(request)
            assert context.method_clicks==["app-radio","picker-forward"] and context.pattern_lookups==0
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("aria_choice",[False,True])
def test_exact_app_title_selects_its_single_unnamed_choice_and_local_continue(tmp_path,monkeypatch,aria_choice):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.picker_open=True;context.title_fallback=True
        context.app_method_label="synthetic inaccessible role name";context.title_aria_choice=aria_choice
        try:
            assert await control._choose_totp_method(request)
            assert context.method_clicks==["app-title","picker-forward"]
            assert context.otp_fills==[] and control.state.totp_attempt_available(request)
            assert not await control._choose_totp_method(request)
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("radio_count",[0,2])
def test_exact_app_title_without_single_associated_choice_stays_human(tmp_path,monkeypatch,radio_count):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.picker_open=True;context.title_fallback=True
        context.app_method_label="synthetic inaccessible role name";context.title_radio_count=radio_count
        try:
            assert not await control._choose_totp_method(request)
            assert context.method_clicks==[] and context.otp_fills==[] and control.state.totp_attempt_available(request)
        finally:await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("has_code,selects",[(True,True),(False,False)])
def test_title_fallback_rejects_code_form_and_never_continues_unchecked_choice(tmp_path,monkeypatch,has_code,selects):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch)
        context.otp_text="Code aus WhatsApp eingeben";context.picker_open=True;context.title_fallback=True
        context.app_method_label="synthetic inaccessible role name";context.title_has_code=has_code;context.title_selects=selects
        try:
            assert await control._choose_totp_method(request) is not has_code
            assert context.method_clicks==([] if has_code else ["app-title"])
            assert context.otp_fills==[] and control.state.totp_attempt_available(request)
        finally:await control.close()
    asyncio.run(run())


def test_no_seed_or_ambiguous_otp_inputs_keep_human_flow(tmp_path,monkeypatch):
    async def run():
        control,context,request=await otp_controller(tmp_path,monkeypatch,configured=False)
        try:
            assert not await control._try_totp_once(request)
            assert context.otp_fills==[] and context.method_clicks==[]
            control.vault.set_totp(SYNTHETIC_TOTP_KEY);control.state.publish(totp_configured=True);context.otp_count=2
            assert not await control._try_totp_once(request)
            assert context.otp_fills==[] and control.state.totp_attempt_available(request)
        finally:await control.close()
    asyncio.run(run())


def test_private_totp_api_returns_only_bool_and_static_validation_errors(tmp_path):
    control,_=controller(tmp_path)
    control.vault.save("synthetic-user","synthetic-password")
    with TestClient(create_app(control.settings,control)) as client:
        before=client.get("/status").json()
        saved=client.put("/totp",json={"secret":SYNTHETIC_TOTP_KEY})
        assert saved.status_code==200 and saved.json()["totp_configured"]
        assert SYNTHETIC_TOTP_KEY not in saved.text and saved.json()["request_id"]==before["request_id"]
        invalid=client.put("/totp",json={"secret":"synthetic-invalid-private-input"})
        assert invalid.status_code==422 and "synthetic-invalid" not in invalid.text
        removed=client.delete("/totp")
        assert removed.status_code==200 and not removed.json()["totp_configured"]
