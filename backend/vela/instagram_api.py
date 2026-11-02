"""Bounded Instagram API authentication with encrypted, resumable continuation.

The Instaloader 4.16 login/session contract is reviewed against its official
instaloadercontext.py. No pickle/session-file helpers or interactive setup APIs
are used. Every requests session, including redirects, passes the same guard.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import http.client
import logging
import threading
import time
from urllib.parse import urlsplit

import instaloader
import requests

from .instagram_api_store import (InstagramApiSessionStore, InstagramApiStorageError,
    StoredInstagramApiSession, safe_checkpoint_url, validate_cookie_dict)
from .totp import fresh_totp


_SEND_LOCK = threading.Lock()
_STATES = frozenset({"ready", "needs_attention", "error"})


@dataclass(frozen=True, repr=False)
class ApiResult:
    state: str
    reason: str
    cookies: list[dict] = field(default_factory=list, repr=False)
    checkpoint_url: str | None = field(default=None, repr=False)
    user_agent: str = field(default="", repr=False)

    def __post_init__(self):
        if self.state not in _STATES:
            raise ValueError("Invalid Instagram API state")


class _ApiStop(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Instagram API operation stopped.")


def _cookie_records(cookies):
    return [{"name": name, "value": value, "domain": ".instagram.com", "path": "/",
        "secure": True, "httpOnly": name == "sessionid"} for name, value in validate_cookie_dict(cookies).items()]


def _browser_cookies(cookies):
    if isinstance(cookies, dict):
        return validate_cookie_dict(cookies)
    if not isinstance(cookies, list) or len(cookies) > 256:
        raise InstagramApiStorageError()
    result = {}
    for item in cookies:
        if not isinstance(item, dict):
            raise InstagramApiStorageError()
        domain = item.get("domain", "")
        if not isinstance(domain, str):
            raise InstagramApiStorageError()
        host = domain.lstrip(".").casefold()
        if not (host == "instagram.com" or host.endswith(".instagram.com")):
            continue
        expiry = item.get("expires", -1)
        if not isinstance(expiry, (int, float)):
            raise InstagramApiStorageError()
        if expiry > 0 and expiry <= time.time():
            continue
        name, value = item.get("name"), item.get("value")
        if name in result and result[name] != value:
            raise InstagramApiStorageError()
        result[name] = value
    return validate_cookie_dict(result)


class _NoRetryRateController:
    def __init__(self, _context):
        pass

    def wait_before_query(self, *_args, **_kwargs):
        pass

    def handle_429(self, *_args, **_kwargs):
        raise _ApiStop("rate_limited")


class _NetworkGuard:
    def __init__(self, stop_event, timeout, clock):
        self.stop_event, self.clock = stop_event, clock
        self.deadline = clock() + timeout
        self.phase = "login"
        self.requests = self.password_posts = self.totp_posts = 0
        self.last_cookies = {}
        self.checkpoint_url = None
        self.challenge = self.rate_limited = self.network_failed = False
        self.explicit_expired = False
        self.feedback = self.auth_rejected = False
        self.auth_false_phase = "none"
        self.auth_false_http_status = 0
        self.auth_false_status_class = "none"
        self.auth_false_identifier_valid = False
        self.legit_two_factor_requested = self.sdk_normal_returned = False

    def check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise _ApiStop("cancelled")
        if self.clock() >= self.deadline:
            raise _ApiStop("timeout")

    def __enter__(self):
        self.check()
        if not _SEND_LOCK.acquire(blocking=False):
            raise _ApiStop("api_busy")
        self.original_send = requests.sessions.Session.send
        self.debuglevel = http.client.HTTPConnection.debuglevel
        http.client.HTTPConnection.debuglevel = 0
        self.loggers = [(logging.getLogger(name), logging.getLogger(name).disabled) for name in ("instaloader", "requests", "urllib3", "urllib3.connectionpool", "http.client")]
        for logger, _ in self.loggers:
            logger.disabled = True
        guard = self

        def send(session, request, **kwargs):
            guard.check()
            try:
                parsed = urlsplit(request.url)
                host = parsed.hostname or ""
                if parsed.scheme != "https" or not (host == "instagram.com" or host.endswith(".instagram.com")) or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or request.method not in {"GET", "POST"} or guard.requests >= 16:
                    raise _ApiStop("network_policy_blocked")
                if request.method == "POST":
                    if guard.phase == "login" and parsed.path == "/api/v1/web/accounts/login/ajax/" and guard.password_posts == 0:
                        guard.password_posts += 1
                    elif guard.phase == "totp" and parsed.path == "/accounts/login/ajax/two_factor/" and guard.totp_posts == 0:
                        guard.totp_posts += 1
                    elif guard.phase != "verify" or parsed.path not in {"/graphql/query", "/graphql/query/", "/api/graphql", "/api/graphql/"}:
                        raise _ApiStop("network_policy_blocked")
            except (ValueError, TypeError):
                raise _ApiStop("network_policy_blocked") from None
            guard.requests += 1
            session.trust_env = False
            session.max_redirects = 3
            request.headers["Host"] = host
            kwargs.update(timeout=min(15, max(.1, guard.deadline-guard.clock())), verify=True, proxies={})
            try:
                response = guard.original_send(session, request, **kwargs)
            except requests.exceptions.RequestException:
                guard.network_failed = True
                raise
            with suppress(Exception):
                guard.last_cookies = validate_cookie_dict(requests.utils.dict_from_cookiejar(session.cookies))
            if response.status_code == 429:
                guard.rate_limited = True
                raise _ApiStop("rate_limited")
            if guard.phase == "verify" and response.status_code in {301, 302, 303, 307, 308}:
                with suppress(Exception):
                    destination = response.headers.get("Location", "")
                    if destination.startswith("/") and not destination.startswith("//"):
                        destination = "https://www.instagram.com"+destination
                    redirect = urlsplit(destination)
                    hostname = redirect.hostname or ""
                    if redirect.scheme == "https" and (hostname == "instagram.com" or hostname.endswith(".instagram.com")) and redirect.username is None and redirect.password is None and redirect.port in (None, 443) and redirect.path.rstrip("/") == "/accounts/login":
                        guard.explicit_expired = True
            if "application/json" in response.headers.get("Content-Type", ""):
                with suppress(Exception):
                    if len(response.content) <= 262144:
                        data = response.json()
                        if isinstance(data, dict):
                            challenge = data.get("challenge")
                            candidate = data.get("checkpoint_url") or data.get("challenge_url")
                            if not candidate and isinstance(challenge, dict):
                                candidate = challenge.get("url") or challenge.get("api_path")
                            guard.checkpoint_url = safe_checkpoint_url(candidate) or guard.checkpoint_url
                            guard.challenge |= bool(challenge or data.get("checkpoint_url") or data.get("challenge_url") or guard.checkpoint_url) or data.get("message") in {"challenge_required", "checkpoint_required"}
                            guard.feedback |= data.get("message") == "feedback_required"
                            two_factor_info = data.get("two_factor_info")
                            identifier = two_factor_info.get("two_factor_identifier") if isinstance(two_factor_info, dict) else None
                            # Instaloader handles this pending first step before
                            # authenticated:false; authentication completes only
                            # after a separate successful TOTP response.
                            identifier_valid = isinstance(identifier, str) and 1 <= len(identifier) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in identifier)
                            password_two_factor = guard.phase == "login" and request.method == "POST" and parsed.path == "/api/v1/web/accounts/login/ajax/" and data.get("two_factor_required") is True
                            pending_two_factor = password_two_factor and identifier_valid and not (guard.challenge or guard.feedback)
                            guard.legit_two_factor_requested |= pending_two_factor
                            if data.get("authenticated") is False and not pending_two_factor and guard.auth_false_phase == "none":
                                guard.auth_false_phase = guard.phase
                                guard.auth_false_http_status = response.status_code if type(response.status_code) is int and 100 <= response.status_code <= 599 else 0
                                wire_status = data.get("status")
                                guard.auth_false_status_class = "ok" if wire_status == "ok" else "missing" if wire_status is None else "non_ok" if isinstance(wire_status, str) else "invalid_type"
                                guard.auth_false_identifier_valid = identifier_valid
                            guard.auth_rejected |= data.get("authenticated") is False and not pending_two_factor
                            guard.auth_rejected |= password_two_factor and not pending_two_factor
                            if guard.phase in {"login", "totp"}:
                                guard.auth_rejected |= guard.challenge or guard.feedback
                            if guard.phase == "verify":
                                user_data = data.get("data")
                                guard.explicit_expired |= data.get("message") == "login_required" or (response.status_code == 200 and data.get("status") in {None, "ok"} and not data.get("errors") and isinstance(user_data, dict) and "user" in user_data and user_data["user"] is None)
            guard.check()
            return response

        requests.sessions.Session.send = send
        return self

    def __exit__(self, *_):
        requests.sessions.Session.send = self.original_send
        http.client.HTTPConnection.debuglevel = self.debuglevel
        for logger, disabled in self.loggers:
            logger.disabled = disabled
        _SEND_LOCK.release()


class InstagramApiClient:
    def __init__(self, store: InstagramApiSessionStore | None = None, *, loader_factory=None,
                 totp_factory=None, timeout=75, clock=time.monotonic):
        self.store = store
        self.loader_factory = loader_factory or instaloader.Instaloader
        self.totp_factory = totp_factory or (lambda secret: asyncio.run(fresh_totp(secret)))
        self.timeout, self.clock = max(1, min(float(timeout), 120)), clock

    def _loader(self):
        loader = self.loader_factory(quiet=True, sleep=False, max_connection_attempts=1,
            request_timeout=15, iphone_support=False, rate_controller=_NoRetryRateController)
        loader.context.log = lambda *_args, **_kwargs: None
        loader.context.error = lambda *_args, **_kwargs: None
        return loader

    @staticmethod
    def _warn_rejected(guard):
        if guard.auth_rejected:
            logging.getLogger(__name__).warning(
                "Instagram authentication rejected: auth_false_phase=%s legit_two_factor_requested=%s sdk_normal_returned=%s password_posts=%d totp_posts=%d http_status=%d status_class=%s identifier_valid=%s",
                guard.auth_false_phase, guard.legit_two_factor_requested, guard.sdk_normal_returned, guard.password_posts, guard.totp_posts,
                guard.auth_false_http_status, guard.auth_false_status_class, guard.auth_false_identifier_valid)

    @staticmethod
    def _snapshot(loader, guard, fallback=None):
        cookies = dict(fallback or {})
        with suppress(Exception):
            cookies.update(validate_cookie_dict(loader.context.save_session()))
        cookies.update(guard.last_cookies)
        return validate_cookie_dict(cookies)

    def _save(self, username, cookies, stage, request_id, attempted, user_agent, checkpoint=None, identifier=None):
        if self.store is None:
            raise InstagramApiStorageError()
        self.store.save(username, cookies=cookies, pending_stage=stage, checkpoint_url=checkpoint,
            two_factor_identifier=identifier, request_id=request_id, totp_attempted=attempted, user_agent=user_agent)

    @staticmethod
    def _result(state, reason, cookies=None, checkpoint=None, user_agent=""):
        return ApiResult(state, reason, _cookie_records(cookies or {}), safe_checkpoint_url(checkpoint), user_agent)

    @staticmethod
    def _account(account):
        if not isinstance(account, dict) or not isinstance(account.get("username"), str) or not account["username"] or not isinstance(account.get("password"), str) or not account["password"]:
            raise InstagramApiStorageError()
        return account["username"]

    def _verify(self, loader, guard, username, cookies, request_id, attempted, user_agent, checkpoint=None, was_ready=False, was_authenticated=False):
        provenance = "ready" if was_ready else "api_authenticated" if was_authenticated else "verification"
        self._save(username, cookies, provenance, request_id, attempted, user_agent, checkpoint)
        if not cookies.get("sessionid"):
            return self._result("needs_attention", "confirmation_required", cookies, checkpoint, user_agent)
        loader.context.user_agent = user_agent
        loader.load_session(username, cookies)
        guard.phase = "verify"
        guard.check()
        identity = loader.test_login()
        updated = self._snapshot(loader, guard, cookies)
        if isinstance(identity, str) and identity.casefold() == username.casefold() and not (guard.challenge or guard.checkpoint_url or guard.auth_rejected):
            self._save(username, updated, "ready", request_id, attempted, user_agent)
            return self._result("ready", "session_ready", updated, user_agent=user_agent)
        checkpoint = guard.checkpoint_url or checkpoint
        # GraphQL may reject a freshly authenticated download session with
        # feedback_required. This is usable only with durable login provenance;
        # it proves neither an unknown session nor a different browser account.
        feedback_only = guard.feedback and identity is None and not (guard.challenge or checkpoint or guard.rate_limited or guard.network_failed or guard.explicit_expired or guard.auth_rejected)
        if feedback_only and provenance in {"ready", "api_authenticated"}:
            self._save(username, updated, "ready", request_id, attempted, user_agent)
            return self._result("ready", "session_ready", updated, user_agent=user_agent)
        reason = "challenge_required" if guard.challenge or guard.checkpoint_url else "rate_limited" if guard.rate_limited else "network_error" if guard.network_failed else "authentication_rejected" if guard.auth_rejected else "identity_mismatch" if isinstance(identity, str) else "feedback_required" if guard.feedback else "session_expired" if was_ready and guard.explicit_expired else "verification_required"
        pending_stage = "checkpoint" if checkpoint or guard.challenge else "blocked" if reason in {"identity_mismatch", "authentication_rejected"} else "verification" if guard.explicit_expired and not was_ready else provenance
        self._save(username, updated, pending_stage, request_id, attempted, user_agent, checkpoint)
        return self._result("error" if reason in {"identity_mismatch", "feedback_required", "authentication_rejected"} else "needs_attention", reason, updated, checkpoint, user_agent)

    def refresh(self, account: dict, request_id: int, state_callbacks, *, stop_event=None) -> ApiResult:
        loader = None
        guard = _NetworkGuard(stop_event, self.timeout, self.clock)
        username = user_agent = ""
        saved = None
        cookies = {}
        identifier = checkpoint = None
        attempted = False
        stage = "login_pending"
        try:
            username = self._account(account)
            if type(request_id) is not int or request_id < 1 or self.store is None:
                raise InstagramApiStorageError()
            saved = self.store.load(username)
            loader = self._loader()
            user_agent = saved.user_agent if saved and saved.user_agent else loader.context.user_agent
            with guard:
                if saved:
                    cookies, checkpoint, identifier = saved.cookies, saved.checkpoint_url, saved.two_factor_identifier
                    attempted, stage = saved.totp_attempted, saved.pending_stage
                    if stage == "two_factor":
                        if not identifier or attempted or not account.get("totp_secret"):
                            return self._result("needs_attention", "two_factor_required", cookies, checkpoint, user_agent)
                        loader.context.user_agent = user_agent
                        loader.load_session(username, cookies)
                        loader.context.two_factor_auth_pending = (loader.context._session, username, identifier)
                    else:
                        result = self._verify(loader, guard, username, cookies, request_id, attempted, user_agent, checkpoint, was_ready=stage=="ready", was_authenticated=stage=="api_authenticated")
                        if stage != "ready" or result.reason != "session_expired":
                            return result
                        # Only a formerly verified session with an explicit
                        # anonymous response may spend a fresh request budget.
                        saved = None
                        cookies = {}
                        checkpoint = identifier = None
                        attempted = False
                        stage = "login_pending"
                        guard.phase = "login"
                        guard.last_cookies = {}
                        guard.explicit_expired = False
                if saved is None:
                    # Persist uncertainty before spending a password budget or
                    # sending credentials: a restart never repeats this login.
                    self._save(username, {}, stage, request_id, False, user_agent)
                    if not state_callbacks.consume_login_attempt(request_id):
                        return self._result("needs_attention", "login_budget_exhausted", user_agent=user_agent)
                    try:
                        loader.login(username, account["password"])
                    except instaloader.TwoFactorAuthRequiredException:
                        pending = loader.context.two_factor_auth_pending
                        if not pending or len(pending) != 3:
                            raise _ApiStop("two_factor_required") from None
                        cookies = validate_cookie_dict(requests.utils.dict_from_cookiejar(pending[0].cookies))
                        identifier = pending[2]
                        stage = "two_factor"
                        self._save(username, cookies, stage, request_id, False, user_agent, identifier=identifier)
                        if guard.auth_rejected:
                            raise _ApiStop("challenge_required" if guard.challenge else "feedback_required" if guard.feedback else "authentication_rejected")
                    else:
                        guard.sdk_normal_returned = True
                        cookies = self._snapshot(loader, guard)
                        if guard.auth_rejected:
                            raise _ApiStop("challenge_required" if guard.challenge else "feedback_required" if guard.feedback else "authentication_rejected")
                        stage = "api_authenticated" if cookies.get("sessionid") else "verification"
                        return self._verify(loader, guard, username, cookies, request_id, False, user_agent, was_authenticated=stage=="api_authenticated")
                if not account.get("totp_secret"):
                    return self._result("needs_attention", "two_factor_required", cookies, checkpoint, user_agent)
                if not state_callbacks.consume_totp_attempt(request_id):
                    return self._result("needs_attention", "totp_budget_exhausted", cookies, checkpoint, user_agent)
                attempted = True
                self._save(username, cookies, "two_factor", request_id, True, user_agent, checkpoint, identifier)
                guard.phase = "totp"
                guard.check()
                code = self.totp_factory(account["totp_secret"])
                try:
                    guard.check()
                    loader.two_factor_login(code)
                    guard.sdk_normal_returned = True
                finally:
                    del code
                cookies = self._snapshot(loader, guard, cookies)
                if guard.auth_rejected:
                    raise _ApiStop("challenge_required" if guard.challenge else "feedback_required" if guard.feedback else "authentication_rejected")
                stage = "api_authenticated" if cookies.get("sessionid") else "verification"
                return self._verify(loader, guard, username, cookies, request_id, attempted, user_agent, checkpoint, was_authenticated=stage=="api_authenticated")
        except InstagramApiStorageError:
            return self._result("error", "storage_error")
        except Exception as error:
            reason = error.reason if isinstance(error, _ApiStop) else "challenge_required" if guard.challenge else "feedback_required" if guard.feedback else "two_factor_rejected" if isinstance(error, instaloader.BadCredentialsException) and attempted else "credentials_rejected" if isinstance(error, instaloader.BadCredentialsException) else "rate_limited" if guard.rate_limited else "network_error" if guard.network_failed or isinstance(error, requests.exceptions.RequestException) else "api_error"
            if loader is not None and username:
                with suppress(Exception):
                    cookies = self._snapshot(loader, guard, cookies)
                    checkpoint = guard.checkpoint_url or checkpoint
                    self._save(username, cookies, "checkpoint" if checkpoint or guard.challenge else stage if stage in {"two_factor", "ready", "api_authenticated"} else "blocked" if reason in {"credentials_rejected", "feedback_required", "authentication_rejected"} else "verification", request_id, attempted, user_agent, checkpoint, identifier if stage == "two_factor" else None)
            return self._result("error" if reason in {"credentials_rejected", "two_factor_rejected", "network_policy_blocked", "cancelled", "feedback_required", "authentication_rejected"} else "needs_attention", reason, cookies, checkpoint, user_agent)
        finally:
            self._warn_rejected(guard)
            if loader is not None:
                with suppress(Exception):
                    loader.close()

    def accept_browser_session(self, account: dict, cookies, *, stop_event=None) -> ApiResult:
        loader = None
        guard = _NetworkGuard(stop_event, self.timeout, self.clock)
        values = {}
        checkpoint = None
        username = user_agent = ""
        saved = None
        try:
            username = self._account(account)
            if self.store is None:
                raise InstagramApiStorageError()
            values = _browser_cookies(cookies)
            if not values.get("sessionid"):
                return self._result("needs_attention", "confirmation_required")
            saved = self.store.load(username)
            loader = self._loader()
            user_agent = saved.user_agent if saved and saved.user_agent else loader.context.user_agent
            checkpoint = saved.checkpoint_url if saved else None
            with guard:
                same_session = saved is not None and saved.cookies.get("sessionid") == values.get("sessionid")
                return self._verify(loader, guard, username, values, saved.request_id if saved else 0,
                    saved.totp_attempted if saved else False, user_agent, checkpoint,
                    was_ready=same_session and saved.pending_stage=="ready", was_authenticated=same_session and saved.pending_stage=="api_authenticated")
        except InstagramApiStorageError:
            return self._result("error", "storage_error")
        except Exception as error:
            reason = error.reason if isinstance(error, _ApiStop) else "challenge_required" if guard.challenge else "feedback_required" if guard.feedback else "rate_limited" if guard.rate_limited else "network_error" if guard.network_failed else "api_error"
            with suppress(Exception):
                self._save(username, values, "checkpoint" if guard.checkpoint_url or checkpoint else "verification",
                    saved.request_id if saved else 0, saved.totp_attempted if saved else False, user_agent, guard.checkpoint_url or checkpoint)
            return self._result("error" if reason == "feedback_required" else "needs_attention", reason, values, guard.checkpoint_url or checkpoint, user_agent)
        finally:
            self._warn_rejected(guard)
            if loader is not None:
                with suppress(Exception):
                    loader.close()
