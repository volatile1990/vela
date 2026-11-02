"""Only public RFC test seeds and synthetic account labels are used here."""
import base64
import random
from urllib.parse import quote

import pytest

from vela.totp import TOTPError, generate_totp, parse_secret
from vela.totp_migration import MigrationError, parse_google_migration


RFC_SECRET = b"12345678901234567890"
EXPECTED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def integer(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def entry(*, secret=RFC_SECRET, name="Instagram:synthetic", issuer="Instagram", algorithm=1, digits=1, kind=2, extra=b""):
    result = blob(1, secret) + blob(2, name.encode()) + blob(3, issuer.encode())
    if algorithm is not None:
        result += integer(4, algorithm)
    if digits is not None:
        result += integer(5, digits)
    if kind is not None:
        result += integer(6, kind)
    return result + extra


def payload(entries=None, *, version=1, batch_size=1, batch_index=0, extra=b""):
    result = b"".join(blob(1, value) for value in (entries if entries is not None else [entry()]))
    if version is not None:
        result += integer(2, version)
    if batch_size is not None:
        result += integer(3, batch_size)
    if batch_index is not None:
        result += integer(4, batch_index)
    return result + extra


def uri(data=None):
    encoded = base64.b64encode(payload() if data is None else data).decode()
    return "otpauth-migration://offline?data=" + quote(encoded, safe="")


def test_known_wire_fixture_decodes_only_secret_and_matches_standard_totp():
    # Field tags and lengths are fixed independently of the test encoder.
    fixed = bytes.fromhex(
        "0a3c0a143132333435363738393031323334353637383930"
        "1213496e7374616772616d3a73796e746865746963"
        "1a09496e7374616772616d200128013002100118012000")
    assert fixed == payload()
    assert parse_google_migration(uri(fixed)) == EXPECTED
    assert parse_secret(uri(fixed)) == EXPECTED
    assert generate_totp(parse_google_migration(uri(fixed)), 59) == "287082"


@pytest.mark.parametrize("version", [1, 2])
def test_supported_versions_and_optional_identifier_are_discarded(version):
    value = uri(payload([entry(extra=blob(8, b"synthetic-opaque-identifier"))], version=version))
    assert parse_google_migration(value) == EXPECTED


@pytest.mark.parametrize("issuer,name", [("Instagram", "synthetic"), ("", "Instagram:synthetic"), ("instagram", "INSTAGRAM:synthetic")])
def test_instagram_scope_is_explicit_and_case_insensitive(issuer, name):
    assert parse_google_migration(uri(payload([entry(issuer=issuer, name=name)]))) == EXPECTED


def test_base64_plus_is_not_form_decoded_to_space_and_urlsafe_encoding_works():
    data = payload([entry(secret=b"\xfb" * 20)])
    encoded = base64.b64encode(data).decode()
    assert "+" in encoded or "/" in encoded
    expected = base64.b32encode(b"\xfb" * 20).decode().rstrip("=")
    assert parse_google_migration("otpauth-migration://offline?data=" + encoded) == expected
    assert parse_google_migration(uri(data)) == expected
    urlsafe = base64.urlsafe_b64encode(data).decode().rstrip("=")
    assert parse_google_migration("otpauth-migration://offline?data=" + urlsafe) == expected


@pytest.mark.parametrize("size", [10, 128])
def test_secret_size_boundaries(size):
    secret = b"s" * size
    assert parse_google_migration(uri(payload([entry(secret=secret)]))) == base64.b32encode(secret).decode().rstrip("=")


@pytest.mark.parametrize("value", [
    None, 1, [], "", " " * 4097, "https://example.invalid/", "otpauth://totp/synthetic?secret=synthetic",
    "otpauth-migration://encrypted?data=AAAA", "otpauth-migration://offline:80?data=AAAA",
    "otpauth-migration://user@offline?data=AAAA", "otpauth-migration://offline/path?data=AAAA",
    "otpauth-migration://offline?data=AAAA#fragment", "otpauth-migration://offline?data=AAAA&data=AAAA",
    "otpauth-migration://offline?data=AAAA&key=synthetic", "otpauth-migration://offline?other=AAAA",
    "otpauth-migration://offline?data=", "otpauth-migration://offline?data=%zz",
    "otpauth-migration://offline?data=A", "otpauth-migration://offline?data=**",
    "otpauth-migration://offline?data=AA=AA", "otpauth-migration://offline?data=AB==",
])
def test_invalid_or_opaque_uri_is_rejected_without_echo(value):
    with pytest.raises(TOTPError) as error:
        parse_google_migration(value)
    assert "data=" not in str(error.value)
    assert "synthetic" not in repr(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("data", [
    payload([], version=1), payload([entry(), entry()]),
    payload(batch_size=2), payload(batch_size=None), payload(batch_index=1),
    payload(version=0), payload(version=3), payload(version=None),
    payload(extra=integer(2, 1)), payload(extra=blob(6, b"unsupported-layout")),
    payload([entry(algorithm=2)]), payload([entry(algorithm=0)]), payload([entry(algorithm=None)]),
    payload([entry(digits=2)]), payload([entry(digits=0)]), payload([entry(digits=None)]),
    payload([entry(kind=1)]), payload([entry(kind=0)]), payload([entry(kind=None)]),
    payload([entry(extra=integer(7, 1))]), payload([entry(extra=integer(9, 60))]),
    payload([entry(issuer="Other", name="Instagram:synthetic")]),
    payload([entry(issuer="Instagram", name="Other:synthetic")]),
    payload([entry(issuer="", name="synthetic")]),
    payload([entry(secret=b"x" * 9)]), payload([entry(secret=b"x" * 129)]),
    payload([entry(extra=blob(1, b"another-synthetic-seed"))]),
    payload([entry(extra=blob(8, b"x" * 257))]),
    b"\0", b"\x0a\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01", b"\x80" * 11,
    b"\x0a\x7f\x00", payload() + b"\x10", payload()[:-1],
])
def test_ambiguous_multi_account_unsupported_and_malformed_payloads_fail_closed(data):
    encoded = uri(data)
    with pytest.raises(TOTPError) as error:
        parse_google_migration(encoded)
    output = str(error.value) + repr(error.value)
    assert encoded not in output
    assert "synthetic" not in output
    assert EXPECTED not in output
    assert RFC_SECRET.decode() not in output


def test_exception_reason_is_never_a_secret_channel():
    error = MigrationError("synthetic-secret-instead-of-reason")
    assert "synthetic-secret" not in str(error)
    assert "synthetic-secret" not in repr(error)


def test_bounded_random_wire_inputs_never_escape_static_parser_errors():
    generator = random.Random(42)
    for _ in range(500):
        data = generator.randbytes(generator.randrange(1, 80))
        with pytest.raises(TOTPError):
            parse_google_migration(uri(data))
