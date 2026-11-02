"""Explicit filesystem, browser and process settings shared by both services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path("/state")
    work_dir: Path = Path("/work")
    download_dir: Path = Path("/downloads")
    credentials_dir: Path = Path("/credentials")
    frontend_dir: Path = Path("/app/frontend/dist")
    base_url: str = "https://vela.home.arpa"
    worker_stale_seconds: int = 45
    max_queue_size: int = 1000
    max_playlist_items: int = 100
    storage_marker: str = "vela"
    storage_marker_name: str = ".vela-volume"
    min_free_bytes: int = 2 * 1024**3
    max_work_bytes: int = 16 * 1024**3
    job_timeout_seconds: int = 6 * 60 * 60
    probe_timeout_seconds: int = 90
    poll_interval: float = 1.0
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vela.sqlite3"

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        host = urlsplit(self.base_url).hostname
        return (host,) if host else ()

    @property
    def allowed_origin(self) -> str:
        parsed = urlsplit(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("VELA_BASE_URL must be an HTTP(S) origin")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("VELA_BASE_URL must not contain a path, query or fragment")
        if not self.storage_marker or Path(self.storage_marker_name).name != self.storage_marker_name:
            raise ValueError("A nonempty storage marker and plain marker filename are required")
        if self.storage_marker_name in {".", "..", ""} or "/" in self.storage_marker_name or "\\" in self.storage_marker_name:
            raise ValueError("Invalid storage marker filename")
        if self.poll_interval < 0.1 or self.max_playlist_items < 1:
            raise ValueError("Invalid polling or playlist limit")
        if self.min_free_bytes < 0 or self.max_work_bytes < 1:
            raise ValueError("Invalid storage limits")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            state_dir=Path(os.environ.get("VELA_STATE_DIR", "/state")),
            work_dir=Path(os.environ.get("VELA_WORK_DIR", "/work")),
            download_dir=Path(os.environ.get("VELA_DOWNLOAD_DIR", "/downloads")),
            credentials_dir=Path(os.environ.get("VELA_CREDENTIALS_DIR", "/credentials")),
            frontend_dir=Path(os.environ.get("VELA_FRONTEND_DIR", "/app/frontend/dist")),
            base_url=os.environ.get("VELA_BASE_URL", "https://vela.home.arpa").rstrip("/"),
            worker_stale_seconds=int(os.environ.get("VELA_WORKER_STALE_SECONDS", "45")),
            max_queue_size=int(os.environ.get("VELA_MAX_QUEUE_SIZE", "1000")),
            max_playlist_items=int(os.environ.get("VELA_MAX_PLAYLIST_ITEMS", "100")),
            storage_marker=os.environ.get("VELA_STORAGE_MARKER", "vela"),
            storage_marker_name=os.environ.get("VELA_STORAGE_MARKER_NAME", ".vela-volume"),
            min_free_bytes=int(os.environ.get("VELA_MIN_FREE_BYTES", str(2 * 1024**3))),
            max_work_bytes=int(os.environ.get("VELA_MAX_WORK_BYTES", str(16 * 1024**3))),
            job_timeout_seconds=int(os.environ.get("VELA_JOB_TIMEOUT_SECONDS", str(6 * 60 * 60))),
            probe_timeout_seconds=int(os.environ.get("VELA_PROBE_TIMEOUT_SECONDS", "90")),
            poll_interval=float(os.environ.get("VELA_POLL_INTERVAL", "1")),
            log_level=os.environ.get("VELA_LOG_LEVEL", "INFO"),
        )
