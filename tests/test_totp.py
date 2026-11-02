"""Public RFC vectors and synthetic setup keys only; no external requests."""
import asyncio
import base64
import json
import time

import pytest

from vela.browser_service import AccountVault, BrowserController, BrowserSettings
from vela.db import Database
from vela.instagram_auth import InstagramAuthState
from vela.totp import TOTPError,fresh_totp,generate_totp,parse_secret


RFC_KEY=base64.b32encode(b"12345678901234567890").decode().rstrip("=")


@pytest.fixture(autouse=True)
def local_sqlite(monkeypatch):monkeypatch.setenv("VELA_ALLOW_UNPATCHED_SQLITE","1")


@pytest.mark.parametrize("timestamp,expected",[(59,"287082"),(1111111109,"081804"),(1111111111,"050471"),(1234567890,"005924"),(2000000000,"279037"),(20000000000,"353130")])
def test_rfc6238_sha1_vectors_truncated_to_six_digits(timestamp,expected):
    assert generate_totp(RFC_KEY,timestamp)==expected


def test_base32_and_instagram_otpauth_normalize_without_account_label():
    grouped=" ".join(RFC_KEY.lower()[index:index+4] for index in range(0,len(RFC_KEY),4))
    assert parse_secret(grouped)==RFC_KEY
    uri=f"otpauth://totp/Instagram:synthetic-account?secret={RFC_KEY}&issuer=Instagram&algorithm=SHA1&digits=6&period=30"
    assert parse_secret(uri)==RFC_KEY
    assert parse_secret(f"otpauth://totp/synthetic-account?secret={RFC_KEY}")==RFC_KEY


@pytest.mark.parametrize("value",[
    "123456","synthetic-invalid-private-value", "A"*4097,
    f"otpauth://hotp/Instagram:synthetic?secret={RFC_KEY}&counter=0",
    f"otpauth://totp/Other:synthetic?secret={RFC_KEY}",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&issuer=Other",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&algorithm=SHA256",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&digits=8",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&period=60",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&secret={RFC_KEY}",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&counter=1",
    f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}#fragment",
])
def test_parser_fails_closed_and_never_echoes_input(value):
    with pytest.raises(TOTPError) as error:parse_secret(value)
    assert value not in str(error.value) and RFC_KEY not in str(error.value) and "synthetic" not in str(error.value)


@pytest.mark.parametrize("timestamp",[float("nan"),float("inf"),-1,1e30])
def test_invalid_clock_is_rejected(timestamp):
    with pytest.raises(TOTPError):generate_totp(RFC_KEY,timestamp)


def test_near_boundary_waits_for_next_slot_without_returning_expiring_code():
    async def run():
        current=[58.0];sleeps=[]
        async def sleep(seconds):sleeps.append(seconds);current[0]+=seconds
        code=await fresh_totp(RFC_KEY,clock=lambda:current[0],sleep=sleep)
        assert len(sleeps)==1 and sleeps[0]==pytest.approx(2.05)
        assert code==generate_totp(RFC_KEY,60.05)
        current[0]=55.0;sleeps.clear()
        assert await fresh_totp(RFC_KEY,clock=lambda:current[0],sleep=sleep)==generate_totp(RFC_KEY,55)
        assert sleeps==[]
    asyncio.run(run())


def test_vault_legacy_format_seed_updates_and_account_switch(tmp_path):
    vault=AccountVault(tmp_path/"secrets")
    vault.save("synthetic-user","synthetic-password")
    assert vault.load()=={"username":"synthetic-user","password":"synthetic-password"}
    assert not vault.totp_configured()
    vault.set_totp(f"otpauth://totp/Instagram:synthetic?secret={RFC_KEY}&issuer=Instagram")
    assert vault.totp_configured() and vault.load()["totp_secret"]==RFC_KEY
    for path in vault.directory.iterdir():
        assert RFC_KEY.encode() not in path.read_bytes() and b"synthetic-password" not in path.read_bytes()
    vault.save("SYNTHETIC-USER","synthetic-new-password")
    assert vault.totp_configured() and vault.load()["password"]=="synthetic-new-password"
    vault.remove_totp();assert not vault.totp_configured()
    assert vault.load()["password"]=="synthetic-new-password"
    vault.set_totp(RFC_KEY);vault.save("synthetic-other-user","synthetic-other-password")
    assert not vault.totp_configured()


def test_old_state_table_upgrades_and_otp_attempt_survives_restart(tmp_path):
    folder=tmp_path/"state";folder.mkdir()
    database=Database(folder/"instagram-auth.sqlite3")
    old={"configured":True,"state":"idle","message":"old-static","request_id":7,"completed_request_id":6,"session_generation":3,"browser_available":False,"interaction_id":None,"updated_at":None}
    with database.connection() as connection:
        connection.execute("CREATE TABLE instagram_auth(id INTEGER PRIMARY KEY,value TEXT NOT NULL,last_requested REAL NOT NULL DEFAULT 0,browser_seen REAL NOT NULL DEFAULT 0,login_attempted INTEGER NOT NULL DEFAULT 0)")
        connection.execute("INSERT INTO instagram_auth(id,value) VALUES(1,?)",(json.dumps(old),))
    state=InstagramAuthState(folder);state.initialize();state.publish(totp_configured=True)
    assert state.status()["request_id"]==7 and state.totp_attempt_available(7)
    assert state.consume_totp_attempt(7)
    restarted=InstagramAuthState(folder);restarted.initialize()
    assert not restarted.consume_totp_attempt(7) and not restarted.totp_attempt_available(7)
    restarted.finish(7,"error");assert restarted.request_refresh()==8
    assert restarted.consume_totp_attempt(8)


def test_controller_seed_save_remove_preserves_profile_cookie_password_and_requests(tmp_path):
    async def run():
        settings=BrowserSettings(state_dir=tmp_path/"state",credentials_dir=tmp_path/"credentials",secrets_dir=tmp_path/"secrets",profile_dir=tmp_path/"profile",control_socket=tmp_path/"control"/"control.sock")
        control=BrowserController(settings)
        control.vault.save("synthetic-user","synthetic-password");control.state.configure(True)
        settings.profile_dir.mkdir(mode=0o700);profile=settings.profile_dir/"synthetic-profile";profile.write_bytes(b"synthetic-retained-profile")
        settings.credentials_dir.mkdir(mode=0o700);jar=settings.credentials_dir/"instagram.cookies.txt";jar.write_bytes(b"synthetic-retained-cookie")
        before=control.state.status()
        saved=await control.set_totp(RFC_KEY)
        assert saved["totp_configured"] and saved["request_id"]==before["request_id"] and saved["session_generation"]==before["session_generation"]
        assert control.login_task is None and control.page is None
        assert RFC_KEY not in json.dumps(saved)
        removed=await control.remove_totp()
        assert not removed["totp_configured"] and removed["request_id"]==before["request_id"]
        assert control.vault.load()=={"username":"synthetic-user","password":"synthetic-password"}
        assert profile.read_bytes()==b"synthetic-retained-profile" and jar.read_bytes()==b"synthetic-retained-cookie"
        for path in settings.state_dir.iterdir():
            if path.is_file():assert RFC_KEY.encode() not in path.read_bytes()
    asyncio.run(run())
