# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio

import pytest
from kasa import AuthenticationError

from camera_sign import cli
from camera_sign.cli import Phone, Plug, camera_in_use, parse_args, phone_cameras_open


def test_camera_in_use(tmp_path):
    refcnt = tmp_path / "refcnt"
    refcnt.write_text("0\n")
    assert camera_in_use(refcnt) is False
    refcnt.write_text("2\n")
    assert camera_in_use(refcnt) is True


def test_driver_not_loaded(tmp_path):
    assert camera_in_use(tmp_path / "missing") is False


def test_host_from_environment(monkeypatch):
    monkeypatch.setenv("CAMERA_SIGN_HOST", "10.0.0.9")
    assert parse_args([]).host == "10.0.0.9"


def test_host_required(monkeypatch):
    monkeypatch.delenv("CAMERA_SIGN_HOST", raising=False)
    with pytest.raises(SystemExit):
        parse_args(["watch"])
    assert parse_args(["status"]).command == "status"


class RefusingDevice:
    """A plug that answers discovery but rejects the login."""

    def __init__(self):
        self.disconnected = False

    async def update(self):
        raise AuthenticationError("Device response did not match our challenge")

    async def disconnect(self):
        self.disconnected = True


def test_failed_login_releases_the_session(monkeypatch):
    device = RefusingDevice()

    async def discover_single(host, **kwargs):
        return device

    monkeypatch.setattr(cli.Discover, "discover_single", discover_single)
    assert asyncio.run(Plug("10.0.0.9", "user", "wrong").set(True)) is False
    assert device.disconnected


IDLE_DUMP = """\
Number of camera devices: 2
Active Camera Clients:
[]
Allowed user IDs: 0
"""

OPEN_DUMP = """\
Number of camera devices: 2
Active Camera Clients:
[
(Camera ID: 0, Cost: 51, PID: 28544, Score: 0, State: 2User Id: 0, \
Client Package Name: com.google.android.GoogleCamera, Conflicting Client Devices: {2, 3, })
]
Allowed user IDs: 0
"""


def test_phone_cameras_open():
    assert phone_cameras_open(IDLE_DUMP) is False
    assert phone_cameras_open(OPEN_DUMP) is True
    with pytest.raises(ValueError):
        phone_cameras_open("adb: device offline")


def fake_adb(tmp_path, monkeypatch, script):
    adb = tmp_path / "adb"
    adb.write_text("#!/bin/sh\n" + script)
    adb.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")


def test_phone_in_use(tmp_path, monkeypatch):
    dump = tmp_path / "dump"
    dump.write_text(OPEN_DUMP)
    fake_adb(tmp_path, monkeypatch, f'[ "$2" = SERIAL ] && cat {dump}\n')
    assert asyncio.run(Phone("SERIAL").in_use()) is True
    dump.write_text(IDLE_DUMP)
    assert asyncio.run(Phone("SERIAL").in_use()) is False


def test_unreachable_phone_counts_as_idle(tmp_path, monkeypatch, caplog):
    fake_adb(tmp_path, monkeypatch, "echo 'adb: device not found' >&2; exit 1\n")
    phone = Phone("SERIAL")
    assert asyncio.run(phone.in_use()) is False
    assert asyncio.run(phone.in_use()) is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "device not found" in warnings[0].getMessage()


def test_hung_phone_times_out(tmp_path, monkeypatch):
    fake_adb(tmp_path, monkeypatch, "exec sleep 10\n")
    monkeypatch.setattr(cli, "ADB_TIMEOUT", 0.2)
    assert asyncio.run(Phone("SERIAL").in_use()) is False


def test_phone_from_environment(monkeypatch):
    monkeypatch.setenv("CAMERA_SIGN_PHONE", "52221JEBF23974")
    assert parse_args(["status"]).phone == "52221JEBF23974"
    monkeypatch.setenv("CAMERA_SIGN_PHONE", "")
    assert parse_args(["status"]).phone is None


def test_wifi_phone_is_reconnected(tmp_path, monkeypatch):
    log = tmp_path / "log"
    fake_adb(tmp_path, monkeypatch, f'echo "$@" >> {log}; [ "$1" = connect ] || exit 1\n')

    async def check_twice():
        phone = Phone("10.0.0.9:5555")
        await phone.in_use()
        await phone.in_use()
        await phone._reconnect

    asyncio.run(check_twice())
    calls = log.read_text().splitlines()
    # Two failed checks, but only one reconnect inside RECONNECT_EVERY.
    assert calls.count("connect 10.0.0.9:5555") == 1
    assert len(calls) == 3
