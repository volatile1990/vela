"""Bounded Netscape imports for fixed sources; secrets never become app metadata."""

from __future__ import annotations

import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit


MAX_COOKIE_BYTES = 256 * 1024
MAX_COOKIE_ROWS = 1000
MAX_LINE_BYTES = 16384
SOURCE_SITES = {
    "instagram": {"label": "Instagram", "domains": ("instagram.com",), "source_hosts": ("instagram.com", "instagr.am")},
    "youtube": {"label": "YouTube", "domains": ("youtube.com", "google.com"), "source_hosts": ("youtube.com", "youtu.be")},
    "tiktok": {"label": "TikTok", "domains": ("tiktok.com",), "source_hosts": ("tiktok.com",)},
    "facebook": {"label": "Facebook", "domains": ("facebook.com",), "source_hosts": ("facebook.com", "fb.watch")},
    "x": {"label": "X", "domains": ("x.com", "twitter.com"), "source_hosts": ("x.com", "twitter.com")},
}
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class SourceSessionError(ValueError):
    def __init__(self, message: str, status_code: int = 422, *, state: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.state = state


@dataclass(frozen=True, repr=False)
class ValidatedCookies:
    text: str
    cookie_count: int
    expires_at: str | None
    state: str = "configured"


def _site(site: str) -> dict:
    if site not in SOURCE_SITES:
        raise SourceSessionError("Diese Quellseite wird nicht unterstützt.", 404)
    return SOURCE_SITES[site]


def _matches(host: str, domains) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def site_for_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or "").lower().rstrip(".")
        for site, configuration in SOURCE_SITES.items():
            if _matches(host, configuration["source_hosts"]):
                return site
    except (ValueError, TypeError):
        pass
    return None


def _iso(timestamp: int | float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_cookie_text(site: str, text: str, *, now: int | None = None) -> ValidatedCookies:
    configuration = _site(site)
    if not isinstance(text, str):
        raise SourceSessionError("Bitte eine Cookies-Datei im Netscape-Textformat auswählen.")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise SourceSessionError("Die Cookies-Datei muss gültigen UTF-8-Text enthalten.") from None
    if len(encoded) > MAX_COOKIE_BYTES:
        raise SourceSessionError("Die Cookies-Datei darf höchstens 256 KiB groß sein.", 413)
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
        raise SourceSessionError("Die Datei ist keine Cookies-Datei im Netscape-Format.")
    now = int(time.time()) if now is None else now
    retained: dict[tuple[str, str, str], tuple[list[str], bool, int]] = {}
    rows = 0
    matched = 0
    expired_instagram_session = False
    for line in lines[1:]:
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise SourceSessionError("Eine Zeile der Cookies-Datei ist zu lang.")
        http_only = line.startswith("#HttpOnly_")
        if not line.strip() or (line.startswith("#") and not http_only):
            continue
        rows += 1
        if rows > MAX_COOKIE_ROWS:
            raise SourceSessionError("Die Cookies-Datei enthält zu viele Einträge.")
        if http_only:
            line = line[len("#HttpOnly_"):]
        fields = line.split("\t")
        if len(fields) != 7:
            raise SourceSessionError("Die Cookies-Datei enthält ungültige Netscape-Einträge.")
        domain, include_subdomains, path, secure, expires, name, value = fields
        bare_domain = domain.removeprefix(".").lower()
        # Unrelated site cookies are discarded before interpreting their values.
        if not _matches(bare_domain, configuration["domains"]):
            continue
        matched += 1
        if len(domain) > 254 or not _DOMAIN.fullmatch(bare_domain):
            raise SourceSessionError("Die Cookies-Datei enthält eine ungültige Cookie-Domain.")
        if include_subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}:
            raise SourceSessionError("Die Cookies-Datei enthält ungültige Sicherheitsangaben.")
        if domain.startswith(".") != (include_subdomains == "TRUE"):
            raise SourceSessionError("Cookie-Domain und Unterdomain-Angabe passen nicht zusammen.")
        if not path.startswith("/") or len(path) > 2048 or any(ord(character) < 32 or ord(character) > 126 for character in path):
            raise SourceSessionError("Die Cookies-Datei enthält einen ungültigen Cookie-Pfad.")
        if not _COOKIE_NAME.fullmatch(name) or len(value.encode("utf-8")) > 8192 or any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise SourceSessionError("Die Cookies-Datei enthält einen ungültigen Cookie-Namen oder -Wert.")
        if expires and not re.fullmatch(r"\d{1,12}", expires):
            raise SourceSessionError("Die Cookies-Datei enthält einen ungültigen Ablaufzeitpunkt.")
        expiration = int(expires or "0")
        if expiration > 253402300799:
            raise SourceSessionError("Ein Ablaufzeitpunkt in der Cookies-Datei ist ungültig.")
        if expiration and expiration <= now:
            retained.pop((("." if domain.startswith(".") else "") + bare_domain, path, name), None)
            if site == "instagram" and name == "sessionid" and value:
                expired_instagram_session = True
            continue
        fields[0] = ("." if domain.startswith(".") else "") + bare_domain
        # Empty expiry is the interoperable Netscape representation for a
        # session cookie. A literal zero is otherwise expired in MozillaCookieJar.
        fields[4] = str(expiration) if expiration else ""
        retained[(fields[0], path, name)] = (fields, http_only, expiration)
    if not retained:
        if matched:
            raise SourceSessionError("Die Sitzung ist abgelaufen. Bitte aktuelle Cookies aus deinem Browser exportieren.", state="expired")
        raise SourceSessionError("Die Datei enthält keine passenden Cookies für diese Quellseite.")
    if site == "instagram" and not any(fields[5] == "sessionid" and fields[6] for fields, _, _ in retained.values()):
        if expired_instagram_session:
            raise SourceSessionError("Die Instagram-Sitzung ist abgelaufen. Bitte neu anmelden und die Cookies erneut exportieren.", state="expired")
        raise SourceSessionError("Die Datei enthält keine angemeldete Instagram-Sitzung (sessionid). Bitte nach der Anmeldung erneut exportieren.")
    expirations = [expiration for _, _, expiration in retained.values() if expiration]
    if site == "instagram":
        expirations = [expiration for fields, _, expiration in retained.values() if fields[5] == "sessionid" and expiration]
    output = ["# Netscape HTTP Cookie File", "# Vela: cookies filtered for the selected source."]
    output.extend(("#HttpOnly_" if http_only else "") + "\t".join(fields) for fields, http_only, _ in retained.values())
    return ValidatedCookies("\n".join(output) + "\n", len(retained), _iso(min(expirations)) if expirations else None)


class SourceSessionStore:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self._lock = RLock()

    @contextmanager
    def _directory(self, *, create: bool = False):
        descriptor = None
        try:
            if self.directory.is_symlink():
                raise SourceSessionError("Der Sitzungsspeicher ist nicht sicher verfügbar.", 503)
            if create:
                self.directory.mkdir(mode=0o700, exist_ok=True)
            if not self.directory.exists():
                raise FileNotFoundError
            if not self.directory.is_dir():
                raise SourceSessionError("Der Sitzungsspeicher ist nicht sicher verfügbar.", 503)
            if os.name == "posix":
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            yield descriptor
        except FileNotFoundError:
            raise
        except OSError:
            raise SourceSessionError("Der Sitzungsspeicher ist derzeit nicht verfügbar.", 503) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _stat(self, name: str, descriptor):
        if descriptor is not None:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        else:
            info = (self.directory / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SourceSessionError("Die gespeicherte Sitzung ist nicht sicher verfügbar.", 503)
        return info

    def _read(self, name: str, descriptor):
        info = self._stat(name, descriptor)
        if info.st_size > MAX_COOKIE_BYTES or (os.name == "posix" and info.st_mode & 0o077):
            raise SourceSessionError("Die gespeicherte Sitzung ist nicht sicher verfügbar.", 503)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name if descriptor is not None else self.directory / name, flags, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as stream:
            actual = os.fstat(stream.fileno())
            if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1 or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino) or (os.name == "posix" and actual.st_mode & 0o077):
                raise SourceSessionError("Die gespeicherte Sitzung ist nicht sicher verfügbar.", 503)
            data = stream.read(MAX_COOKIE_BYTES + 1)
        if len(data) > MAX_COOKIE_BYTES:
            raise SourceSessionError("Die gespeicherte Sitzung ist zu groß.")
        try:
            return data.decode("utf-8"), info
        except UnicodeError:
            raise SourceSessionError("Die gespeicherte Sitzung ist ungültig.") from None

    def status(self, site: str) -> dict:
        configuration = _site(site)
        item = {"site": site, "label": configuration["label"], "configured": False, "cookie_count": 0, "expires_at": None, "updated_at": None, "state": "missing"}
        with self._lock:
            try:
                with self._directory() as descriptor:
                    text, info = self._read(f"{site}.cookies.txt", descriptor)
                    item.update(configured=True, updated_at=_iso(info.st_mtime))
                    result = validate_cookie_text(site, text)
                    item.update(cookie_count=result.cookie_count, expires_at=result.expires_at, state=result.state)
            except FileNotFoundError:
                pass
            except SourceSessionError as error:
                item["state"] = error.state
        return item

    def items(self) -> list[dict]:
        return [self.status(site) for site in SOURCE_SITES]

    def import_text(self, site: str, text: str) -> dict:
        result = validate_cookie_text(site, text)
        name = f"{site}.cookies.txt"
        temporary = f".{site}.{secrets.token_hex(12)}.tmp"
        with self._lock, self._directory(create=True) as descriptor:
            created = False
            try:
                try:
                    self._stat(name, descriptor)
                except FileNotFoundError:
                    pass
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(temporary if descriptor is not None else self.directory / temporary, flags, 0o600, dir_fd=descriptor)
                created = True
                with os.fdopen(fd, "wb") as stream:
                    if os.name == "posix":
                        os.fchmod(stream.fileno(), 0o600)
                    stream.write(result.text.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                if descriptor is not None:
                    os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                    os.fsync(descriptor)
                else:
                    os.replace(self.directory / temporary, self.directory / name)
            finally:
                if created and descriptor is not None:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
                elif created:
                    (self.directory / temporary).unlink(missing_ok=True)
        return self.status(site)

    def delete(self, site: str) -> None:
        _site(site)
        with self._lock:
            try:
                with self._directory() as descriptor:
                    self._stat(f"{site}.cookies.txt", descriptor)
                    if descriptor is not None:
                        os.unlink(f"{site}.cookies.txt", dir_fd=descriptor)
                        os.fsync(descriptor)
                    else:
                        (self.directory / f"{site}.cookies.txt").unlink()
            except FileNotFoundError:
                pass
