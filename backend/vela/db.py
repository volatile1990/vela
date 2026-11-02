"""Small, durable SQLite queue shared by the API and one worker."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

try:  # The Linux image supplies a patched SQLite independently of Python.
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3


STATES = frozenset({"queued", "probing", "downloading", "postprocessing", "finalizing", "waiting_storage", "waiting_auth", "interrupted", "completed", "failed", "cancelled"})
TERMINAL = frozenset({"completed", "failed", "cancelled"})
ACTIVE = STATES - TERMINAL - {"queued"}
UPDATE_FIELDS = frozenset({"metadata", "title", "media_id", "extractor", "status", "phase", "progress", "downloaded_bytes", "total_bytes", "speed", "eta", "error", "error_code", "heartbeat_at", "cancel_requested", "finished_at"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_sqlite_version(version: str) -> bool:
    parts = tuple(int(part) for part in version.split(".")[:3])
    return parts >= (3, 51, 3) or (3, 50, 7) <= parts < (3, 51, 0) or (3, 44, 6) <= parts < (3, 45, 0)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def initialize(self) -> None:
        if not safe_sqlite_version(sqlite3.sqlite_version) and os.environ.get("VELA_ALLOW_UNPATCHED_SQLITE") != "1":
            raise RuntimeError("Vela requires SQLite with the WAL-reset fix (3.51.3+ or a supported patched release)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RuntimeError("Unsupported Vela database schema; use a compatible API and worker")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL, kind TEXT NOT NULL,
                    profile TEXT NOT NULL, max_height INTEGER, playlist INTEGER NOT NULL DEFAULT 0,
                    playlist_items TEXT, idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued', phase TEXT NOT NULL DEFAULT 'queued',
                    progress REAL, downloaded_bytes INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER,
                    speed REAL, eta REAL, title TEXT, media_id TEXT, extractor TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}', error TEXT, error_code TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, heartbeat_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
                    mime TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(job_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version = 1;
                COMMIT;
            """)

    @staticmethod
    def _event(connection, data: dict, event_type: str = "changed") -> None:
        connection.execute("INSERT INTO events(type,data,created_at) VALUES(?,?,?)", (event_type, json.dumps(data, separators=(",", ":")), utcnow()))
        # Keep a bounded replay window. Clients falling behind receive a snapshot.
        connection.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0)-2000 FROM events)")

    @staticmethod
    def _job(connection, row) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value.pop("idempotency_key", None)
        value["metadata"] = json.loads(value["metadata"])
        value["playlist"] = bool(value["playlist"])
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["artifacts"] = [dict(item) for item in connection.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at,id", (value["id"],))]
        return value

    def create_job(self, *, url, profile="original", max_height=1080, playlist=False, kind="download", playlist_items=None, idempotency_key=None, max_queued=1000) -> dict:
        if kind not in {"download", "probe"} or profile not in {"original", "compatible", "audio"}:
            raise ValueError("Invalid job kind or profile")
        stamp, job_id = utcnow(), str(uuid4())
        with self.transaction() as connection:
            if idempotency_key:
                previous = connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if previous:
                    expected = (url, profile, max_height, bool(playlist), kind, playlist_items)
                    actual = (previous["url"], previous["profile"], previous["max_height"], bool(previous["playlist"]), previous["kind"], previous["playlist_items"])
                    if expected != actual:
                        raise ValueError("Idempotency key was already used for a different request")
                    return self._job(connection, previous)
            if connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','waiting_auth')").fetchone()[0] >= max_queued:
                raise OverflowError("The download queue is full")
            connection.execute("""INSERT INTO jobs(id,url,kind,profile,max_height,playlist,playlist_items,idempotency_key,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (job_id,url,kind,profile,max_height,int(playlist),playlist_items,idempotency_key,stamp,stamp))
            self._event(connection, {"job_id": job_id})
            return self._job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get_job(self, job_id) -> dict | None:
        with self.connection() as connection:
            return self._job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit=100, offset=0) -> list[dict]:
        with self.connection() as connection:
            return [self._job(connection, row) for row in connection.execute("SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()]

    def count_jobs(self) -> int:
        with self.connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def job_counts(self) -> dict:
        with self.connection() as connection:
            counts = dict(connection.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
        return {"queued": counts.get("queued", 0), "active": sum(counts.get(state, 0) for state in ACTIVE), "completed": counts.get("completed", 0), "failed": counts.get("failed", 0)}

    def claim_next(self, kind: str | None = None) -> dict | None:
        if kind not in {None, "probe", "download"}:
            raise ValueError("Invalid job kind")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 AND (? IS NULL OR kind=?) ORDER BY created_at,id LIMIT 1", (kind,kind)).fetchone()
            if row is None:
                return None
            stamp = utcnow()
            connection.execute("UPDATE jobs SET status='probing',phase='probing',attempt=attempt+1,started_at=?,updated_at=?,heartbeat_at=? WHERE id=?", (stamp,stamp,stamp,row["id"]))
            self._event(connection, {"job_id": row["id"]})
            return self._job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def update_job(self, job_id, **fields) -> dict:
        if fields.keys() - UPDATE_FIELDS:
            raise ValueError("Unsupported job update fields")
        if "status" in fields and fields["status"] not in STATES:
            raise ValueError("Invalid job status")
        if "progress" in fields and fields["progress"] is not None:
            fields["progress"] = max(0, min(100, float(fields["progress"])))
        if "metadata" in fields:
            if not isinstance(fields["metadata"], dict):
                raise ValueError("Job metadata must be an object")
            fields["metadata"] = json.dumps(fields["metadata"], ensure_ascii=False, allow_nan=False)
        if "cancel_requested" in fields:
            fields["cancel_requested"] = int(fields["cancel_requested"])
        fields["updated_at"] = utcnow()
        with self.transaction() as connection:
            if not connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise KeyError(job_id)
            connection.execute("UPDATE jobs SET " + ",".join(f"{name}=?" for name in fields) + " WHERE id=?", (*fields.values(),job_id))
            self._event(connection, {"job_id": job_id})
            return self._job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def request_cancel(self, job_id) -> dict:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] in TERMINAL:
                return self._job(connection, row)
            stamp = utcnow()
            if row["status"] in {"queued", "waiting_auth"}:
                connection.execute("UPDATE jobs SET status='cancelled',phase='cancelled',cancel_requested=1,finished_at=?,updated_at=? WHERE id=?", (stamp,stamp,job_id))
            else:
                connection.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (stamp,job_id))
            self._event(connection, {"job_id": job_id})
            return self._job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def retry_job(self, job_id) -> dict:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in TERMINAL:
                raise ValueError("Only completed, failed or cancelled jobs can be retried")
            metadata = json.loads(row["metadata"])
            # This is the user's explicit retry boundary. Automatic auth
            # resumes and crash recovery deliberately retain this budget.
            metadata.pop("_instagram_auth", None)
            connection.execute("""UPDATE jobs SET status='queued',phase='queued',cancel_requested=0,progress=NULL,
                downloaded_bytes=0,total_bytes=NULL,speed=NULL,eta=NULL,error=NULL,error_code=NULL,
                started_at=NULL,finished_at=NULL,heartbeat_at=NULL,metadata=?,updated_at=? WHERE id=?""", (json.dumps(metadata, ensure_ascii=False, allow_nan=False),utcnow(),job_id))
            self._event(connection, {"job_id": job_id})
            return self._job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def active_jobs(self) -> list[dict]:
        with self.connection() as connection:
            return [self._job(connection, row) for row in connection.execute("SELECT * FROM jobs WHERE status NOT IN ('queued','completed','failed','cancelled')").fetchall()]

    def resolve_waiting_auth(self, job_id: str, *, status: str, error: str | None = None,
                             error_code: str | None = None) -> bool:
        """Resolve only a still-waiting job; cancellation always wins the race."""
        if status not in {"queued", "failed", "cancelled"}:
            raise ValueError("Invalid authentication resolution")
        with self.transaction() as connection:
            row = connection.execute("SELECT status,cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "waiting_auth":
                return False
            if row["cancel_requested"]:
                status, error, error_code = "cancelled", None, None
            stamp = utcnow()
            connection.execute("""UPDATE jobs SET status=?,phase=?,error=?,error_code=?,progress=NULL,
                speed=NULL,eta=NULL,heartbeat_at=NULL,finished_at=?,updated_at=? WHERE id=?""",
                (status,status,error,error_code,None if status == "queued" else stamp,stamp,job_id))
            self._event(connection, {"job_id": job_id})
            return True

    def update_waiting_auth(self, job_id: str, **fields) -> bool:
        if not fields or fields.keys() - {"metadata", "error", "error_code"}:
            raise ValueError("Invalid authentication update")
        if "metadata" in fields:
            if not isinstance(fields["metadata"], dict):
                raise ValueError("Job metadata must be an object")
            fields["metadata"] = json.dumps(fields["metadata"], ensure_ascii=False, allow_nan=False)
        fields["updated_at"] = utcnow()
        with self.transaction() as connection:
            changed = connection.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) +
                " WHERE id=? AND status='waiting_auth' AND cancel_requested=0", (*fields.values(),job_id)).rowcount
            if changed:
                self._event(connection, {"job_id": job_id})
            return bool(changed)

    def add_artifact(self, job_id, *, relative_path, name, size, mime, sha256) -> dict:
        if size < 0:
            raise ValueError("Invalid artifact size")
        with self.transaction() as connection:
            connection.execute("""INSERT INTO artifacts(id,job_id,relative_path,name,size,mime,sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,relative_path) DO UPDATE SET name=excluded.name,
                size=excluded.size,mime=excluded.mime,sha256=excluded.sha256""", (str(uuid4()),job_id,relative_path,name,size,mime,sha256,utcnow()))
            self._event(connection, {"job_id": job_id})
            return dict(connection.execute("SELECT * FROM artifacts WHERE job_id=? AND relative_path=?", (job_id,relative_path)).fetchone())

    def list_artifacts(self, job_id) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at,id", (job_id,))]

    def get_artifact(self, artifact_id) -> dict | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            return dict(row) if row else None

    def set_worker_status(self, status: dict) -> None:
        with self.transaction() as connection:
            previous_row = connection.execute("SELECT value FROM settings WHERE key='worker'").fetchone()
            previous = json.loads(previous_row[0]) if previous_row else {}
            connection.execute("INSERT INTO settings(key,value) VALUES('worker',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(status, allow_nan=False),))
            previous_event = connection.execute("SELECT value FROM settings WHERE key='worker_event_at'").fetchone()
            import time
            now = time.time()
            important_change = any(previous.get(key) != status.get(key) for key in ("status", "engine")) or previous.get("storage", {}).get("available") != status.get("storage", {}).get("available")
            if important_change or not previous_event or now - float(previous_event[0]) >= 10:
                self._event(connection, {"worker": True})
                connection.execute("INSERT INTO settings(key,value) VALUES('worker_event_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))

    def get_worker_status(self) -> dict:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key='worker'").fetchone()
            return json.loads(row[0]) if row else {"status": "offline", "heartbeat_at": None, "engine": {}, "storage": {"available": False}}

    def events_after(self, event_id, limit=100) -> list[dict]:
        with self.connection() as connection:
            return [{"id": row["id"], "type": row["type"], "data": json.loads(row["data"])} for row in connection.execute("SELECT id,type,data FROM events WHERE id>? ORDER BY id LIMIT ?", (event_id,limit))]

    def latest_event_id(self) -> int:
        with self.connection() as connection:
            return connection.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
