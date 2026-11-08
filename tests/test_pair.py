# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio
import logging

import pytest

from camera_sign import pair

CONNECT_DUMP = """\
+;wlp3s0;IPv4;adb-SERIAL-xYz12;_adb-tls-connect._tcp;local
=;wlp3s0;IPv6;adb-SERIAL-xYz12;_adb-tls-connect._tcp;local;Android.local;fe80::1;40779;"name=Pixel"
=;wlp3s0;IPv4;adb-OTHER-aBc34;_adb-tls-connect._tcp;local;Other.local;10.0.0.7;33333;"name=Other"
=;wlp3s0;IPv4;adb-SERIAL-xYz12;_adb-tls-connect._tcp;local;Android.local;10.0.0.9;40779;"name=Pixel"
"""

PAIRING_DUMP = """\
=;wlp3s0;IPv4;adb-SERIAL-xYz12;_adb-tls-pairing._tcp;local;Android.local;10.0.0.9;43039;"name=Pixel"
"""


def test_endpoint_in_takes_the_resolved_ipv4_record():
    assert pair.endpoint_in(CONNECT_DUMP, "SERIAL") == "10.0.0.9:40779"
    assert pair.endpoint_in(PAIRING_DUMP, "SERIAL") == "10.0.0.9:43039"


def test_endpoint_in_ignores_another_phone():
    assert pair.endpoint_in(CONNECT_DUMP, "ABSENT") is None
    assert pair.endpoint_in("", "SERIAL") is None


def test_endpoint_in_matches_the_whole_serial():
    # The suffix after the serial changes at every restart, but a phone
    # whose serial merely starts the same must not be mistaken for it.
    assert pair.endpoint_in(CONNECT_DUMP, "SERIA") is None


def test_normalise_accepts_the_code_as_the_phone_shows_it():
    assert pair.normalise("979548") == "979548"
    assert pair.normalise("979 548\n") == "979548"
    assert pair.normalise("979-548") == "979548"


def test_normalise_rejects_what_cannot_be_a_code():
    assert pair.normalise("") is None
    assert pair.normalise("97954") is None
    assert pair.normalise("9795480") is None
    assert pair.normalise("abcdef") is None


def fake_binary(tmp_path, name, script):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def fake_avahi(tmp_path, *, connect=CONNECT_DUMP, pairing=PAIRING_DUMP):
    """Install an avahi-browse answering per service type, as the real one does."""
    connect_file = tmp_path / "connect.txt"
    connect_file.write_text(connect)
    pairing_file = tmp_path / "pairing.txt"
    pairing_file.write_text(pairing)
    fake_binary(
        tmp_path,
        "avahi-browse",
        f"for arg; do last=$arg; done\n"
        f'case "$last" in\n'
        f"  *pairing*) cat {pairing_file} ;;\n"
        f"  *) cat {connect_file} ;;\n"
        f"esac\n",
    )


def on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")


def test_advertised_finds_each_service(tmp_path, monkeypatch):
    fake_avahi(tmp_path)
    on_path(tmp_path, monkeypatch)
    assert asyncio.run(pair.advertised("SERIAL", pair.CONNECT_TYPE)) == "10.0.0.9:40779"
    assert asyncio.run(pair.advertised("SERIAL", pair.PAIRING_TYPE)) == "10.0.0.9:43039"


def test_ask_for_code_returns_what_was_typed(tmp_path, monkeypatch):
    log = tmp_path / "log"
    fake_binary(tmp_path, "kdialog", f'echo "$@" > {log}; echo "979 548"\n')
    on_path(tmp_path, monkeypatch)
    assert asyncio.run(pair.ask_for_code("SERIAL")) == "979548"
    # The phone-side steps have to reach the screen, not just the journal.
    asked = log.read_text()
    assert "Pair device with pairing code" in asked
    assert "SERIAL" in asked


def test_ask_for_code_falls_back_to_zenity(tmp_path, monkeypatch):
    # PATH replaced by a directory holding zenity alone, so kdialog is
    # genuinely missing.  Prepending would find the real kdialog that
    # this machine has and open a window no test can answer; the script
    # uses shell builtins only, so nothing else need be reachable.
    only = tmp_path / "only-zenity"
    only.mkdir()
    fake_binary(only, "zenity", "echo 979548\n")
    monkeypatch.setenv("PATH", str(only))
    assert asyncio.run(pair.ask_for_code("SERIAL")) == "979548"


def test_cancelled_dialog_gives_no_code(tmp_path, monkeypatch, caplog):
    fake_binary(tmp_path, "kdialog", "exit 1\n")
    on_path(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)  # a cancel is news, not a warning
    assert asyncio.run(pair.ask_for_code("SERIAL")) is None
    assert "cancelled" in caplog.text


def test_a_code_that_cannot_be_one_is_reported(tmp_path, monkeypatch):
    fake_binary(tmp_path, "kdialog", "echo not-a-code\n")
    on_path(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="six-digit"):
        asyncio.run(pair.ask_for_code("SERIAL"))


def test_no_dialog_at_all_says_so(tmp_path, monkeypatch):
    # PATH replaced, not prepended: neither dialog is installed here.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(LookupError, match="kdialog nor zenity"):
        asyncio.run(pair.ask_for_code("SERIAL"))


def test_a_slow_dialog_gives_up(tmp_path, monkeypatch, caplog):
    fake_binary(tmp_path, "kdialog", "exec sleep 10\n")
    on_path(tmp_path, monkeypatch)
    monkeypatch.setattr(pair, "CODE_WAIT", 0.2)
    assert asyncio.run(pair.ask_for_code("SERIAL")) is None
    assert "within 0 seconds" in caplog.text


def test_pair_with_code_uses_the_advertised_pairing_port(tmp_path, monkeypatch):
    log = tmp_path / "log"
    fake_avahi(tmp_path)
    fake_binary(
        tmp_path, "adb", f'echo "$@" >> {log}; echo "Successfully paired to $2 [guid=adb-SERIAL]"\n'
    )
    on_path(tmp_path, monkeypatch)
    assert asyncio.run(pair.pair_with_code("SERIAL", "979548")) == "10.0.0.9:43039"
    # The pairing port, not the connect port, and the code as typed.
    assert log.read_text().strip() == "pair 10.0.0.9:43039 979548"


def test_pairing_screen_not_open_says_so(tmp_path, monkeypatch):
    fake_avahi(tmp_path, pairing="")
    fake_binary(tmp_path, "adb", "exit 1\n")
    on_path(tmp_path, monkeypatch)
    with pytest.raises(OSError, match="not advertising a pairing port"):
        asyncio.run(pair.pair_with_code("SERIAL", "979548"))


def test_a_refused_code_carries_adbs_own_words(tmp_path, monkeypatch):
    fake_avahi(tmp_path)
    # adb prints its refusal on standard output, and has been seen to
    # exit zero doing it, so the text has to be what decides.
    fake_binary(tmp_path, "adb", "echo 'Failed: Wrong password'\n")
    on_path(tmp_path, monkeypatch)
    with pytest.raises(OSError, match="Wrong password"):
        asyncio.run(pair.pair_with_code("SERIAL", "111111"))
