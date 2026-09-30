# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio

import pytest
from kasa import AuthenticationError

from camera_sign import cli
from camera_sign.cli import Plug, camera_in_use, parse_args


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
