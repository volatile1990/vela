"""Secret-free, durable coordination between API, worker and browser sidecar."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import Database, safe_sqlite_version, sqlite3, utcnow


STATES = frozenset({"unconfigured", "idle", "logging_in", "ready", "needs_attention", "error"})
MESSAGES = {
    "unconfigured": "Für Instagram ist noch kein Zugang gespeichert.",
    "idle": "Instagram-Zugang gespeichert. Die Anmeldung kann gestartet werden.",
    "logging_in": "Die Instagram-Anmeldung wird vorbereitet.",
    "ready": "Die Instagram-Sitzung wurde für Downloads bereitgestellt.",
    "needs_attention": "Instagram benötigt eine Bestätigung im Browserfenster.",
    "error": "Die Instagram-Anmeldung konnte nicht abgeschlossen werden.",
}
SAFE_MESSAGES = frozenset(MESSAGES.values()) | {
    "Die Anmeldung wurde unterbrochen. Bitte das Browserfenster erneut öffnen.",
    "Die Bestätigungszeit ist abgelaufen. Bitte die Anmeldung erneut öffnen.",
    "Der gespeicherte Instagram-Zugang konnte nicht sicher gelesen werden.",
    "Das Browserfenster konnte nicht geöffnet werden. Bitte später erneut versuchen.",
}
_DEFAULT = {"configured": False, "totp_configured": False, "state": "unconfigured", "message": MESSAGES["unconfigured"],
    "session_generation": 0, "request_id": 0, "completed_request_id": 0,
    "interaction_id": None, "browser_available": False, "updated_at": None}


class InstagramAuthState:
    def __init__(self, state_dir: Path | str):
        self.path = Path(state_dir) / "instagram-auth.sqlite3"
        self._database = Database(self.path)

    def initialize(self) -> None:
        if not safe_sqlite_version(sqlite3.sqlite_version) and os.environ.get("VELA_ALLOW_UNPATCHED_SQLITE") != "1":
            raise RuntimeError("Vela requires SQLite with the WAL-reset fix")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._database.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS instagram_auth(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL,last_requested REAL NOT NULL DEFAULT 0,browser_seen REAL NOT NULL DEFAULT 0,login_attempted INTEGER NOT NULL DEFAULT 0)")
            # Serialize upgrades because API, worker and sidecar may start together.
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns={row["name"] for row in connection.execute("PRAGMA table_info(instagram_auth)")}
                if "totp_attempted" not in columns:connection.execute("ALTER TABLE instagram_auth ADD COLUMN totp_attempted INTEGER NOT NULL DEFAULT 0")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            connection.execute("INSERT OR IGNORE INTO instagram_auth(id,value) VALUES(1,?)", (json.dumps(_DEFAULT),))

    def status(self) -> dict:
        if not self.path.exists():
            return dict(_DEFAULT)
        try:
            with self._database.connection() as connection:
                row = connection.execute("SELECT value,browser_seen FROM instagram_auth WHERE id=1").fetchone()
            if row is None:
                return dict(_DEFAULT)
            result = json.loads(row["value"])
            if result.get("browser_available") and time.time() - row["browser_seen"] > 30:
                result["browser_available"] = False
                result["interaction_id"] = None
            return {key: result.get(key, value) for key, value in _DEFAULT.items()}
        except sqlite3.OperationalError:
            return dict(_DEFAULT)

    def publish(self, **fields) -> dict:
        if fields.keys() - _DEFAULT.keys():
            raise ValueError("Unsupported Instagram state fields")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError("Invalid Instagram state")
        if "message" in fields and fields["message"] not in SAFE_MESSAGES:
            raise ValueError("Instagram messages must be static and secret-free")
        if "interaction_id" in fields and fields["interaction_id"] is not None:
            import re
            if not isinstance(fields["interaction_id"], str) or not re.fullmatch(r"[a-f0-9]{48}", fields["interaction_id"]):
                raise ValueError("Invalid interaction identifier")
        self.initialize()
        with self._database.transaction() as connection:
            row = connection.execute("SELECT value FROM instagram_auth WHERE id=1").fetchone()
            current = json.loads(row["value"])
            current.update(fields)
            if "state" in fields and "message" not in fields:
                current["message"] = MESSAGES[fields["state"]]
            current["updated_at"] = utcnow()
            if not current["browser_available"]:
                current["interaction_id"] = None
            connection.execute("UPDATE instagram_auth SET value=?,browser_seen=CASE WHEN ? THEN ? ELSE browser_seen END WHERE id=1", (json.dumps(current), bool(fields.get("browser_available")), time.time()))
        return self.status()

    def configure(self, configured: bool) -> dict:
        self.initialize()
        with self._database.transaction() as connection:
            row = connection.execute("SELECT value FROM instagram_auth WHERE id=1").fetchone()
            current = json.loads(row["value"])
            if not configured:current["totp_configured"]=False
            current.update(configured=bool(configured), state="idle" if configured else "unconfigured", message=MESSAGES["idle" if configured else "unconfigured"], completed_request_id=current["request_id"], browser_available=False, interaction_id=None, updated_at=utcnow())
            connection.execute("UPDATE instagram_auth SET value=?,last_requested=0 WHERE id=1", (json.dumps(current),))
        return self.status()

    def request_refresh(self, force: bool = False) -> int:
        self.initialize()
        with self._database.transaction() as connection:
            row = connection.execute("SELECT value,last_requested FROM instagram_auth WHERE id=1").fetchone()
            current = json.loads(row["value"])
            if not current["configured"]:
                return 0
            if current["request_id"] > current["completed_request_id"]:
                return current["request_id"]
            now = time.time()
            if now - row["last_requested"] < (10 if force else 60):
                return 0
            current["request_id"] += 1
            current.update(state="logging_in", message=MESSAGES["logging_in"], updated_at=utcnow())
            connection.execute("UPDATE instagram_auth SET value=?,last_requested=? WHERE id=1", (json.dumps(current),now))
            return current["request_id"]

    def pending_request(self) -> int | None:
        status = self.status()
        if status["configured"] and status["request_id"] > status["completed_request_id"]:
            return status["request_id"]
        return None

    def claim_refresh(self) -> int | None:
        request_id = self.pending_request()
        if request_id is not None:
            self.publish(state="logging_in")
        return request_id

    def consume_login_attempt(self, request_id: int) -> bool:
        self.initialize()
        with self._database.transaction() as connection:
            row = connection.execute("SELECT value,login_attempted FROM instagram_auth WHERE id=1").fetchone()
            current = json.loads(row["value"])
            if request_id != current["request_id"] or request_id <= row["login_attempted"] or request_id <= current["completed_request_id"]:
                return False
            connection.execute("UPDATE instagram_auth SET login_attempted=? WHERE id=1", (request_id,))
            return True

    def consume_totp_attempt(self, request_id: int) -> bool:
        self.initialize()
        with self._database.transaction() as connection:
            row=connection.execute("SELECT value,totp_attempted FROM instagram_auth WHERE id=1").fetchone()
            current=json.loads(row["value"])
            if request_id!=current["request_id"] or request_id<=row["totp_attempted"] or request_id<=current["completed_request_id"] or not current.get("totp_configured",False):return False
            connection.execute("UPDATE instagram_auth SET totp_attempted=? WHERE id=1",(request_id,))
            return True

    def totp_attempt_available(self,request_id:int) -> bool:
        self.initialize()
        with self._database.connection() as connection:
            row=connection.execute("SELECT value,totp_attempted FROM instagram_auth WHERE id=1").fetchone()
            current=json.loads(row["value"])
            return bool(current.get("totp_configured",False) and request_id==current["request_id"] and request_id>current["completed_request_id"] and request_id>row["totp_attempted"])

    def finish(self, request_id: int, state: str, *, message: str | None = None, interaction_id: str | None = None, browser_available: bool = False) -> dict:
        if state not in {"ready", "needs_attention", "error"} or (message is not None and message not in SAFE_MESSAGES):
            raise ValueError("Invalid Instagram result")
        self.initialize()
        with self._database.transaction() as connection:
            row = connection.execute("SELECT value FROM instagram_auth WHERE id=1").fetchone()
            current = json.loads(row["value"])
            if request_id != current["request_id"] or request_id <= current["completed_request_id"]:
                return current
            current.update(state=state, message=message or MESSAGES[state], completed_request_id=current["completed_request_id"] if state == "needs_attention" else request_id, browser_available=browser_available, interaction_id=interaction_id if browser_available else None, updated_at=utcnow())
            if state == "ready":
                current["session_generation"] += 1
            connection.execute("UPDATE instagram_auth SET value=?,browser_seen=? WHERE id=1", (json.dumps(current),time.time()))
        return self.status()
