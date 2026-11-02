"""Synthetic credentials and fake browser only; no external login is performed."""
import asyncio
import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from vela.browser_service import (AccountStorageError,AccountVault,ActionInput,BrowserController,
    BrowserControlError,BrowserSettings,HOME_URL,LOGIN_URL,LOGIN_PASSWORD_SELECTOR,PASSWORD_SELECTOR,
    SUBMIT_SELECTOR,USERNAME_SELECTOR,allowed_browser_request,cookie_text_from_browser,create_app)
from vela.instagram_auth import InstagramAuthState


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
        if self.selector in (USERNAME_SELECTOR,LOGIN_PASSWORD_SELECTOR):return self.page.url==LOGIN_URL
        return False
    async def fill(self,value):self.page.fills.append((self.selector,value))
    async def click(self,**kwargs):
        if self.selector=="Decline optional cookies":self.page.context.overlay=False;return
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
    async def route(self,pattern,handler):self.route_handler=handler
    async def route_web_socket(self,pattern,handler):self.ws_handler=handler
    def on(self,name,handler):self.events[name]=handler
    async def new_page(self):self.page=FakePage(self);self.pages.append(self.page);return self.page
    async def close(self):
        self.closed=True
        for page in self.pages:await page.close()
    async def cookies(self,*args):
        return [{"domain":".instagram.com","path":"/","secure":True,"httpOnly":True,"expires":time.time()+3600,
            "name":"sessionid" if self.authenticated else "csrftoken","value":"synthetic-session-value"}]


def controller(tmp_path,result="ready",authenticated=False):
    contexts=[]
    async def factory(settings):
        context=FakeContext(result,authenticated);contexts.append(context);return context
    return BrowserController(options(tmp_path),browser_factory=factory),contexts


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


def test_saved_account_login_once_exports_and_masks_screen(tmp_path):
    async def run():
        control,contexts=controller(tmp_path)
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            ready=await wait_state(control,"ready")
            assert ready["session_generation"]==1 and ready["completed_request_id"]==1
            assert contexts[0].page.submits==1
            assert [value for _,value in contexts[0].page.fills]==["synthetic-user","synthetic-password"]
            assert not contexts[0].overlay
            data=await control.screen();assert data.startswith(b"\x89PNG")
            screenshot=contexts[0].page.screenshot_options
            assert "path" not in screenshot and screenshot["mask"][0].selector==PASSWORD_SELECTOR
            assert 'name="pass"' in PASSWORD_SELECTOR and 'name*="code"' in PASSWORD_SELECTOR
            assert "synthetic-password" not in json.dumps(control.state.status())
            assert (control.settings.credentials_dir/"instagram.cookies.txt").is_file()
        finally:await control.close()
    asyncio.run(run())


def test_persisted_browser_session_is_reused_without_password_submission(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,authenticated=True)
        control.vault.save("synthetic-user","synthetic-password")
        await control.start()
        try:
            await control.login();await wait_state(control,"ready")
            assert contexts[0].page.submits==0 and contexts[0].page.fills==[]
        finally:await control.close()
    asyncio.run(run())


def test_challenge_to_ready_same_request_with_single_login_and_expiry(tmp_path):
    async def run():
        control,contexts=controller(tmp_path,result="challenge")
        await control.start()
        try:
            await control.save_account("synthetic-user","synthetic-password")
            attention=await wait_state(control,"needs_attention")
            assert attention["completed_request_id"]==0 and contexts[0].page.submits==1
            contexts[0].authenticated=True;contexts[0].page.url=HOME_URL
            ready=await wait_state(control,"ready")
            assert ready["request_id"]==1 and ready["completed_request_id"]==1 and ready["session_generation"]==1
            assert contexts[0].page.submits==1
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
        await control._open_browser();control.page.url=HOME_URL
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
