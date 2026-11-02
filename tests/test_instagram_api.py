"""Synthetic accounts and fake HTTP transport only; never contact Instagram."""
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

import pytest
import requests

from vela.browser_service import AccountVault, private_directory
from vela.instagram_api import ApiResult, InstagramApiClient, _ApiStop, _NetworkGuard
from vela.instagram_api_store import InstagramApiSessionStore, InstagramApiStorageError, safe_checkpoint_url
from vela.instagram_auth import InstagramAuthState


ACCOUNT = {"username": "synthetic-user", "password": "synthetic-password", "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"}
COOKIE = "synthetic-private-cookie"
IDENTIFIER = "synthetic-private-two-factor-identifier"
CHECKPOINT = "https://www.instagram.com/challenge/synthetic-private-token/"


@pytest.fixture(autouse=True)
def sqlite_for_tests(monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")


def setup(tmp_path):
    vault = AccountVault(tmp_path/"secrets")
    vault.save(ACCOUNT["username"], ACCOUNT["password"])
    store = InstagramApiSessionStore(vault, private_directory)
    state = InstagramAuthState(tmp_path/"state")
    state.configure(True)
    state.publish(totp_configured=True)
    request_id = state.request_refresh()
    return vault, store, state, request_id


class Transport:
    def __init__(self, *, two_factor=True, verify="ready", login="ready", otp="ready"):
        self.two_factor, self.verify, self.login, self.otp = two_factor, verify, login, otp
        self.calls = []
        self.saw_before_verify = None
        self.before_verify = None

    def install(self, monkeypatch):
        transport = self
        def send(session, request, **kwargs):
            assert kwargs["verify"] and not kwargs["proxies"] and 0 < kwargs["timeout"] <= 15
            return transport.send(session, request, **kwargs)
        monkeypatch.setattr(requests.sessions.Session, "send", send)

    def send(self, session, request, **kwargs):
        path = urlsplit(request.url).path
        self.calls.append((request.method, path))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response.cookies.set("csrftoken", "synthetic-csrf")
        session.cookies.set("csrftoken", "synthetic-csrf")
        data = {"status": "ok"}
        if path == "/api/v1/web/accounts/login/ajax/":
            if self.login == "timeout":
                raise requests.Timeout("synthetic-private-url-and-password")
            if self.login == "bad":
                data.update(authenticated=False, user=True)
            elif self.login == "feedback":
                data.update(status="fail", message="feedback_required")
            elif self.login == "challenge":
                data.update(status="fail", checkpoint_url=CHECKPOINT)
            elif self.two_factor:
                data.update(two_factor_required=True, two_factor_info={"two_factor_identifier": IDENTIFIER})
            else:
                data.update(authenticated=True, user=True)
                session.cookies.set("sessionid", COOKIE)
        elif path == "/accounts/login/ajax/two_factor/":
            if self.otp == "bad":
                data.update(status="fail", message="synthetic-private-error")
            else:
                if self.otp != "no_sid":session.cookies.set("sessionid", COOKIE)
                if self.otp == "feedback":
                    data.update(message="feedback_required")
                elif self.otp == "denied":
                    data.update(authenticated=False)
        elif path == "/graphql/query":
            if self.before_verify:
                self.saw_before_verify = self.before_verify()
            verify = self.verify.pop(0) if isinstance(self.verify, list) else self.verify
            if verify == "timeout":
                raise requests.Timeout("synthetic-private-url")
            if verify == "challenge":
                response.status_code = 400
                data.update(status="fail", message="challenge_required", checkpoint_url=CHECKPOINT)
            elif verify == "rate":
                response.status_code = 429
                data.update(status="fail", message="synthetic-private-rate")
            elif verify in {"feedback", "feedback_challenge", "feedback_expired", "feedback_denied"}:
                response.status_code = 400
                data.update(status="fail", message="feedback_required")
                if verify == "feedback_challenge":data.update(challenge={"url":CHECKPOINT})
                elif verify == "feedback_expired":data.update(data={"user":None})
                elif verify == "feedback_denied":data.update(authenticated=False)
            elif verify == "login_required":
                response.status_code = 401
                data.update(status="fail", message="login_required")
            elif verify == "login_redirect":
                response.status_code = 302
                response.headers["Location"] = "https://www.instagram.com/accounts/login/"
            elif verify == "unknown":
                data["data"] = {}
            elif verify in {"matched_challenge", "matched_denied"}:
                data["data"]={"user":{"username":ACCOUNT["username"]}}
                if verify=="matched_challenge":data["challenge"]={"url":CHECKPOINT}
                else:data["authenticated"]=False
            else:
                data["data"] = {"user": {"username": "synthetic-other-user" if verify == "other" else ACCOUNT["username"].upper()} if verify not in {"none", "expired"} else None}
        response._content = json.dumps(data).encode()
        return response


def client(store):
    return InstagramApiClient(store, totp_factory=lambda _secret: "123456")


def test_success_persists_before_verification_and_reuses_session_without_credentials(tmp_path, monkeypatch, capsys):
    vault, store, state, request_id = setup(tmp_path)
    transport = Transport()
    transport.before_verify = lambda: store.load(ACCOUNT["username"])
    transport.install(monkeypatch)
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.state == "ready"
    assert transport.saw_before_verify.pending_stage == "api_authenticated"
    assert transport.saw_before_verify.cookies["sessionid"] == COOKIE
    assert transport.saw_before_verify.totp_attempted
    assert store.load(ACCOUNT["username"]).pending_stage == "ready"
    assert state.consume_login_attempt(request_id) is False and state.consume_totp_attempt(request_id) is False
    assert sum(path.endswith("login/ajax/") for _, path in transport.calls) == 1
    assert sum(path.endswith("ajax/two_factor/") for _, path in transport.calls) == 1
    before = list(transport.calls)
    assert client(store).refresh(ACCOUNT, request_id+1, state).state == "ready"
    assert transport.calls[len(before):] == [("GET", "/graphql/query")]
    assert all(cookie["domain"] == ".instagram.com" and cookie["secure"] for cookie in result.cookies)
    text = capsys.readouterr()
    assert not text.out and not text.err
    for secret in (*ACCOUNT.values(), COOKIE, IDENTIFIER, CHECKPOINT, "123456"):
        assert secret not in repr(result) and secret.encode() not in (vault.directory/store._FILE).read_bytes()


@pytest.mark.parametrize("verify,reason", [("challenge", "challenge_required"), ("timeout", "network_error"), ("rate", "rate_limited"), ("none", "verification_required")])
def test_accepted_two_factor_session_survives_failed_verify_then_resumes_without_password(tmp_path, monkeypatch, verify, reason):
    _, store, state, request_id = setup(tmp_path)
    transport = Transport(verify=verify)
    transport.install(monkeypatch)
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.state == "needs_attention" and result.reason == reason
    pending = store.load(ACCOUNT["username"])
    assert pending.cookies["sessionid"] == COOKIE and pending.totp_attempted
    if verify == "challenge":
        assert pending.checkpoint_url == CHECKPOINT and result.checkpoint_url == CHECKPOINT
    previous = len(transport.calls)
    transport.verify = "ready"
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.state == "ready"
    assert transport.calls[previous:] == [("GET", "/graphql/query")]


@pytest.mark.parametrize("two_factor",[False,True])
def test_feedback_only_on_graphql_accepts_proven_fresh_sdk_session(tmp_path,monkeypatch,two_factor):
    _,store,state,request_id=setup(tmp_path)
    transport=Transport(two_factor=two_factor,verify="feedback")
    transport.before_verify=lambda:store.load(ACCOUNT["username"])
    transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert transport.saw_before_verify.pending_stage=="api_authenticated"
    assert result.state=="ready" and result.reason=="session_ready"
    assert any(cookie["name"]=="sessionid" and cookie["value"]==COOKIE for cookie in result.cookies)
    assert store.load(ACCOUNT["username"]).pending_stage=="ready"
    assert sum(path.endswith("login/ajax/") for _,path in transport.calls)==1
    assert sum(path.endswith("ajax/two_factor/") for _,path in transport.calls)==int(two_factor)


@pytest.mark.parametrize("stage",["ready","api_authenticated"])
def test_feedback_only_resumes_durably_proven_session_without_auth_posts(tmp_path,monkeypatch,stage):
    _,store,state,request_id=setup(tmp_path)
    store.save(ACCOUNT["username"],cookies={"sessionid":COOKIE,"csrftoken":"synthetic-csrf"},pending_stage=stage)
    transport=Transport(verify="feedback");transport.install(monkeypatch)
    assert client(store).refresh(ACCOUNT,request_id,state).state=="ready"
    assert transport.calls==[("GET","/graphql/query")]
    assert state.consume_login_attempt(request_id)


@pytest.mark.parametrize("stage",["login_pending","verification","checkpoint","blocked"])
def test_unproven_feedback_is_static_error_and_never_promoted(tmp_path,monkeypatch,stage):
    _,store,state,request_id=setup(tmp_path)
    store.save(ACCOUNT["username"],cookies={"sessionid":COOKIE,"csrftoken":"synthetic-csrf"},pending_stage=stage)
    transport=Transport(verify="feedback");transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert result.state=="error" and result.reason=="feedback_required"
    assert store.load(ACCOUNT["username"]).pending_stage not in {"ready","api_authenticated"}
    assert transport.calls==[("GET","/graphql/query")]


@pytest.mark.parametrize("login,otp,reason",[("feedback","ready","feedback_required"),("ready","feedback","feedback_required"),("ready","denied","authentication_rejected")])
def test_auth_post_feedback_or_denied_authentication_never_creates_provenance(tmp_path,monkeypatch,login,otp,reason):
    _,store,state,request_id=setup(tmp_path)
    transport=Transport(login=login,otp=otp,verify="feedback");transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert result.state=="error" and result.reason==reason
    assert store.load(ACCOUNT["username"]).pending_stage not in {"ready","api_authenticated"}
    assert not any(path=="/graphql/query" for _,path in transport.calls)


def test_sdk_return_without_nonempty_sessionid_never_creates_login_provenance(tmp_path,monkeypatch):
    _,store,state,request_id=setup(tmp_path)
    transport=Transport(otp="no_sid",verify="feedback");transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert result.state=="needs_attention"
    assert store.load(ACCOUNT["username"]).pending_stage=="verification"
    assert not any(path=="/graphql/query" for _,path in transport.calls)


def test_real_challenge_cannot_be_promoted_by_simultaneous_feedback(tmp_path,monkeypatch):
    _,store,state,request_id=setup(tmp_path)
    transport=Transport(verify="feedback_challenge");transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert result.state=="needs_attention" and result.reason=="challenge_required"
    assert result.checkpoint_url==CHECKPOINT
    assert store.load(ACCOUNT["username"]).pending_stage=="checkpoint"


def test_feedback_with_explicit_authenticated_false_is_never_promoted(tmp_path,monkeypatch):
    _,store,state,request_id=setup(tmp_path)
    transport=Transport(verify="feedback_denied");transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert result.state=="error" and result.reason=="authentication_rejected"
    assert store.load(ACCOUNT["username"]).pending_stage=="blocked"


@pytest.mark.parametrize("cached",[False,True])
@pytest.mark.parametrize("verify,reason",[("matched_challenge","challenge_required"),("matched_denied","authentication_rejected")])
def test_matching_identity_never_overrides_explicit_challenge_or_authentication_denial(tmp_path,monkeypatch,cached,verify,reason):
    _,store,state,request_id=setup(tmp_path)
    if cached:store.save(ACCOUNT["username"],cookies={"sessionid":COOKIE,"csrftoken":"synthetic-csrf"},pending_stage="ready")
    transport=Transport(verify=verify);transport.install(monkeypatch)
    result=client(store).refresh(ACCOUNT,request_id,state)
    assert result.state!="ready" and result.reason==reason
    assert store.load(ACCOUNT["username"]).pending_stage==("checkpoint" if verify=="matched_challenge" else "blocked")
    assert sum(path.endswith("login/ajax/") for _,path in transport.calls)==int(not cached)


def test_browser_session_with_changed_identity_token_does_not_inherit_provenance(tmp_path,monkeypatch):
    _,store,_,_=setup(tmp_path)
    store.save(ACCOUNT["username"],cookies={"sessionid":"synthetic-old-session","csrftoken":"synthetic-csrf"},pending_stage="ready")
    transport=Transport(verify="feedback");transport.install(monkeypatch)
    result=client(store).accept_browser_session(ACCOUNT,{"sessionid":COOKIE,"csrftoken":"synthetic-csrf"})
    assert result.state=="error" and result.reason=="feedback_required"
    assert store.load(ACCOUNT["username"]).pending_stage=="verification"


@pytest.mark.parametrize("expired",["expired","login_required","login_redirect"])
def test_only_previously_verified_explicit_expiry_allows_one_new_password_and_totp(tmp_path, monkeypatch, expired):
    _, store, state, request_id = setup(tmp_path)
    store.save(ACCOUNT["username"], cookies={"sessionid": COOKIE, "csrftoken": "synthetic-csrf"}, pending_stage="ready", totp_attempted=True)
    transport = Transport(verify=[expired,"ready"])
    transport.install(monkeypatch)
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.state == "ready"
    assert sum(path.endswith("login/ajax/") for _,path in transport.calls) == 1
    assert sum(path.endswith("ajax/two_factor/") for _,path in transport.calls) == 1
    assert not state.consume_login_attempt(request_id) and not state.consume_totp_attempt(request_id)
    assert store.load(ACCOUNT["username"]).pending_stage == "ready"


@pytest.mark.parametrize("failure",["timeout","rate","challenge","unknown"])
def test_previously_ready_transient_failure_preserves_provenance_without_password(tmp_path, monkeypatch, failure):
    _, store, state, request_id = setup(tmp_path)
    store.save(ACCOUNT["username"], cookies={"sessionid": COOKIE, "csrftoken": "synthetic-csrf"}, pending_stage="ready")
    transport = Transport(verify=failure);transport.install(monkeypatch)
    result = client(store).refresh(ACCOUNT,request_id,state)
    assert result.state == "needs_attention"
    assert transport.calls == [("GET","/graphql/query")]
    assert store.load(ACCOUNT["username"]).pending_stage == ("checkpoint" if failure=="challenge" else "ready")
    transport.verify="ready"
    assert client(store).refresh(ACCOUNT,request_id,state).state == "ready"
    assert transport.calls == [("GET","/graphql/query"),("GET","/graphql/query")]
    assert state.consume_login_attempt(request_id)  # No password budget spent.


@pytest.mark.parametrize("stage",["verification","checkpoint","blocked"])
def test_pending_explicitly_anonymous_session_never_authorizes_a_new_password(tmp_path,monkeypatch,stage):
    _, store, state, request_id = setup(tmp_path)
    store.save(ACCOUNT["username"],cookies={"sessionid":COOKIE,"csrftoken":"synthetic-csrf"},pending_stage=stage)
    transport=Transport(verify="expired");transport.install(monkeypatch)
    assert client(store).refresh(ACCOUNT,request_id,state).state=="needs_attention"
    assert transport.calls==[("GET","/graphql/query")]
    assert state.consume_login_attempt(request_id)


def test_crash_after_password_reservation_leaves_uncertainty_and_prevents_new_login(tmp_path,monkeypatch):
    _,store,state,request_id=setup(tmp_path)
    class SimulatedCrash(BaseException):pass
    transport=Transport();transport.install(monkeypatch)
    original=transport.send
    def crash(session,request,**kwargs):
        if urlsplit(request.url).path.endswith("login/ajax/"):raise SimulatedCrash()
        return original(session,request,**kwargs)
    transport.send=crash
    with pytest.raises(SimulatedCrash):client(store).refresh(ACCOUNT,request_id,state)
    assert store.load(ACCOUNT["username"]).pending_stage=="login_pending"
    assert not state.consume_login_attempt(request_id)
    before=list(transport.calls)
    assert client(store).refresh(ACCOUNT,request_id+1,state).state=="needs_attention"
    assert transport.calls==before


def test_crash_after_totp_reservation_preserves_durable_no_repeat_budget(tmp_path,monkeypatch):
    _,store,state,request_id=setup(tmp_path)
    class SimulatedCrash(BaseException):pass
    transport=Transport();transport.install(monkeypatch)
    def crash(_):raise SimulatedCrash()
    with pytest.raises(SimulatedCrash):InstagramApiClient(store,totp_factory=crash).refresh(ACCOUNT,request_id,state)
    assert store.load(ACCOUNT["username"]).totp_attempted
    assert not state.consume_totp_attempt(request_id)
    before=list(transport.calls)
    assert client(store).refresh(ACCOUNT,request_id+1,state).state=="needs_attention"
    assert transport.calls==before


def test_two_factor_pending_without_seed_can_resume_once_after_seed_is_added(tmp_path, monkeypatch):
    _, store, state, request_id = setup(tmp_path)
    transport = Transport()
    transport.install(monkeypatch)
    no_seed = {key: value for key, value in ACCOUNT.items() if key != "totp_secret"}
    result = client(store).refresh(no_seed, request_id, state)
    assert result.state == "needs_attention" and result.reason == "two_factor_required"
    pending = store.load(ACCOUNT["username"])
    assert pending.pending_stage == "two_factor" and pending.two_factor_identifier == IDENTIFIER
    assert not pending.totp_attempted
    previous = len(transport.calls)
    assert client(store).refresh(ACCOUNT, request_id, state).state == "ready"
    assert transport.calls[previous:] == [("POST", "/accounts/login/ajax/two_factor/"), ("GET", "/graphql/query")]


def test_failed_two_factor_is_never_retried_even_with_new_request(tmp_path, monkeypatch):
    _, store, state, request_id = setup(tmp_path)
    transport = Transport(otp="bad")
    transport.install(monkeypatch)
    assert client(store).refresh(ACCOUNT, request_id, state).reason == "two_factor_rejected"
    before = list(transport.calls)
    result = client(store).refresh(ACCOUNT, request_id+1, state)
    assert result.state == "needs_attention"
    assert transport.calls == before
    assert store.load(ACCOUNT["username"]).totp_attempted


@pytest.mark.parametrize("failure", ["timeout", "challenge", "bad"])
def test_uncertain_or_rejected_password_never_repeats_after_restart(tmp_path, monkeypatch, failure):
    _, store, state, request_id = setup(tmp_path)
    transport = Transport(login=failure)
    transport.install(monkeypatch)
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.state in {"needs_attention", "error"}
    assert store.load(ACCOUNT["username"]) is not None
    before = list(transport.calls)
    result = client(store).refresh(ACCOUNT, request_id+1, state)
    assert result.state == "needs_attention"
    assert transport.calls == before


def test_durable_budget_refusal_and_cancel_never_send_credentials(tmp_path, monkeypatch):
    _, store, state, request_id = setup(tmp_path)
    transport = Transport()
    transport.install(monkeypatch)
    state.consume_login_attempt(request_id)
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.reason == "login_budget_exhausted" and transport.calls == []
    stop = threading.Event();stop.set()
    assert client(store).refresh(ACCOUNT, request_id, state, stop_event=stop).reason == "cancelled"
    assert transport.calls == []


def test_human_browser_session_is_account_verified_and_never_causes_password_login(tmp_path, monkeypatch):
    _, store, _, _ = setup(tmp_path)
    transport = Transport()
    transport.install(monkeypatch)
    records = [{"name": "sessionid", "value": COOKIE, "domain": ".instagram.com", "expires": -1},
        {"name": "csrftoken", "value": "synthetic-csrf", "domain": "www.instagram.com", "expires": -1},
        {"name": "other", "value": "synthetic-foreign", "domain": "example.invalid"}]
    result = client(store).accept_browser_session(ACCOUNT, records)
    assert result.state == "ready" and "other" not in store.load(ACCOUNT["username"]).cookies
    assert transport.calls == [("GET", "/graphql/query")]
    transport.verify = "other"
    assert client(store).accept_browser_session(ACCOUNT, records).reason == "identity_mismatch"


def test_store_roundtrip_account_guard_clear_and_size_bounds(tmp_path):
    vault, store, _, _ = setup(tmp_path)
    account_before = (vault.directory/vault._ACCOUNT).read_bytes()
    store.save(ACCOUNT["username"], cookies={"sessionid": COOKIE}, pending_stage="checkpoint", checkpoint_url=CHECKPOINT, two_factor_identifier=IDENTIFIER, request_id=3, totp_attempted=True)
    saved = store.load(ACCOUNT["username"].upper())
    assert saved.cookies == {"sessionid": COOKIE} and saved.checkpoint_url == CHECKPOINT
    with pytest.raises(InstagramApiStorageError):
        store.load("synthetic-other-account")
    with pytest.raises(InstagramApiStorageError):
        store.save(ACCOUNT["username"], cookies={"a": "x"*8000, "b": "y"*8000}, pending_stage="ready")
    assert (vault.directory/vault._ACCOUNT).read_bytes() == account_before
    if os.name == "posix":
        assert (vault.directory/store._FILE).stat().st_mode & 0o777 == 0o600
    store.clear();assert store.load(ACCOUNT["username"]) is None
    assert (vault.directory/vault._ACCOUNT).read_bytes() == account_before


def test_corrupt_session_fails_closed_without_fresh_password(tmp_path, monkeypatch):
    vault, store, state, request_id = setup(tmp_path)
    store.save(ACCOUNT["username"], cookies={"sessionid": COOKIE}, pending_stage="ready")
    path = vault.directory/store._FILE
    path.write_bytes(path.read_bytes()[:-1]+b"x")
    transport = Transport();transport.install(monkeypatch)
    result = client(store).refresh(ACCOUNT, request_id, state)
    assert result.state == "error" and result.reason == "storage_error" and transport.calls == []


@pytest.mark.parametrize("value", ["https://example.invalid/challenge/a", "https://instagram.com.evil.invalid/challenge/a", "http://instagram.com/challenge/a", "https://a:b@instagram.com/challenge/a", "https://instagram.com:444/challenge/a", "//example.invalid/challenge/a", "https://instagram.com/accounts/password/reset/", "https://instagram.com/accounts/emailsignup/", "https://instagram.com/challenge/a#fragment", "https://instagram.com/challenge/../accounts/password/reset/", "https://instagram.com/challenge/%2e%2e/accounts/password/reset/", "https://instagram.com/challenge/%5caccount", "https://instagram.com/challenge/%00"])
def test_checkpoint_urls_are_instagram_confirmation_only(value):
    assert safe_checkpoint_url(value) is None
    assert safe_checkpoint_url("/challenge/synthetic/") == "https://www.instagram.com/challenge/synthetic/"


@pytest.mark.parametrize("method,url", [("GET", "http://instagram.com/"), ("GET", "https://instagram.com.evil.invalid/"), ("GET", "https://a:b@instagram.com/"), ("DELETE", "https://instagram.com/"), ("GET", "https://instagram.com:444/")])
def test_global_transport_guard_rejects_foreign_schemes_domains_and_methods(monkeypatch, method, url):
    def transport(*_args, **_kwargs):
        raise AssertionError("Network transport must not run")
    monkeypatch.setattr(requests.sessions.Session, "send", transport)
    with _NetworkGuard(None, 60, __import__("time").monotonic):
        with pytest.raises(_ApiStop):
            requests.Session().send(requests.Request(method, url).prepare())


def test_guard_covers_new_sessions_redirects_and_repeated_auth_posts(monkeypatch):
    calls = []
    def transport(session, request, **kwargs):
        calls.append(urlsplit(request.url).path)
        if request.url.endswith("/redirect"):
            return requests.Session().send(requests.Request("GET", "https://example.invalid/").prepare())
        response = requests.Response();response.status_code=200;response.headers={};return response
    monkeypatch.setattr(requests.sessions.Session, "send", transport)
    with _NetworkGuard(None, 60, __import__("time").monotonic):
        with pytest.raises(_ApiStop):
            requests.Session().send(requests.Request("GET", "https://instagram.com/redirect").prepare())
        url = "https://www.instagram.com/api/v1/web/accounts/login/ajax/"
        requests.Session().send(requests.Request("POST", url).prepare())
        with pytest.raises(_ApiStop):
            requests.Session().send(requests.Request("POST", url).prepare())
    assert calls == ["/redirect", "/api/v1/web/accounts/login/ajax/"]


def test_guard_budget_and_deadline_stop_without_a_second_request(monkeypatch):
    calls = []
    clock = [0.0]
    def transport(*_args, **_kwargs):
        calls.append(True);clock[0]=80
        response=requests.Response();response.status_code=200;response.headers={};return response
    monkeypatch.setattr(requests.sessions.Session, "send", transport)
    with _NetworkGuard(None, 60, lambda: clock[0]):
        with pytest.raises(_ApiStop):
            requests.Session().send(requests.Request("GET", "https://www.instagram.com/").prepare())
    assert len(calls) == 1


def test_result_and_errors_never_expose_secret_fields_in_repr():
    result = ApiResult("needs_attention", "challenge_required", [{"value": COOKIE}], CHECKPOINT, "synthetic-private-agent")
    for secret in (COOKIE, CHECKPOINT, "synthetic-private-agent"):
        assert secret not in repr(result)
    assert COOKIE not in str(InstagramApiStorageError())
