"""Encrypted, account-bound Instagram API continuation; no browser imports."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import secrets
from urllib.parse import unquote, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


STAGES = frozenset({"login_pending", "two_factor", "verification", "checkpoint", "ready", "blocked"})
MAX_PAYLOAD = 12288
_PREFIX = b"VELAIGAPI1"
_AAD = b"vela.instagram.api.session.v1"
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")


class InstagramApiStorageError(ValueError):
    def __init__(self):
        super().__init__("Die geschützte Instagram-API-Sitzung konnte nicht verarbeitet werden.")


def safe_checkpoint_url(value: str | None) -> str | None:
    """Return only a private HTTPS Instagram confirmation destination."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            return None
        if value.startswith("/") and not value.startswith("//"):
            value = "https://www.instagram.com" + value
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == "instagram.com" or host.endswith(".instagram.com")) or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None or parsed.fragment:
            return None
        decoded_path = unquote(parsed.path)
        if "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")) or any(ord(c) < 32 or ord(c) == 127 for c in decoded_path):
            return None
        if not any(parsed.path.startswith(prefix) for prefix in ("/challenge/", "/checkpoint/", "/accounts/login/two_factor/", "/accounts/login/two_step_verification/")):
            return None
        return urlunsplit(("https", host, parsed.path, parsed.query, ""))
    except (ValueError, TypeError):
        return None


def validate_cookie_dict(cookies: dict) -> dict[str, str]:
    if not isinstance(cookies, dict) or len(cookies) > 128:
        raise InstagramApiStorageError()
    result = {}
    for name, value in cookies.items():
        if not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name) or not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise InstagramApiStorageError()
        if value:
            result[name] = value
    return result


@dataclass(frozen=True, repr=False)
class StoredInstagramApiSession:
    cookies: dict[str, str] = field(repr=False)
    pending_stage: str
    checkpoint_url: str | None = field(default=None, repr=False)
    two_factor_identifier: str | None = field(default=None, repr=False)
    request_id: int = 0
    totp_attempted: bool = False
    user_agent: str = field(default="", repr=False)


class InstagramApiSessionStore:
    """Reuse a supplied AccountVault's checked I/O and key, under a new AAD."""
    _FILE = "instagram.api-session.enc"

    def __init__(self, vault, private_directory):
        self.vault = vault
        self.private_directory = private_directory

    @staticmethod
    def _username(value):
        if not isinstance(value, str) or not value or len(value) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise InstagramApiStorageError()
        return value.casefold()

    @staticmethod
    def _validate(data, expected_username):
        required = {"version", "username", "cookies", "pending_stage", "checkpoint_url", "two_factor_identifier", "request_id", "totp_attempted", "user_agent"}
        if not isinstance(data, dict) or data.keys() != required or type(data["version"]) is not int or data["version"] != 1 or data["username"] != expected_username or not isinstance(data["pending_stage"], str) or data["pending_stage"] not in STAGES:
            raise InstagramApiStorageError()
        cookies = validate_cookie_dict(data["cookies"])
        checkpoint = safe_checkpoint_url(data["checkpoint_url"])
        if data["checkpoint_url"] is not None and checkpoint is None:
            raise InstagramApiStorageError()
        identifier = data["two_factor_identifier"]
        if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 1024 or any(ord(c) < 32 or ord(c) == 127 for c in identifier)):
            raise InstagramApiStorageError()
        if type(data["request_id"]) is not int or not 0 <= data["request_id"] <= 2**63-1 or type(data["totp_attempted"]) is not bool:
            raise InstagramApiStorageError()
        if not isinstance(data["user_agent"], str) or len(data["user_agent"]) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in data["user_agent"]):
            raise InstagramApiStorageError()
        return StoredInstagramApiSession(cookies, data["pending_stage"], checkpoint, identifier, data["request_id"], data["totp_attempted"], data["user_agent"])

    def load(self, expected_username: str) -> StoredInstagramApiSession | None:
        expected = self._username(expected_username)
        try:
            with self.private_directory(self.vault.directory) as descriptor:
                try:
                    encrypted = self.vault._read(self._FILE, descriptor)
                except FileNotFoundError:
                    return None
                key = self.vault._read(self.vault._KEY, descriptor)
            if len(key) != 32 or not encrypted.startswith(_PREFIX) or not len(_PREFIX)+28 <= len(encrypted) <= MAX_PAYLOAD+len(_PREFIX)+28:
                raise InstagramApiStorageError()
            offset = len(_PREFIX)
            payload = AESGCM(key).decrypt(encrypted[offset:offset+12], encrypted[offset+12:], _AAD)
            if len(payload) > MAX_PAYLOAD:
                raise InstagramApiStorageError()
            return self._validate(json.loads(payload), expected)
        except InstagramApiStorageError:
            raise
        except Exception:
            raise InstagramApiStorageError() from None

    def save(self, expected_username: str, *, cookies: dict, pending_stage: str, checkpoint_url=None,
             two_factor_identifier=None, request_id=0, totp_attempted=False, user_agent="") -> None:
        expected = self._username(expected_username)
        data = {"version": 1, "username": expected, "cookies": cookies, "pending_stage": pending_stage,
            "checkpoint_url": checkpoint_url, "two_factor_identifier": two_factor_identifier,
            "request_id": request_id, "totp_attempted": totp_attempted, "user_agent": user_agent}
        self._validate(data, expected)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > MAX_PAYLOAD:
            raise InstagramApiStorageError()
        try:
            with self.private_directory(self.vault.directory) as descriptor:
                key = self.vault._read(self.vault._KEY, descriptor)
                if len(key) != 32:
                    raise InstagramApiStorageError()
                nonce = secrets.token_bytes(12)
                self.vault._write(self._FILE, _PREFIX+nonce+AESGCM(key).encrypt(nonce, payload, _AAD), descriptor)
        except InstagramApiStorageError:
            raise
        except Exception:
            raise InstagramApiStorageError() from None

    def clear(self) -> None:
        try:
            with self.private_directory(self.vault.directory) as descriptor:
                try:
                    self.vault._read(self._FILE, descriptor)
                except FileNotFoundError:
                    return
                os.unlink(self._FILE if descriptor is not None else self.vault.directory/self._FILE, dir_fd=descriptor)
                if descriptor is not None:
                    os.fsync(descriptor)
        except Exception:
            raise InstagramApiStorageError() from None
