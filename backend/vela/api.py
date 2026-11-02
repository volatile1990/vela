"""Trusted-network HTTP interface; external media fetching belongs to the worker."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings
from .db import Database
from .source_sessions import MAX_COOKIE_BYTES, SOURCE_SITES, SourceSessionError, SourceSessionStore
from .browser_client import BrowserClient


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=4096)
    profile: str = "original"
    max_height: int | None = 1080
    playlist: bool = False
    playlist_items: str | None = Field(default=None, max_length=512)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("A public HTTP(S) media URL without embedded credentials is required")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Invalid URL")
        try:
            parsed.port
        except ValueError:
            raise ValueError("Invalid URL port") from None
        import ipaddress
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".home.arpa")) or "%" in host:
            raise ValueError("Private network URLs are not supported")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Private network URLs are not supported")
        return value

    @field_validator("profile")
    @classmethod
    def valid_profile(cls, value):
        if value not in {"original", "compatible", "audio"}:
            raise ValueError("Invalid output profile")
        return value

    @field_validator("max_height")
    @classmethod
    def valid_height(cls, value):
        if value not in {None, 480, 720, 1080, 2160}:
            raise ValueError("Invalid maximum video height")
        return value

    @field_validator("playlist_items")
    @classmethod
    def valid_playlist_items(cls, value):
        if value is not None and (not value or not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?(?:,[1-9]\d*(?:-[1-9]\d*)?)*", value)):
            raise ValueError("Playlist items must be positive indices or ranges, such as 1-5,8")
        return value


def _origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _safe_file(settings: Settings, relative_path: str):
    """Hold a descriptor, rejecting traversal and symlinks at every path component."""
    if "\\" in relative_path or "\x00" in relative_path:
        raise HTTPException(404, "File is unavailable")
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {".", "..", ""} for part in relative.parts):
        raise HTTPException(404, "File is unavailable")
    try:
        root = settings.download_dir.resolve(strict=True)
        marker = root / settings.storage_marker_name
        if marker.is_symlink() or marker.stat().st_size > 4096 or marker.read_text(encoding="utf-8").strip() != settings.storage_marker:
            raise HTTPException(503, "Download storage is unavailable")
    except (OSError, UnicodeError):
        raise HTTPException(503, "Download storage is unavailable") from None
    if os.name == "posix":
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise HTTPException(503, "Download storage is unavailable") from None
        try:
            for component in relative.parts[:-1]:
                next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            file_descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            info = os.fstat(file_descriptor)
            if not stat.S_ISREG(info.st_mode):
                os.close(file_descriptor)
                raise OSError("Artifact is not a regular file")
            return os.fdopen(file_descriptor, "rb"), info
        except OSError:
            raise HTTPException(404, "File is unavailable") from None
        finally:
            os.close(descriptor)
    # Local Windows development. Production Linux uses openat/O_NOFOLLOW above.
    candidate = root
    try:
        for component in relative.parts:
            candidate = candidate / component
            if candidate.is_symlink():
                raise OSError("Symlink artifact")
        candidate.resolve(strict=True).relative_to(root)
        handle = candidate.open("rb")
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            handle.close()
            raise OSError("Artifact is not a regular file")
        return handle, info
    except (OSError, ValueError):
        raise HTTPException(404, "File is unavailable") from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.db_path)
    source_sessions = SourceSessionStore(settings.credentials_dir)

    @asynccontextmanager
    async def lifespan(application):
        database.initialize()
        yield

    app = FastAPI(title="Vela", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database, app.state.settings = database, settings
    app.state.browser_client = BrowserClient(settings.browser_socket)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts), www_redirect=False)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Validation messages must not echo signed source URLs.
        details = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in error.errors()]
        return JSONResponse({"detail": details}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if _origin(request.headers.get("origin", "")) != _origin(settings.allowed_origin):
                return JSONResponse({"detail": "This origin is not allowed"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/session")
    def current_session():
        # Kept as a lightweight capability response for existing browser clients.
        return {"authenticated": True, "username": None, "csrf_token": None, "version": __version__}

    async def browser_body(request: Request) -> dict:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "Bitte die Eingabe als JSON übertragen.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 16 * 1024:
                raise HTTPException(413, "Die Eingabe ist zu groß.")
            body.extend(chunk)
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise HTTPException(422, "Die Eingabe ist nicht gültig.") from None

    @app.get("/api/instagram-account")
    async def instagram_account():
        return await app.state.browser_client.request("GET", "/status")

    @app.put("/api/instagram-account")
    async def save_instagram_account(request: Request):
        value = await browser_body(request)
        username, password = value.get("username"), value.get("password")
        if (set(value) != {"username", "password"} or not isinstance(username, str)
                or not 1 <= len(username.strip()) <= 254 or any(ord(x) < 32 for x in username)
                or not isinstance(password, str) or not 1 <= len(password) <= 1024):
            raise HTTPException(422, "Bitte einen Instagram-Benutzernamen und ein Passwort eingeben.")
        return await app.state.browser_client.request("PUT", "/account", {"username": username.strip(), "password": password})

    @app.delete("/api/instagram-account")
    async def remove_instagram_account():
        return await app.state.browser_client.request("DELETE", "/account")

    @app.put("/api/instagram-account/totp")
    async def save_instagram_totp(request: Request):
        value = await browser_body(request)
        secret = value.get("secret")
        if set(value) != {"secret"} or not isinstance(secret, str) or not 1 <= len(secret) <= 4096:
            raise HTTPException(422, "Bitte einen gültigen Instagram-Einrichtungsschlüssel eingeben.")
        return await app.state.browser_client.request("PUT", "/totp", {"secret": secret})

    @app.delete("/api/instagram-account/totp")
    async def remove_instagram_totp():
        return await app.state.browser_client.request("DELETE", "/totp")

    @app.post("/api/instagram-account/login")
    async def login_instagram(request: Request):
        if await browser_body(request) != {}:
            raise HTTPException(422, "Die Eingabe ist nicht gültig.")
        return await app.state.browser_client.request("POST", "/login", {})

    @app.post("/api/instagram-account/action")
    async def instagram_action(request: Request):
        import math
        value = await browser_body(request)
        interaction, kind = value.get("interaction_id"), value.get("kind")
        if (not isinstance(interaction, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", interaction)
                or not isinstance(kind, str) or kind not in {"click", "text", "key", "scroll"}):
            raise HTTPException(422, "Die Eingabe ist nicht gültig.")
        fields = {"click": {"x", "y"}, "text": {"text"}, "key": {"key"}, "scroll": {"delta"}}[kind]
        if set(value) != {"interaction_id", "kind"} | fields:
            raise HTTPException(422, "Die Eingabe ist nicht gültig.")
        if kind == "click" and any(type(value[x]) not in {int, float} or not math.isfinite(value[x]) or not 0 <= value[x] <= 1 for x in ("x", "y")):
            raise HTTPException(422, "Die Position ist nicht gültig.")
        if kind == "text" and (not isinstance(value["text"], str) or not 1 <= len(value["text"]) <= 4096):
            raise HTTPException(422, "Die Eingabe ist nicht gültig.")
        if kind == "key" and (not isinstance(value["key"], str) or value["key"] not in {"Enter", "Tab", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape", "Delete", "Home", "End", "Space"}):
            raise HTTPException(422, "Diese Taste ist nicht erlaubt.")
        if kind == "scroll" and (type(value["delta"]) is not int or not -1000 <= value["delta"] <= 1000):
            raise HTTPException(422, "Die Eingabe ist nicht gültig.")
        return await app.state.browser_client.request("POST", "/action", value)

    @app.get("/api/instagram-account/screen")
    async def instagram_screen():
        content = await app.state.browser_client.request("GET", "/screen", screen=True)
        return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.exception_handler(SourceSessionError)
    async def source_session_error(request: Request, error: SourceSessionError):
        return JSONResponse({"detail": str(error)}, status_code=error.status_code)

    @app.get("/api/source-sessions")
    def list_source_sessions():
        return {"items": source_sessions.items()}

    @app.put("/api/source-sessions/{site}")
    async def import_source_session(site: str, request: Request):
        if site not in SOURCE_SITES:
            raise HTTPException(404, "Diese Quellseite wird nicht unterstützt.")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/plain":
            raise HTTPException(415, "Bitte eine Cookies-Datei als einfachen UTF-8-Text übertragen.")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise HTTPException(400, "Die angegebene Dateigröße ist ungültig.") from None
            if size < 0 or size > MAX_COOKIE_BYTES:
                raise HTTPException(413, "Die Cookies-Datei darf höchstens 256 KiB groß sein.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_COOKIE_BYTES:
                raise HTTPException(413, "Die Cookies-Datei darf höchstens 256 KiB groß sein.")
            body.extend(chunk)
        try:
            text = body.decode("utf-8")
        except UnicodeError:
            raise HTTPException(422, "Die Cookies-Datei muss gültigen UTF-8-Text enthalten.") from None
        try:
            return await asyncio.to_thread(source_sessions.import_text, site, text)
        except OSError:
            raise HTTPException(503, "Der Sitzungsspeicher ist derzeit nicht verfügbar.") from None

    @app.delete("/api/source-sessions/{site}")
    def delete_source_session(site: str):
        try:
            source_sessions.delete(site)
        except OSError:
            raise HTTPException(503, "Der Sitzungsspeicher ist derzeit nicht verfügbar.") from None
        return {"ok": True}

    @app.get("/api/overview")
    def overview():
        worker = database.get_worker_status()
        try:
            heartbeat = datetime.fromisoformat(worker.get("heartbeat_at", "").replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - heartbeat).total_seconds() > settings.worker_stale_seconds:
                worker["status"] = "offline"
        except (ValueError, TypeError, AttributeError):
            worker["status"] = "offline"
        return {"version": __version__, "counts": database.job_counts(), "worker": worker, "storage": worker.get("storage", {"available": False})}

    @app.get("/api/jobs")
    def jobs(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        return {"items": database.list_jobs(limit, offset), "total": database.count_jobs()}

    def enqueue(body: JobInput, kind: str, idempotency_key: str | None):
        if idempotency_key and (len(idempotency_key) > 128 or not re.fullmatch(r"[\w.:-]+", idempotency_key, flags=re.ASCII)):
            raise HTTPException(400, "Invalid idempotency key")
        if body.playlist_items:
            count = 0
            for part in body.playlist_items.split(","):
                ends = [int(value) for value in part.split("-")]
                if len(ends) == 2 and ends[1] < ends[0]:
                    raise HTTPException(422, "Invalid playlist range")
                if ends[-1] > settings.max_playlist_items:
                    raise HTTPException(422, "Selected playlist index exceeds the configured limit")
                count += ends[-1] - ends[0] + 1
            if count > settings.max_playlist_items:
                raise HTTPException(422, "Too many selected playlist items")
        try:
            return database.create_job(**body.model_dump(), kind=kind, idempotency_key=idempotency_key, max_queued=settings.max_queue_size)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        except OverflowError:
            raise HTTPException(429, "The download queue is full") from None

    @app.post("/api/jobs", status_code=202)
    def add_job(body: JobInput, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
        return enqueue(body, "download", idempotency_key)

    @app.post("/api/previews", status_code=202)
    def preview(body: JobInput, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
        return enqueue(body, "probe", idempotency_key)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            return database.request_cancel(job_id)
        except KeyError:
            raise HTTPException(404, "Job not found") from None

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        try:
            return database.retry_job(job_id)
        except KeyError:
            raise HTTPException(404, "Job not found") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/events")
    async def events(request: Request):
        try:
            cursor = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor") from None

        async def stream():
            nonlocal cursor
            last_heartbeat = 0.0
            latest = await asyncio.to_thread(database.latest_event_id)
            if cursor == 0 or cursor > latest or cursor < latest - 2000:
                cursor = latest
                yield f"id: {cursor}\nevent: changed\ndata: {{\"snapshot\":true}}\n\n"
            while not await request.is_disconnected():
                records = await asyncio.to_thread(database.events_after, cursor)
                for record in records:
                    cursor = record["id"]
                    yield f"id: {cursor}\nevent: changed\ndata: {json.dumps(record['data'], separators=(',', ':'))}\n\n"
                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(settings.poll_interval)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    @app.get("/api/artifacts/{artifact_id}/download")
    def download(artifact_id: str, request: Request):
        artifact = database.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(404, "File not found")
        handle, info = _safe_file(settings, artifact["relative_path"])
        if info.st_size != artifact["size"]:
            handle.close()
            raise HTTPException(409, "The downloaded file has changed")
        start, end, status_code = 0, info.st_size - 1, 200
        range_header = request.headers.get("range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()) or info.st_size == 0:
                handle.close()
                raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{info.st_size}"})
            left, right = match.groups()
            if left:
                start, end = int(left), min(int(right), end) if right else end
            else:
                start = max(0, info.st_size - int(right))
            if start > end or start >= info.st_size:
                handle.close()
                raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{info.st_size}"})
            status_code = 206
        size = max(0, end - start + 1)
        from urllib.parse import quote
        # Shared worker metadata never controls headers or filesystem locations directly.
        name = artifact["name"].replace("\r", "").replace("\n", "").replace("\\", "_").replace("/", "_")[:255] or "download"
        headers = {"Content-Length": str(size), "Accept-Ranges": "bytes", "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{info.st_size}"

        def chunks():
            try:
                handle.seek(start)
                remaining = size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                handle.close()

        return StreamingResponse(chunks(), status_code=status_code, media_type="application/octet-stream", headers=headers)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"], include_in_schema=False)
    def unknown_api(path: str):
        raise HTTPException(404, "Endpoint not found")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Endpoint not found")
        root = settings.frontend_dir.resolve()
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(404, "File not found") from None
        if path and candidate.is_file():
            return FileResponse(candidate)
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Vela is temporarily unavailable")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
