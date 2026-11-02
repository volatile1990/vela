"""Bounded, cancellable yt-dlp subprocess adapter.

Only the worker imports this module. No user-supplied command line options or
shell are involved; stdout is a private, structured protocol.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import ipaddress
import socket
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
METADATA_TEMPLATE = "%(.{id,title,duration,extractor_key,uploader})j"
PROGRESS_TEMPLATE = "%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta,fragment_index,fragment_count})j"


class EngineError(RuntimeError):
    def __init__(self, message: str, category: str = "engine"):
        super().__init__(message)
        self.category = category


class EngineCancelled(EngineError):
    def __init__(self) -> None:
        super().__init__("Auftrag abgebrochen.", "cancelled")


def validate_source_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 32 for char in url):
        raise EngineError("Ungültige Quelladresse.", "validation")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in (None, 80, 443):
            raise ValueError
        if host in ("localhost", "host.docker.internal", "gateway.docker.internal") or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
            raise ValueError
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
        except ValueError as exc:
            # A host containing ':' or only numeric IP notation is not a domain.
            if ":" in host or all(char in "0123456789." for char in host):
                raise ValueError from exc
    except (ValueError, TypeError) as exc:
        raise EngineError("Nur öffentliche HTTP-/HTTPS-Quellen sind erlaubt.", "validation") from exc
    return url


def _process_identity(pid: int) -> str | None:
    if os.name != "posix" or not Path("/proc").exists():
        return None
    try:
        # comm may itself contain spaces or parentheses; fields after its final
        # ')' begin at stat field 3. starttime is field 22.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _group_running(pgid: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                return True
        except (OSError, IndexError, ValueError):
            continue
    return False


def recover_process(work_dir: Path) -> None:
    """Kill only a recorded, still-identical Linux process group after a crash."""
    marker = work_dir / ".vela-process.json"
    if not marker.exists():
        return
    try:
        record = json.loads(marker.read_text())
        pid = int(record["pid"])
        if os.name != "posix":
            raise EngineError("Ein alter Engine-Prozess muss vor dem Neustart geprüft werden.", "recovery")
        identity = _process_identity(pid)
        if identity is not None:
            if identity != record.get("identity") or os.getpgid(pid) != pid:
                # PID was reused. It must never be signalled.
                marker.unlink()
                return
            os.killpg(pid, signal.SIGKILL)
        else:
            # The group can outlive its leader (e.g. FFmpeg). An existing group
            # with an absent leader cannot have become a new session with the
            # same PGID while its old members are alive.
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while _group_running(pid):
            if time.monotonic() >= deadline:
                raise EngineError("Der vorherige Engine-Prozess ist noch nicht beendet.", "recovery")
            time.sleep(0.05)
        marker.unlink(missing_ok=True)
    except EngineError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EngineError("Ein alter Engine-Prozess konnte nicht sicher abgeglichen werden.", "recovery") from exc


@dataclass
class DownloadResult:
    files: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def public_metadata(info: dict[str, Any]) -> dict[str, Any]:
    """Keep remote URLs, headers, cookies and arbitrary extractor fields private."""
    return {
        "id": str(info.get("id") or "")[:200],
        "title": str(info.get("title") or "Unbenannt")[:500],
        "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
        "extractor": str(info.get("extractor_key") or info.get("extractor") or "")[:100],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:200],
        "is_live": bool(info.get("is_live")),
        "playlist_count": info.get("playlist_count") if isinstance(info.get("playlist_count"), int) else None,
    }


def format_selector(profile: str, max_height: int) -> str:
    if max_height not in (360, 480, 720, 1080, 1440, 2160, 4320):
        raise EngineError("Ungültige Auflösung.", "validation")
    # Generic direct-media URLs often omit height. Preserve those streams;
    # the cap applies where the extractor knows the source resolution.
    height = f"[height<=?{max_height}]"
    if profile == "original":
        return f"bv*{height}+ba/b{height}"
    if profile == "compatible":
        # Deliberately fail instead of silently producing incompatible codecs or
        # launching expensive transcoding on the shared home server.
        return (f"bv*{height}[vcodec^=avc1]+ba[acodec^=mp4a]/b{height}[vcodec^=avc1][acodec^=mp4a]"
                f"/b{height}[ext=mp4][vcodec^=?avc1][acodec^=?mp4a]")
    if profile == "audio":
        return "bestaudio/best"
    raise EngineError("Unbekanntes Ausgabeprofil.", "validation")


class Engine:
    def __init__(self, executable: str = "yt-dlp", *, timeout: float = 7200,
                 probe_timeout: float = 90, max_output: int = 64 * 1024 * 1024,
                 max_line: int = 1024 * 1024, cancel_grace: float = 4,
                 ffprobe_executable: str = "ffprobe"):
        self.executable = executable
        self.timeout = timeout
        self.probe_timeout = probe_timeout
        self.max_output = max_output
        self.max_line = max_line
        self.cancel_grace = cancel_grace
        self.ffprobe_executable = ffprobe_executable

    def common_args(self) -> list[str]:
        return [self.executable, "--ignore-config", "--no-cache-dir", "--no-colors",
                "--no-warnings", "--socket-timeout", "20", "--retries", "3",
                "--fragment-retries", "3", "--extractor-retries", "2"]

    async def validate_dns(self, url: str) -> None:
        validate_source_url(url)
        host = urlsplit(url).hostname
        try:
            async with asyncio.timeout(10):
                addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise EngineError("Die Quelle verweist auf eine interne oder gesperrte Netzwerkadresse.", "validation")
        except (OSError, TimeoutError) as exc:
            raise EngineError("Die Quelladresse konnte nicht aufgelöst werden.", "network") from exc

    async def probe(self, url: str, work_dir: Path, *, cancel: asyncio.Event | None = None,
                    playlist_limit: int = 50) -> dict[str, Any]:
        validate_source_url(url)
        await self.validate_dns(url)
        if not 1 <= playlist_limit <= 100:
            raise EngineError("Ungültige Playlistgrenze.", "validation")
        # --flat-playlist avoids downloading all media metadata for a channel.
        args = self.common_args() + ["--dump-single-json", "--skip-download",
            "--flat-playlist", "--playlist-end", str(playlist_limit), "--", url]
        lines: list[str] = []

        async def receive(line: str) -> None:
            if line.strip():
                lines.append(line)

        await self._run(args, work_dir, receive, cancel, self.probe_timeout)
        try:
            info = json.loads("\n".join(lines))
        except (ValueError, TypeError) as exc:
            raise EngineError("Die Quelle hat keine gültigen Metadaten geliefert.", "metadata") from exc
        if not isinstance(info, dict):
            raise EngineError("Ungültige Metadaten der Quelle.", "metadata")
        result = public_metadata(info)
        entries = info.get("entries")
        if isinstance(entries, list):
            result["entries"] = [
                {**public_metadata(item), "url": str(item.get("webpage_url") or item.get("url") or "")}
                for item in entries[:playlist_limit] if isinstance(item, dict)
            ]
            result["type"] = "playlist"
            result["limited"] = len(entries) >= playlist_limit
        else:
            result["type"] = "video"
        return result

    async def download(self, url: str, work_dir: Path, *, profile: str = "original",
                       max_height: int = 1080, cancel: asyncio.Event | None = None,
                       on_event: EventCallback | None = None) -> DownloadResult:
        validate_source_url(url)
        await self.validate_dns(url)
        selector = format_selector(profile, max_height)
        args = self.common_args() + ["--no-playlist", "--no-simulate", "--newline", "--progress", "--progress-delta", "1",
            "--continue", "--part", "--no-overwrites", "--max-filesize", "20G",
            "--paths", str(work_dir), "--output", "media.%(ext)s", "--format", selector,
            # Render a projected object, not individual JSON values: missing
            # individual fields render as bare NA even with the `j` conversion.
            "--print", "before_dl:VELA_META:" + METADATA_TEMPLATE,
            "--print", "after_move:VELA_FILE:%(filepath)j",
            "--progress-template", "download:VELA_PROGRESS:" + PROGRESS_TEMPLATE,
            "--progress-template", "postprocess:VELA_POSTPROCESS:{}"]
        if profile == "compatible":
            args += ["--merge-output-format", "mp4"]
        elif profile == "audio":
            args += ["--extract-audio", "--audio-format", "best"]
        args += ["--", url]
        result = DownloadResult()
        root = work_dir.resolve()

        async def receive(line: str) -> None:
            kind, sep, payload = line.partition(":")
            if not sep or kind not in ("VELA_META", "VELA_FILE", "VELA_PROGRESS", "VELA_POSTPROCESS"):
                return
            try:
                value = json.loads(payload)
            except ValueError as exc:
                raise EngineError("Ungültige Antwort der Download-Engine.", "protocol") from exc
            if kind == "VELA_META" and isinstance(value, dict):
                result.metadata = public_metadata(value)
                if on_event:
                    await on_event("metadata", result.metadata)
            elif kind == "VELA_FILE" and isinstance(value, str):
                path = Path(value).resolve()
                if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
                    raise EngineError("Die Engine meldete einen ungültigen Dateipfad.", "path")
                if path not in result.files:
                    result.files.append(path)
            elif kind == "VELA_PROGRESS" and isinstance(value, dict) and on_event:
                safe = {key: value.get(key) for key in ("status", "downloaded_bytes", "total_bytes", "total_bytes_estimate", "speed", "eta", "fragment_index", "fragment_count")}
                await on_event("progress", safe)
            elif kind == "VELA_POSTPROCESS" and on_event:
                await on_event("postprocessing", {})

        await self._run(args, work_dir, receive, cancel, self.timeout)
        if not result.files:
            raise EngineError("Der Download lieferte keine fertige Datei.", "missing_file")
        if profile == "compatible":
            for file in result.files:
                await self.verify_compatible(file, work_dir, cancel=cancel)
        return result

    async def verify_compatible(self, file: Path, work_dir: Path,
                                *, cancel: asyncio.Event | None = None) -> None:
        """Verify real codecs, including direct media with missing source metadata."""
        lines: list[str] = []

        async def receive(line: str) -> None:
            lines.append(line)

        await self._run([self.ffprobe_executable, "-v", "error", "-show_entries",
            "stream=codec_type,codec_name", "-of", "json", str(file)], work_dir,
            receive, cancel, min(30, self.probe_timeout))
        try:
            streams = json.loads("\n".join(lines))["streams"]
            videos = [stream for stream in streams if stream.get("codec_type") == "video"]
            audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
            compatible = bool(videos) and all(stream.get("codec_name") == "h264" for stream in videos) and all(stream.get("codec_name") == "aac" for stream in audios)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise EngineError("Die Codecs der fertigen Datei konnten nicht geprüft werden.", "format") from exc
        if not compatible:
            raise EngineError("Die Quelle bietet für dieses Profil kein H.264-Video mit AAC-Audio. Es wurde keine Konvertierung ausgeführt.", "format")

    async def _run(self, args: list[str], work_dir: Path,
                   receive: Callable[[str], Awaitable[None]], cancel: asyncio.Event | None,
                   timeout: float) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            proc = await asyncio.create_subprocess_exec(*args, cwd=work_dir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=self.max_line, **kwargs)
        except OSError as exc:
            raise EngineError("Die Download-Engine konnte nicht gestartet werden.", "engine_start") from exc
        marker = work_dir / ".vela-process.json"
        try:
            marker.write_text(json.dumps({"pid": proc.pid, "identity": _process_identity(proc.pid)}), encoding="utf-8")
        except OSError as exc:
            await self._stop(proc)
            raise EngineError("Das Prozessprotokoll konnte nicht gespeichert werden.", "storage") from exc
        output_bytes = 0
        errors: list[str] = []

        async def drain(stream: asyncio.StreamReader, stdout: bool) -> None:
            nonlocal output_bytes
            while True:
                try:
                    raw = await stream.readline()
                except ValueError as exc:
                    raise EngineError("Engine-Ausgabe überschreitet das Zeilenlimit.", "output_limit") from exc
                if not raw:
                    break
                output_bytes += len(raw)
                if output_bytes > self.max_output:
                    raise EngineError("Engine-Ausgabe überschreitet das Größenlimit.", "output_limit")
                text = raw.decode("utf-8", errors="replace").strip()
                if stdout:
                    await receive(text)
                elif "ERROR:" in text:
                    # Store categories only. Remote URLs may contain secrets.
                    errors.append(self._error_category(text))

        async def watch_cancel() -> None:
            if cancel is None:
                await asyncio.Future()
            else:
                await cancel.wait()
                raise EngineCancelled()

        tasks = [asyncio.create_task(drain(proc.stdout, True)),
                 asyncio.create_task(drain(proc.stderr, False)),
                 asyncio.create_task(proc.wait()), asyncio.create_task(watch_cancel())]
        try:
            async with asyncio.timeout(timeout):
                pending = set(tasks[:3])
                while pending:
                    done, _ = await asyncio.wait(pending | {tasks[3]}, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                        pending.discard(task)
                if cancel and cancel.is_set():
                    raise EngineCancelled()
                if proc.returncode:
                    category = errors[-1] if errors else "download"
                    messages = {"format": "Für dieses Profil ist kein passendes Format verfügbar.",
                        "authentication": "Die Quelle erfordert eine Anmeldung oder zusätzliche Freigabe.",
                        "unavailable": "Das Medium ist nicht verfügbar.",
                        "network": "Die Quelle konnte nicht erreicht werden.",
                        "storage": "Die Arbeitsdatei konnte nicht gespeichert werden."}
                    raise EngineError(messages.get(category, "Die Quelle konnte nicht heruntergeladen werden."), category)
        except TimeoutError as exc:
            raise EngineError("Zeitlimit der Download-Engine erreicht.", "timeout") from exc
        finally:
            await self._stop(proc)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            marker.unlink(missing_ok=True)

    async def _stop(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None and os.name == "nt":
            return
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.cancel_grace)
        except TimeoutError:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await proc.wait()
        finally:
            if os.name != "nt":
                # The group may still contain FFmpeg after the yt-dlp leader
                # has exited. Never leave those children behind.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @staticmethod
    def _error_category(message: str) -> str:
        text = message.lower()
        if "requested format" in text or "no video formats" in text:
            return "format"
        if any(word in text for word in ("sign in", "login", "cookies", "not a bot", "403", "private video")):
            return "authentication"
        if any(word in text for word in ("not available", "unavailable", "404", "removed")):
            return "unavailable"
        if any(word in text for word in ("no space", "permission denied", "read-only file")):
            return "storage"
        if any(word in text for word in ("timed out", "network", "connection", "resolve")):
            return "network"
        return "download"
