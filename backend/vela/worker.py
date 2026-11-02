"""Single-owner durable queue worker. SQLite is the source of truth."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import mimetypes
import os
from pathlib import Path
import shutil
import signal
import time
from typing import Any

from .config import Settings
from .db import Database
from .engine import Engine, EngineError, EngineCancelled, recover_process, validate_source_url
from .storage import Storage, StorageError


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerLock:
    """A heartbeat alone never grants permission to restart a previous job."""
    def __init__(self, state_dir: Path):
        self.path = state_dir / "worker.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if self.file.read(1) == b"":
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Ein anderer Vela-Worker besitzt bereits die Warteschlange.") from exc
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()
            self.file = None


def selected_indexes(expression: str | None, count: int, limit: int) -> list[int]:
    if not expression:
        return list(range(min(count, limit)))
    indexes: set[int] = set()
    try:
        for section in expression.split(","):
            bounds = [int(value.strip()) for value in section.strip().split("-")]
            if len(bounds) not in (1, 2):
                raise ValueError
            first, last = bounds[0], bounds[-1]
            if first < 1 or last < first or last > limit:
                raise ValueError
            indexes.update(range(first - 1, min(last, count)))
    except ValueError as exc:
        raise EngineError(f"Playlistauswahl muss zwischen 1 und {limit} liegen.", "validation") from exc
    if not indexes:
        raise EngineError("Die Playlistauswahl enthält keine verfügbaren Einträge.", "validation")
    return sorted(indexes)


class Worker:
    def __init__(self, settings: Settings, *, db: Database | None = None,
                 engine: Engine | None = None, storage: Storage | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.engine = engine or Engine(timeout=settings.job_timeout_seconds,
            probe_timeout=settings.probe_timeout_seconds, credentials_dir=settings.credentials_dir)
        self.storage = storage or Storage(settings.download_dir, marker=settings.storage_marker_name,
            marker_value=settings.storage_marker, reserve_bytes=settings.min_free_bytes)
        self.stop = asyncio.Event()
        self.active_cancels: set[asyncio.Event] = set()
        self.last_storage_retry = 0.0

    async def run(self) -> None:
        self.db.initialize()
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        with WorkerLock(self.settings.state_dir):
            self.recover()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(signum, self.request_stop)
                except (NotImplementedError, RuntimeError):
                    pass
            heartbeat = asyncio.create_task(self._heartbeat())
            running: dict[str, asyncio.Task] = {}
            try:
                while not self.stop.is_set():
                    if time.monotonic() - self.last_storage_retry >= 15:
                        self.last_storage_retry = time.monotonic()
                        await self.retry_finalizations()
                    for kind in ("download", "probe"):
                        task = running.get(kind)
                        if task is not None and task.done():
                            task.result()
                            del running[kind]
                        if kind not in running:
                            job = self.db.claim_next(kind=kind)
                            if job is not None:
                                running[kind] = asyncio.create_task(self.handle(job))
                    try:
                        await asyncio.wait_for(self.stop.wait(), self.settings.poll_interval)
                    except TimeoutError:
                        pass
            finally:
                self.request_stop()
                await asyncio.gather(*running.values(), return_exceptions=True)
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                self.db.set_worker_status({"status": "stopped", "heartbeat_at": utcnow()})

    def request_stop(self) -> None:
        self.stop.set()
        for cancel in self.active_cancels:
            cancel.set()

    def work_path(self, job_id: str) -> Path:
        import uuid
        if str(uuid.UUID(job_id)) != job_id:
            raise EngineError("Ungültige Auftragskennung.", "path")
        root = self.settings.work_dir.resolve()
        result = root / job_id
        if result.is_symlink() or not result.resolve().is_relative_to(root):
            raise EngineError("Ungültiges Arbeitsverzeichnis.", "path")
        return result

    def check_work(self) -> None:
        if shutil.disk_usage(self.settings.work_dir).free < self.settings.min_free_bytes:
            raise EngineError("Die freie Reserve auf dem Arbeitslaufwerk ist erreicht.", "work_full")
        total = 0
        for path in self.settings.work_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
                if total > self.settings.max_work_bytes:
                    raise EngineError("Das Arbeitsbudget wurde erreicht.", "work_full")

    def recover(self) -> None:
        active = self.db.active_jobs()
        active_ids = {job["id"] for job in active}
        for job in active:
            directory = self.work_path(job["id"])
            recover_process(directory)
            self.clear_session_copy(directory)
            if job.get("cancel_requested"):
                self.db.update_job(job["id"], status="cancelled", phase="cancelled", finished_at=utcnow())
                self.cleanup(directory)
            elif job["status"] in ("finalizing", "waiting_storage") and job.get("metadata", {}).get("finalization"):
                self.db.update_job(job["id"], status="waiting_storage", phase="finalizing", error=None, error_code=None)
            else:
                self.db.update_job(job["id"], status="queued", phase="queued", error=None, error_code=None)
        # A previously reconciled queued attempt can still carry a crash
        # remnant. Only known UUID job directories and the fixed private name
        # are touched; media partials remain available for resume.
        for directory in self.settings.work_dir.iterdir():
            if not directory.is_dir() or directory.is_symlink() or directory.name in active_ids:
                continue
            try:
                checked = self.work_path(directory.name)
            except (ValueError, EngineError):
                continue
            if self.db.get_job(directory.name) is not None:
                recover_process(checked)
                self.clear_session_copy(checked)

    @staticmethod
    def clear_session_copy(directory: Path) -> None:
        try:
            (directory / ".vela-cookies.txt").unlink(missing_ok=True)
        except OSError:
            raise EngineError("Die temporäre Sitzung eines unterbrochenen Auftrags konnte nicht bereinigt werden.", "session_cleanup") from None

    async def _heartbeat(self) -> None:
        while True:
            state: dict[str, Any] = {"available": False, "path": str(self.settings.download_dir), "label": "UNAS · Unprotected/Vela"}
            try:
                await asyncio.to_thread(self.storage.check)
                disk = await asyncio.to_thread(shutil.disk_usage, self.settings.download_dir)
                state.update(available=True, free_bytes=disk.free, total_bytes=disk.total, error=None)
            except StorageError as exc:
                state["error"] = str(exc)
            self.db.set_worker_status({"heartbeat_at": utcnow(), "status": "running",
                "engine": "yt-dlp · Deno · FFmpeg", "storage": state})
            await asyncio.sleep(max(1, self.settings.poll_interval))

    async def handle(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        metadata = dict(job.get("metadata") or {})
        if metadata.pop("finalization", None) is not None:
            # A fresh retry is a new download attempt. The previous attempt's
            # journal must not make a crash during this attempt look finalized.
            self.db.update_job(job_id, metadata=metadata)
        directory = self.work_path(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        cancel = asyncio.Event()
        self.active_cancels.add(cancel)
        monitor_error: list[EngineError] = []

        async def monitor() -> None:
            while True:
                latest = self.db.get_job(job_id)
                if self.stop.is_set() or (latest and latest.get("cancel_requested")):
                    cancel.set()
                    return
                try:
                    await asyncio.to_thread(self.check_work)
                except (EngineError, OSError) as exc:
                    monitor_error.append(exc if isinstance(exc, EngineError) else EngineError("Das Arbeitslaufwerk ist nicht verfügbar.", "work_full"))
                    cancel.set()
                    return
                self.db.update_job(job_id, heartbeat_at=utcnow())
                await asyncio.sleep(1)

        watcher = asyncio.create_task(monitor())
        last_progress = 0.0

        async def event(kind: str, payload: dict[str, Any]) -> None:
            nonlocal last_progress
            if kind == "metadata":
                self.db.update_job(job_id, metadata=payload, title=payload["title"],
                    media_id=payload["id"], extractor=payload["extractor"])
            elif kind == "source_session":
                current = self.db.get_job(job_id)
                metadata = dict(current.get("metadata") or {}) if current else {}
                metadata.update(payload)
                self.db.update_job(job_id, metadata=metadata)
            elif kind == "postprocessing":
                self.db.update_job(job_id, status="postprocessing", phase="postprocessing")
            elif kind == "progress" and time.monotonic() - last_progress >= 1:
                last_progress = time.monotonic()
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
                downloaded = payload.get("downloaded_bytes") or 0
                progress = min(100, 100 * downloaded / total) if isinstance(total, (int, float)) and total > 0 else None
                self.db.update_job(job_id, status="downloading", phase="downloading", progress=progress,
                    downloaded_bytes=downloaded, total_bytes=total, speed=payload.get("speed"), eta=payload.get("eta"))

        try:
            validate_source_url(job["url"])
            await asyncio.to_thread(self.check_work)
            if job["kind"] == "probe" or job.get("playlist"):
                info = await self.engine.probe(job["url"], directory, cancel=cancel,
                    playlist_limit=min(100, self.settings.max_playlist_items))
                self.db.update_job(job_id, metadata=info, title=info["title"], media_id=info["id"], extractor=info["extractor"])
                if job["kind"] == "probe":
                    self.db.update_job(job_id, status="completed", phase="completed", progress=100, finished_at=utcnow())
                    self.cleanup(directory)
                    return
                if info.get("type") == "playlist":
                    await self._expand_playlist(job, info, cancel)
                    self.cleanup(directory)
                    return
            self.db.update_job(job_id, status="downloading", phase="downloading")
            result = await self.engine.download(job["url"], directory, profile=job["profile"],
                max_height=job.get("max_height") or 2160, cancel=cancel, on_event=event)
            # This durable journal precedes all NAS writes. Repeating the copy
            # after a crash discovers an already-published identical file.
            metadata = dict(result.metadata)
            metadata["finalization"] = [{"source": file.relative_to(directory.resolve()).as_posix(),
                "title": metadata.get("title") or "Medium"} for file in result.files]
            self.db.update_job(job_id, metadata=metadata, status="finalizing", phase="finalizing", progress=100)
            await self.finalize(self.db.get_job(job_id))
        except EngineCancelled:
            if monitor_error:
                exc = monitor_error[0]
                self.db.update_job(job_id, status="failed", phase="failed", error=str(exc), error_code=exc.category, finished_at=utcnow())
                self.cleanup(directory)
            elif self.stop.is_set():
                self.db.update_job(job_id, status="interrupted", phase="interrupted", error="Worker wird neu gestartet.", error_code="interrupted")
            else:
                self.db.update_job(job_id, status="cancelled", phase="cancelled", finished_at=utcnow())
                self.cleanup(directory)
        except StorageError as exc:
            self.storage_failure(job_id, exc)
        except EngineError as exc:
            self.db.update_job(job_id, status="failed", phase="failed", error=str(exc), error_code=exc.category, finished_at=utcnow())
            if exc.category == "work_full":
                self.cleanup(directory)
        except Exception:
            # Never persist traceback text: extractor messages and paths may
            # contain source authentication material.
            self.db.update_job(job_id, status="failed", phase="failed", error="Der Auftrag konnte nicht abgeschlossen werden.", error_code="internal", finished_at=utcnow())
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self.active_cancels.discard(cancel)

    async def _expand_playlist(self, job: dict, info: dict, cancel: asyncio.Event) -> None:
        entries = info.get("entries", [])
        children = []
        for index in selected_indexes(job.get("playlist_items"), len(entries), self.settings.max_playlist_items):
            if cancel.is_set():
                raise EngineCancelled()
            entry = entries[index]
            url = validate_source_url(entry.get("url", ""))
            try:
                child = self.db.create_job(url=url, profile=job["profile"], max_height=job.get("max_height") or 2160,
                    playlist=False, idempotency_key=f"playlist:{job['id']}:{index}", max_queued=self.settings.max_queue_size)
            except OverflowError as exc:
                raise EngineError("Die Warteschlange ist voll. Bereits übernommene Playlist-Einträge bleiben erhalten.", "queue_full") from exc
            children.append(child["id"])
        info["playlist_jobs"] = children
        self.db.update_job(job["id"], metadata=info, status="completed", phase="completed", progress=100, finished_at=utcnow())

    async def retry_finalizations(self) -> None:
        for job in self.db.active_jobs():
            if job["status"] != "waiting_storage":
                continue
            if job.get("cancel_requested"):
                self.db.update_job(job["id"], status="cancelled", phase="cancelled", finished_at=utcnow())
                self.cleanup(self.work_path(job["id"]))
                continue
            try:
                await self.finalize(job)
            except StorageError as exc:
                self.storage_failure(job["id"], exc)
            except EngineError as exc:
                self.db.update_job(job["id"], status="failed", phase="failed", error=str(exc), error_code=exc.category, finished_at=utcnow())

    def storage_failure(self, job_id: str, exc: StorageError) -> None:
        if exc.category in ("conflict", "path", "missing_file"):
            self.db.update_job(job_id, status="failed", phase="failed", error=str(exc), error_code=exc.category, finished_at=utcnow())
        else:
            self.db.update_job(job_id, status="waiting_storage", phase="finalizing", error=str(exc), error_code=exc.category)

    async def finalize(self, job: dict) -> None:
        directory = self.work_path(job["id"])
        items = job.get("metadata", {}).get("finalization")
        if not isinstance(items, list) or not items:
            raise EngineError("Das Abschlussprotokoll fehlt.", "recovery")
        self.db.update_job(job["id"], status="finalizing", phase="finalizing")
        for item in items:
            source = (directory / item["source"]).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise EngineError("Ungültiger Pfad im Abschlussprotokoll.", "path")
            published = await asyncio.to_thread(self.storage.publish, job["id"], source, title=item.get("title", "Medium"))
            self.db.add_artifact(job["id"], relative_path=published.relative_path, name=published.filename,
                size=published.size, mime=mimetypes.guess_type(published.filename)[0] or "application/octet-stream", sha256=published.sha256)
        self.db.update_job(job["id"], status="completed", phase="completed", progress=100, error=None, error_code=None, finished_at=utcnow())
        self.cleanup(directory)

    def cleanup(self, directory: Path) -> None:
        root = self.settings.work_dir.resolve()
        if directory.is_symlink() or directory.resolve().parent != root:
            raise EngineError("Arbeitsverzeichnis darf nicht entfernt werden.", "path")
        shutil.rmtree(directory, ignore_errors=True)


def worker_healthy(settings: Settings) -> bool:
    try:
        status = Database(settings.db_path).get_worker_status()
        heartbeat = datetime.fromisoformat(status["heartbeat_at"])
        return status.get("status") == "running" and (datetime.now(timezone.utc) - heartbeat).total_seconds() < 30
    except (OSError, KeyError, ValueError):
        return False
