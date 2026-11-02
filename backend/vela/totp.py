"""Restricted Instagram provisioning parser and RFC 6238 SHA-1 TOTP in RAM."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import math
import re
import struct
import time
from urllib.parse import parse_qsl, unquote, urlsplit


class TOTPError(ValueError):
    def __init__(self):
        super().__init__("Der Einrichtungsschlüssel ist ungültig oder passt nicht zu Instagram (SHA1, 6 Ziffern, 30 Sekunden).")


def _normalize_base32(value: str) -> str:
    normalized=re.sub(r"[\s-]","",value).upper()
    if not re.fullmatch(r"[A-Z2-7]{16,208}={0,6}",normalized):raise TOTPError()
    unpadded=normalized.rstrip("=")
    padded=unpadded+"="*((-len(unpadded))%8)
    if "=" in normalized and normalized!=padded:raise TOTPError()
    try:key=base64.b32decode(padded,casefold=False)
    except Exception:raise TOTPError() from None
    if not 10<=len(key)<=128 or base64.b32encode(key).decode().rstrip("=")!=unpadded:raise TOTPError()
    return unpadded


def parse_secret(value: str) -> str:
    if not isinstance(value,str) or not value or len(value)>4096:raise TOTPError()
    value=value.strip()
    if value.lower().startswith("otpauth-migration:"):
        from .totp_migration import parse_google_migration
        try:return _normalize_base32(parse_google_migration(value))
        except Exception:raise TOTPError() from None
    if ":" not in value:return _normalize_base32(value)
    try:
        parsed=urlsplit(value)
        if parsed.scheme.lower()!="otpauth" or parsed.netloc!="totp" or parsed.fragment or not parsed.path.startswith("/") or not parsed.path[1:]:raise TOTPError()
        pairs=parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True)
        parameters=dict(pairs)
        if len(parameters)!=len(pairs) or parameters.keys()-{"secret","issuer","algorithm","digits","period"}:raise TOTPError()
        if parameters.get("algorithm","SHA1").upper()!="SHA1" or parameters.get("digits","6")!="6" or parameters.get("period","30")!="30":raise TOTPError()
        label=unquote(parsed.path[1:])
        if label.count(":")>1:raise TOTPError()
        issuers=([label.split(":",1)[0]] if ":" in label else [])+([parameters["issuer"]] if "issuer" in parameters else [])
        if any(issuer.strip().casefold()!="instagram" for issuer in issuers):raise TOTPError()
        return _normalize_base32(parameters["secret"])
    except TOTPError:raise
    except Exception:raise TOTPError() from None


def generate_totp(secret: str, timestamp: float | None = None) -> str:
    timestamp=time.time() if timestamp is None else timestamp
    if not isinstance(timestamp,(int,float)) or not math.isfinite(timestamp) or timestamp<0:raise TOTPError()
    normalized=parse_secret(secret)
    key=base64.b32decode(normalized+"="*((-len(normalized))%8))
    try:counter=struct.pack(">Q",int(timestamp//30))
    except (OverflowError,struct.error):raise TOTPError() from None
    digest=hmac.new(key,counter,hashlib.sha1).digest()
    offset=digest[-1]&0x0f
    value=int.from_bytes(digest[offset:offset+4],"big")&0x7fffffff
    return str(value%1_000_000).zfill(6)


async def fresh_totp(secret: str, *, clock=None, sleep=None) -> str:
    clock=clock or time.time
    sleep=sleep or asyncio.sleep
    timestamp=clock()
    remaining=30-(timestamp%30)
    if remaining<5:
        await sleep(remaining+.05)
        timestamp=clock()
    return generate_totp(secret,timestamp)
