"""Fail-closed NAS publication with deterministic paths and verified copies."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import uuid


class StorageError(RuntimeError):
    def __init__(self, message: str, category: str = "storage"):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class PublishedFile:
    relative_path: str
    size: int
    sha256: str
    filename: str


def safe_filename(title: str, extension: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:160] or "Medium"
    extension = extension.lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,12}", extension):
        raise StorageError("Ungültige Dateiendung.", "path")
    return f"{cleaned}.{extension}"


def file_hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class Storage:
    def __init__(self, root: Path | str, *, marker: str = ".vela-volume",
                 marker_value: str = "vela", reserve_bytes: int = 256 * 1024 * 1024):
        self.root = Path(root).absolute()
        self.marker = marker
        self.marker_value = marker_value
        self.reserve_bytes = reserve_bytes
        if Path(marker).name != marker:
            raise ValueError("Marker must be a plain filename")

    def check(self, required_bytes: int = 0) -> None:
        # Never create root: otherwise an absent NAS can fill the host root disk.
        try:
            if not self.root.is_dir() or self.root.is_symlink():
                raise StorageError("Der Ausgabespeicher ist nicht eingebunden.", "storage_offline")
            marker = self.root / self.marker
            if marker.is_symlink() or marker.read_text(encoding="utf-8").strip() != self.marker_value:
                raise StorageError("Die Identität des Ausgabespeichers stimmt nicht.", "storage_offline")
            if shutil.disk_usage(self.root).free < required_bytes + self.reserve_bytes:
                raise StorageError("Auf dem Ausgabespeicher ist nicht genügend Platz.", "storage_full")
        except (OSError, UnicodeError) as exc:
            raise StorageError("Der Ausgabespeicher ist nicht verfügbar.", "storage_offline") from exc

    def publish(self, job_id: str, source: Path | str, *, title: str = "Medium") -> PublishedFile:
        try:
            if str(uuid.UUID(job_id)) != job_id:
                raise ValueError
        except ValueError as exc:
            raise StorageError("Ungültige Auftragskennung.", "path") from exc
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise StorageError("Die fertige Arbeitsdatei fehlt.", "missing_file")
        size = source.stat().st_size
        if size <= 0:
            raise StorageError("Die Arbeitsdatei ist leer.", "missing_file")
        self.check(size)
        target_dir = self.root / job_id
        if target_dir.is_symlink():
            raise StorageError("Ungültiges Ausgabeverzeichnis.", "path")
        target_dir.mkdir(exist_ok=True)
        if not target_dir.resolve().is_relative_to(self.root.resolve()):
            raise StorageError("Ausgabepfad außerhalb des Speichers.", "path")
        filename = safe_filename(title, source.suffix)
        target = target_dir / filename
        if target.is_symlink():
            raise StorageError("Ungültiger Ausgabepfad.", "path")
        if target.exists():
            source_hash, source_size = file_hash(source)
            target_hash, target_size = file_hash(target)
            if source_hash != target_hash or source_size != target_size:
                raise StorageError("Eine abweichende Zieldatei besteht bereits.", "conflict")
            self.check()
            return PublishedFile(target.relative_to(self.root).as_posix(), target_size, target_hash, filename)
        temporary = target_dir / f".{filename}.vela.tmp"
        if temporary.is_symlink():
            raise StorageError("Ungültiger temporärer Ausgabepfad.", "path")
        digest = hashlib.sha256()
        copied = 0
        try:
            with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    self.check()
                    outgoing.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            actual_hash, actual_size = file_hash(temporary)
            if copied != size or actual_size != size or actual_hash != digest.hexdigest():
                raise StorageError("Die Kopierprüfung ist fehlgeschlagen.", "integrity")
            self.check()
            if target.exists():
                raise StorageError("Die Zieldatei wurde zwischenzeitlich angelegt.", "conflict")
            os.replace(temporary, target)
            self._sync_dir(target_dir)
            return PublishedFile(target.relative_to(self.root).as_posix(), size, actual_hash, filename)
        except OSError as exc:
            raise StorageError("Die Datei konnte nicht auf dem Ausgabespeicher veröffentlicht werden.") from exc

    @staticmethod
    def _sync_dir(path: Path) -> None:
        if os.name == "nt":
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
