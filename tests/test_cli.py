# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import pytest

from camera_sign.cli import camera_in_use, parse_args


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
