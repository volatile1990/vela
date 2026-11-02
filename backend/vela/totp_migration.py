"""Local, narrow reader for a legacy Google Authenticator migration URI.

Format evidence (primary implementation, not a current Google API guarantee):
https://github.com/beemdevelopment/Aegis/blob/master/app/src/main/proto/google_auth.proto
https://github.com/beemdevelopment/Aegis/blob/master/app/src/main/java/com/beemdevelopment/aegis/otp/GoogleAuthInfo.java
Known optional identifier field 8 (never returned):
https://github.com/beemdevelopment/Aegis/pull/1682
Wire format: https://protobuf.dev/programming-guides/encoding/

Only one explicitly identified Instagram SHA1/six-digit TOTP is accepted.
The supported migration schema has no period field; TOTP uses 30 seconds,
matching the reference importer. Unknown layouts and batch exports fail closed.
No networking, QR service, files, logging, or account names leave this module.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import re
from urllib.parse import unquote_to_bytes, urlsplit

from .totp import TOTPError


INVALID = "Dieser Google-Authenticator-Export ist ungültig oder wird nicht unterstützt."
SINGLE = "Bitte exportiere in Google Authenticator genau einen Instagram-Eintrag in einem einzelnen QR-Code."
PROFILE = "Unterstützt wird nur ein Instagram-TOTP-Eintrag mit SHA-1 und sechs Ziffern."
UNSUPPORTED = "Dieses Exportformat wird nicht unterstützt. Verwende einen kompatiblen Google-Authenticator-Export für genau einen Instagram-Eintrag."
MAX_URI = 4096
MAX_PAYLOAD = 3072


class MigrationError(TOTPError):
    def __init__(self, reason: str = "invalid"):
        messages = {"invalid": INVALID, "single": SINGLE, "profile": PROFILE, "unsupported": UNSUPPORTED}
        # Retain the shared TOTPError type while permitting only static text.
        ValueError.__init__(self, messages.get(reason, INVALID))


class _Malformed(Exception):
    pass


class _UnknownFormat(Exception):
    pass


@dataclass(frozen=True, repr=False)
class _Parameters:
    secret: bytes
    name: str
    issuer: str


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise _Malformed()
        octet = data[offset]
        offset += 1
        if index == 9 and octet > 1:
            raise _Malformed()
        value |= (octet & 0x7f) << (index * 7)
        if not octet & 0x80:
            return value, offset
    raise _Malformed()


def _fields(data: bytes, schema: dict[int, int], *, repeated: int | None = None) -> dict[int, list[bytes | int]]:
    result: dict[int, list[bytes | int]] = {}
    offset = count = 0
    while offset < len(data):
        count += 1
        if count > 32:
            raise _Malformed()
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise _Malformed()
        if number not in schema:
            raise _UnknownFormat()
        if wire != schema[number] or (number in result and number != repeated):
            raise _Malformed()
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            length, offset = _varint(data, offset)
            if length > len(data) - offset:
                raise _Malformed()
            value = data[offset:offset + length]
            offset += length
        else:
            raise _Malformed()
        result.setdefault(number, []).append(value)
        if number == repeated and len(result[number]) > 1:
            raise MigrationError("single")
    return result


def _text(value: bytes | int, limit: int) -> str:
    if not isinstance(value, bytes) or len(value) > limit:
        raise _Malformed()
    text = value.decode("utf-8")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise _Malformed()
    return text


def _parse(uri: str) -> str:
    if not isinstance(uri, str) or not 1 <= len(uri) <= MAX_URI:
        raise _Malformed()
    uri = uri.strip()
    if any(not 33 <= ord(character) <= 126 for character in uri):
        raise _Malformed()
    parsed = urlsplit(uri)
    if parsed.scheme != "otpauth-migration" or parsed.netloc.casefold() != "offline" or parsed.path not in {"", "/"} or parsed.fragment:
        raise _UnknownFormat()
    # unquote_to_bytes deliberately preserves literal '+': it is part of
    # Base64, not form-encoded whitespace in a QR migration URI.
    if not parsed.query.startswith("data=") or "&" in parsed.query:
        raise _Malformed()
    encoded = parsed.query[5:]
    if not encoded or re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
        raise _Malformed()
    encoded = unquote_to_bytes(encoded).decode("ascii")
    if not re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", encoded) or len(encoded) % 4 == 1:
        raise _Malformed()
    payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    canonical = base64.b64encode(payload).decode("ascii").rstrip("=")
    if canonical != encoded.translate(str.maketrans("-_", "+/")).rstrip("=") or not 1 <= len(payload) <= MAX_PAYLOAD:
        raise _Malformed()
    outer = _fields(payload, {1: 2, 2: 0, 3: 0, 4: 0, 5: 0}, repeated=1)
    if outer.get(2) not in ([1], [2]):
        raise _UnknownFormat()
    if len(outer.get(1, [])) != 1 or outer.get(3) != [1] or outer.get(4, [0]) != [0]:
        raise MigrationError("single")
    parameters = _fields(outer[1][0], {1: 2, 2: 2, 3: 2, 4: 0, 5: 0, 6: 0, 7: 0, 8: 2})
    if parameters.get(4) != [1] or parameters.get(5) != [1] or parameters.get(6) != [2] or parameters.get(7, [0]) != [0]:
        raise MigrationError("profile")
    secret = parameters.get(1, [b""])[0]
    if not isinstance(secret, bytes) or not 10 <= len(secret) <= 128:
        raise _Malformed()
    item = _Parameters(secret, _text(parameters.get(2, [b""])[0], 1024), _text(parameters.get(3, [b""])[0], 128))
    if 8 in parameters:
        _text(parameters[8][0], 256)  # Known opaque metadata, never retained.
    issuer = item.issuer.strip().casefold()
    label_prefix = item.name.split(":", 1)[0].strip().casefold() if ":" in item.name else ""
    label_is_instagram = item.name.strip().casefold() == "instagram"
    if ((issuer and issuer != "instagram") or (label_prefix and label_prefix != "instagram")
            or not (issuer == "instagram" or label_prefix == "instagram" or label_is_instagram)):
        raise MigrationError("single")
    return base64.b32encode(item.secret).decode("ascii").rstrip("=")


def parse_google_migration(uri: str) -> str:
    """Return only a canonical secret; errors contain no URI or account data."""
    try:
        return _parse(uri)
    except TOTPError:
        raise
    except _UnknownFormat:
        raise MigrationError("unsupported") from None
    except (_Malformed, ValueError, UnicodeError, binascii.Error, OverflowError, TypeError):
        raise MigrationError() from None
