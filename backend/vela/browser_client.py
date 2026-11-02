"""Finite API-to-browser protocol over a local Unix socket; no browser secrets return."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re

import httpx
from fastapi import HTTPException


MESSAGES = {
    "unconfigured": "Noch kein Instagram-Zugang hinterlegt.",
    "idle": "Der Zugang ist hinterlegt. Vela meldet sich bei Bedarf an.",
    "logging_in": "Vela meldet sich bei Instagram an …",
    "ready": "Die Instagram-Sitzung ist bereit.",
    "needs_attention": "Instagram benötigt deine Bestätigung. Öffne die Anmeldung unten.",
    "error": "Die Anmeldung konnte nicht abgeschlossen werden. Prüfe sie im Anmeldefenster oder versuche es später erneut.",
}


def public_status(value: dict) -> dict:
    """Whitelist metadata rather than returning arbitrary browser text or identities."""
    if not isinstance(value, dict):
        raise ValueError("Invalid browser state")
    state = value.get("state")
    if not isinstance(state, str) or state not in MESSAGES:
        raise ValueError("Invalid browser state")
    result = {"configured": value.get("configured") is True, "state": state,
              "message": MESSAGES[state], "browser_available": value.get("browser_available") is True}
    for key in ("session_generation", "request_id", "completed_request_id"):
        number = value.get(key, 0)
        if type(number) is not int or not 0 <= number < 2**53:
            raise ValueError("Invalid browser metadata")
        result[key] = number
    interaction = value.get("interaction_id")
    if interaction is not None and (not isinstance(interaction, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", interaction)):
        raise ValueError("Invalid interaction")
    result["interaction_id"] = interaction
    stamp = value.get("updated_at")
    result["updated_at"] = datetime.fromisoformat(stamp.replace("Z", "+00:00")).isoformat() if isinstance(stamp, str) and len(stamp) <= 40 else None
    return result


class BrowserClient:
    def __init__(self, socket: Path):
        self.socket = socket

    async def request(self, method: str, path: str, payload: dict | None = None, *, screen: bool = False):
        # All callers use fixed paths. No URL, host or headers from a user are forwarded.
        if path not in {"/status", "/account", "/login", "/action", "/screen"}:
            raise ValueError("Invalid browser operation")
        limit = 3 * 1024**2 if screen else 64 * 1024
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(self.socket), retries=0)
            timeout = 30 if path == "/account" else 12
            async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout, connect=2), trust_env=False) as client:
                async with client.stream(method, "http://vela-browser" + path, json=payload) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise ValueError("Browser response too large")
                    if response.status_code != 200:
                        # Never return a traceback, page text or arbitrary service error.
                        code = response.status_code if response.status_code in {409, 422, 429} else 503
                        message = {409: "Die Anmeldung hat sich geändert. Lade ihren aktuellen Stand neu.",
                                   422: "Diese Eingabe ist nicht gültig.",
                                   429: "Bitte warte vor einem weiteren Anmeldeversuch."}.get(code, "Der Instagram-Browserdienst ist gerade nicht verfügbar.")
                        raise HTTPException(code, message)
                    if screen:
                        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("Invalid browser image")
                        return bytes(content)
                    return public_status(json.loads(content))
        except HTTPException:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, "Der Instagram-Browserdienst ist gerade nicht verfügbar.") from None
