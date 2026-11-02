import asyncio
import json

from fastapi.testclient import TestClient
from fastapi import HTTPException
import httpx
import pytest

from vela.api import create_app
from vela.browser_client import BrowserClient, public_status
from vela.config import Settings


STATUS = dict(configured=True, state="needs_attention", message="PRIVATE-DO-NOT-RETURN",
              session_generation=3, request_id=4, completed_request_id=3,
              interaction_id="a" * 32, browser_available=True, updated_at=None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE", "1")
    app = create_app(Settings(state_dir=tmp_path, base_url="http://testserver"))
    calls = []

    class Fake:
        async def request(self, method, path, payload=None, *, screen=False):
            calls.append((method, path, payload, screen))
            return b"\x89PNG\r\n\x1a\n" if screen else public_status(STATUS)

    app.state.browser_client = Fake()
    with TestClient(app) as result:
        result.headers["Origin"] = "http://testserver"
        yield result, calls


def test_credentials_are_forwarded_only_to_fixed_browser_path_and_never_returned(client):
    api, calls = client
    body = {"username": "synthetic_user", "password": "SYNTHETIC-SECRET"}
    response = api.put("/api/instagram-account", json=body)
    assert response.status_code == 200
    assert calls == [("PUT", "/account", body, False)]
    assert "SYNTHETIC" not in response.text and "PRIVATE" not in response.text
    assert "username" not in response.json() and "password" not in response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [{"username": "x"}, {"username": "x", "password": ""},
    {"username": "x\n", "password": "x"}, {"username": [], "password": "x"},
    {"username": "x", "password": "x", "url": "http://127.0.0.1"}])
def test_bad_credentials_do_not_reach_browser_or_echo_values(client, body):
    api, calls = client
    response = api.put("/api/instagram-account", json=body)
    assert response.status_code == 422 and calls == []
    assert "127.0.0.1" not in response.text


def test_input_limit_and_origin_are_enforced_before_forwarding(client):
    api, calls = client
    assert api.put("/api/instagram-account", content=b"x" * 16385, headers={"Content-Type": "application/json"}).status_code == 413
    assert api.put("/api/instagram-account", json={"username": "x", "password": "x"}, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert api.post("/api/instagram-account/login", json={"url": "https://foreign.invalid"}).status_code == 422
    assert calls == []


@pytest.mark.parametrize("action", [
    {"kind": []}, {"kind": "click", "x": 1.1, "y": 0},
    {"kind": "click", "x": True, "y": 0}, {"kind": "text", "text": "x" * 4097},
    {"kind": "key", "key": "Control+l"}, {"kind": "key", "key": []},
    {"kind": "scroll", "delta": 1001}, {"kind": "navigate", "url": "https://example.com"},
])
def test_only_finite_browser_interactions_allowed(client, action):
    api, calls = client
    response = api.post("/api/instagram-account/action", json={"interaction_id": "a" * 32, **action})
    assert response.status_code == 422 and calls == []


def test_controls_and_screen_keep_protocol_and_no_cache(client):
    api, calls = client
    action = {"interaction_id": "a" * 32, "kind": "click", "x": .5, "y": .75}
    assert api.post("/api/instagram-account/action", json=action).status_code == 200
    assert calls[-1] == ("POST", "/action", action, False)
    response = api.get("/api/instagram-account/screen")
    assert response.content.startswith(b"\x89PNG")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "image/png"
    assert api.post("/api/instagram-account/login", json={}).status_code == 200
    assert api.delete("/api/instagram-account").status_code == 200


def test_browser_client_strips_identity_and_errors(monkeypatch, tmp_path):
    payload = {**STATUS, "password": "NEVER-RETURN", "cookies": ["NEVER-RETURN"]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: transport)
    result = asyncio.run(BrowserClient(tmp_path / "control.sock").request("GET", "/status"))
    assert "NEVER" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="NEVER-RETURN"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(BrowserClient(tmp_path / "control.sock").request("POST", "/login", {}))
    assert error.value.status_code == 503 and "NEVER" not in error.value.detail


def test_totp_secret_only_reaches_fixed_socket_route_and_never_returns(client):
    api, calls = client
    body = {"secret": "SYNTHETIC-TOTP-SECRET"}
    response = api.put("/api/instagram-account/totp", json=body)
    assert response.status_code == 200
    assert calls == [("PUT", "/totp", body, False)]
    assert "SYNTHETIC" not in response.text and "secret" not in response.json()
    assert response.json()["totp_configured"] is False
    assert response.headers["cache-control"] == "no-store"
    assert api.delete("/api/instagram-account/totp").status_code == 200
    assert calls[-1] == ("DELETE", "/totp", None, False)
    assert len(calls) == 2  # Neither operation logs in or deletes the account.


@pytest.mark.parametrize("body", [{}, {"secret": ""}, {"secret": []}, {"secret": True},
    {"secret": "x" * 4097}, {"secret": "PRIVATE", "username": "PRIVATE"}])
def test_invalid_totp_inputs_never_forward_or_echo(client, body):
    api, calls = client
    response = api.put("/api/instagram-account/totp", json=body)
    assert response.status_code == 422 and calls == []
    assert "PRIVATE" not in response.text


def test_totp_mutations_require_same_origin(client):
    api, calls = client
    foreign = {"Origin": "https://foreign.invalid"}
    assert api.put("/api/instagram-account/totp", json={"secret": "PRIVATE"}, headers=foreign).status_code == 403
    assert api.delete("/api/instagram-account/totp", headers=foreign).status_code == 403
    assert calls == []


def test_totp_status_is_strict_boolean_and_socket_path_is_finite(monkeypatch, tmp_path):
    assert public_status({**STATUS, "totp_configured": True})["totp_configured"] is True
    assert public_status({**STATUS, "totp_configured": "PRIVATE"})["totp_configured"] is False
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={**STATUS, "totp_configured": True, "secret": "PRIVATE"}))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: transport)
    result = asyncio.run(BrowserClient(tmp_path / "control.sock").request("PUT", "/totp", {"secret": "PRIVATE"}))
    assert result["totp_configured"] is True and "PRIVATE" not in json.dumps(result)
    with pytest.raises(ValueError):
        asyncio.run(BrowserClient(tmp_path / "control.sock").request("PUT", "/totp/anything", {}))
