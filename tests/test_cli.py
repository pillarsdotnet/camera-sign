# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio

import pytest
from kasa import AuthenticationError

from camera_sign import cli
from camera_sign.cli import Phone, Plug, camera_in_use, parse_args, phone_cameras_open


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    # parse_args reads ~/.config/camera-sign/env; keep the developer's own
    # settings out of every test.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


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


def test_host_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMERA_SIGN_HOST", raising=False)
    conf = tmp_path / "camera-sign"
    conf.mkdir()
    (conf / "env").write_text(
        "# a comment\n"
        "CAMERA_SIGN_HOST=10.0.0.8\n"
        "#CAMERA_SIGN_PHONE=192.168.1.51:5555\n"
        'CAMERA_SIGN_PASSWORD="hush hush"\n'
    )
    args = parse_args([])
    assert args.host == "10.0.0.8"
    assert args.password == "hush hush"  # noqa: S105 -- a quoting example, not a secret
    assert args.phone is None  # commented out, so not read


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    conf = tmp_path / "camera-sign"
    conf.mkdir()
    (conf / "env").write_text("CAMERA_SIGN_HOST=10.0.0.8\n")
    monkeypatch.setenv("CAMERA_SIGN_HOST", "10.0.0.9")
    assert parse_args([]).host == "10.0.0.9"


class RefusingDevice:
    """A plug that answers discovery but rejects the login."""

    def __init__(self):
        self.disconnected = False

    async def update(self):
        msg = "Device response did not match our challenge"
        raise AuthenticationError(msg)

    async def disconnect(self):
        self.disconnected = True


def test_failed_login_releases_the_session(monkeypatch):
    device = RefusingDevice()

    async def discover_single(_host, **_kwargs):
        return device

    monkeypatch.setattr(cli.Discover, "discover_single", discover_single)
    assert asyncio.run(Plug("10.0.0.9", "user", "wrong").set(on=True)) is False
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
    with pytest.raises(ValueError, match="no camera client list"):
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


AVAHI_DUMP = """\
+;wlp3s0;IPv4;adb-SERIAL-xYz12;_adb-tls-connect._tcp;local
=;wlp3s0;IPv6;adb-SERIAL-xYz12;_adb-tls-connect._tcp;local;Android.local;fe80::1;40779;"name=Pixel"
=;wlp3s0;IPv4;adb-OTHER-aBc34;_adb-tls-connect._tcp;local;Other.local;10.0.0.7;33333;"name=Other"
=;wlp3s0;IPv4;adb-SERIAL-xYz12;_adb-tls-connect._tcp;local;Android.local;10.0.0.9;40779;"name=Pixel"
"""


def fake_avahi_browse(tmp_path, dump):
    browse = tmp_path / "avahi-browse"
    browse.write_text(f'#!/bin/sh\ncat <<"EOF"\n{dump}EOF\n')
    browse.chmod(0o755)


def test_mdns_phone_is_discovered(tmp_path, monkeypatch):
    dump = tmp_path / "dump"
    dump.write_text(OPEN_DUMP)
    log = tmp_path / "log"
    fake_adb(tmp_path, monkeypatch, f'echo adb "$@" >> {log}; [ "$1" = connect ] || cat {dump}\n')
    fake_avahi_browse(tmp_path, AVAHI_DUMP)
    assert asyncio.run(Phone("mdns:SERIAL").in_use()) is True
    assert log.read_text().splitlines() == [
        "adb connect 10.0.0.9:40779",
        "adb -s 10.0.0.9:40779 shell dumpsys media.camera",
    ]


def test_mdns_phone_absent_counts_as_idle(tmp_path, monkeypatch, caplog):
    fake_adb(tmp_path, monkeypatch, "exit 1\n")
    fake_avahi_browse(tmp_path, "")
    phone = Phone("mdns:SERIAL")
    assert asyncio.run(phone.in_use()) is False
    assert asyncio.run(phone.in_use()) is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "not advertised" in warnings[0].getMessage()


def test_phone_switch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.phone_enabled()
    assert cli.main(["phone", "off"]) == 0
    assert not cli.phone_enabled()
    assert cli.main(["phone"]) == 0
    assert cli.main(["phone", "on"]) == 0
    assert cli.phone_enabled()
    assert capsys.readouterr().out.splitlines() == [
        "watching the phone: off",
        "watching the phone: off",
        "watching the phone: on",
    ]


def test_phone_switch_needs_no_plug(monkeypatch):
    monkeypatch.delenv("CAMERA_SIGN_HOST", raising=False)
    assert parse_args(["phone", "off"]).setting == "off"
    with pytest.raises(SystemExit):
        parse_args(["status", "on"])


def test_switched_off_phone_is_not_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "camera_in_use", lambda: False)

    class BusyPhone:
        async def in_use(self):
            return True

    assert asyncio.run(cli.any_camera_in_use(BusyPhone())) is True
    cli.set_phone_enabled(on=False)
    assert asyncio.run(cli.any_camera_in_use(BusyPhone())) is False


class FlakyDevice:
    """A plug whose network comes and goes at the test's say-so."""

    answering = True

    async def update(self):
        pass

    async def turn_on(self):
        if not self.answering:
            msg = "Timed out getting discovery response for 10.0.0.9"
            raise TimeoutError(msg)

    turn_off = turn_on

    async def disconnect(self):
        pass


def test_plug_reports_each_change_once(monkeypatch):
    device = FlakyDevice()

    async def discover_single(_host, **_kwargs):
        if not device.answering:
            msg = "Timed out getting discovery response for 10.0.0.9"
            raise TimeoutError(msg)
        return device

    monkeypatch.setattr(cli.Discover, "discover_single", discover_single)
    changes = []

    async def on_change(error):
        changes.append(error)

    async def flap():
        plug = Plug("10.0.0.9", None, None, on_change)
        assert await plug.set(on=True)
        device.answering = False
        assert not await plug.set(on=True)
        assert not await plug.set(on=True)
        device.answering = True
        assert await plug.set(on=True)
        assert await plug.set(on=True)

    asyncio.run(flap())
    assert changes == ["Timed out getting discovery response for 10.0.0.9", None]


def test_phone_reports_each_change_once(tmp_path, monkeypatch):
    down = tmp_path / "down"
    dump = tmp_path / "dump"
    dump.write_text(IDLE_DUMP)
    fake_adb(tmp_path, monkeypatch, f"[ -e {down} ] && exit 1; cat {dump}\n")
    changes = []

    async def on_change(error):
        changes.append(error)

    async def flap():
        phone = Phone("SERIAL", on_change)
        assert await phone.in_use() is False
        down.touch()
        assert await phone.in_use() is False
        assert await phone.in_use() is False
        down.unlink()
        assert await phone.in_use() is False

    asyncio.run(flap())
    assert changes == ["adb exited 1", None]


class FakeNotifier:
    """Records what reached the screen, and lets a test press a button."""

    def __init__(self):
        self.shown = []
        self.offered = []
        self.closed = 0
        self._buttons = ()

    async def show(self, summary, body="", *, urgent=False, actions=()):
        self.shown.append((summary, urgent))
        self.offered.append([(key, label) for key, label, _ in actions])
        self.bodies = getattr(self, "bodies", [])
        self.bodies.append(body)
        self._buttons = actions

    async def close(self):
        self.closed += 1

    def press(self, label):
        """Return the callback behind the button with this label."""
        for _key, button, callback in self._buttons:
            if button == label:
                return callback
        msg = f"no {label!r} button on screen"
        raise AssertionError(msg)


def test_phone_change_notifications():
    notifier = FakeNotifier()
    # A phone named by address is not reached over mDNS, so there is
    # nothing to pair and no button to offer.
    phone = Phone("10.0.0.9:5555")
    cli.notify_phone_changes(phone, notifier)

    async def flap():
        await phone.on_change("timed out")
        await phone.on_change(None)

    asyncio.run(flap())
    assert notifier.shown == [
        ("On-air sign phone is not answering", True),
        ("On-air sign phone is back", False),
    ]
    assert notifier.offered == [[], []]


PAIRING_DUMP = (
    "=;wlp3s0;IPv4;adb-SERIAL-xYz12;_adb-tls-pairing._tcp;local;"
    'Android.local;10.0.0.9;43039;"name=Pixel"\n'
)


def fake_avahi_by_type(tmp_path, *, connect=AVAHI_DUMP, pairing=PAIRING_DUMP):
    """Install an avahi-browse that answers per service type.

    fake_avahi_browse above returns one dump whatever is asked for, which
    cannot serve the pairing flow: it looks up the connect port and then
    the pairing port, and the two differ.
    """
    connect_file = tmp_path / "connect.txt"
    connect_file.write_text(connect)
    pairing_file = tmp_path / "pairing.txt"
    pairing_file.write_text(pairing)
    browse = tmp_path / "avahi-browse"
    browse.write_text(
        "#!/bin/sh\n"
        "for arg; do last=$arg; done\n"
        'case "$last" in\n'
        f"  *pairing*) cat {pairing_file} ;;\n"
        f"  *) cat {connect_file} ;;\n"
        "esac\n"
    )
    browse.chmod(0o755)


def test_repair_button_only_when_the_phone_is_still_advertised(tmp_path, monkeypatch):
    fake_avahi_browse(tmp_path, AVAHI_DUMP)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    advertised = FakeNotifier()
    phone = Phone("mdns:SERIAL")
    cli.notify_phone_changes(phone, advertised)
    asyncio.run(phone.on_change("failed to connect to 10.0.0.9:40779"))
    # On the network but refusing adb: a dropped key, which pairing fixes.
    assert advertised.offered == [[(cli.REPAIR_ACTION, "Re-pair")]]

    # Out of the house: nothing advertised, so nothing to pair with.
    fake_avahi_browse(tmp_path, "")
    away = FakeNotifier()
    gone = Phone("mdns:SERIAL")
    cli.notify_phone_changes(gone, away)
    asyncio.run(gone.on_change("wireless debugging is not advertised on the network"))
    assert away.offered == [[]]


def test_repair_button_pairs_and_dismisses_the_alert(tmp_path, monkeypatch):
    log = tmp_path / "log"
    fake_avahi_by_type(tmp_path)
    fake_adb(
        tmp_path,
        monkeypatch,
        f'echo "$@" >> {log}\n'
        'case "$1" in pair) echo "Successfully paired to $2 [guid=adb-SERIAL]" ;; esac\n',
    )
    fake_binary = tmp_path / "kdialog"
    fake_binary.write_text('#!/bin/sh\necho "979 548"\n')
    fake_binary.chmod(0o755)

    notifier = FakeNotifier()
    phone = Phone("mdns:SERIAL")
    cli.notify_phone_changes(phone, notifier)

    async def fail_then_press():
        await phone.on_change("failed to connect to 10.0.0.9:40779")
        await notifier.press("Re-pair")()
        if phone._reconnect is not None:
            await phone._reconnect  # the pairing asks for an immediate retry

    asyncio.run(fail_then_press())
    calls = log.read_text().splitlines()
    # The pairing port, and the code with the phone's own spacing removed.
    assert "pair 10.0.0.9:43039 979548" in calls
    assert notifier.closed == 1  # the alert was dismissed, as asked
    assert notifier.shown == [("On-air sign phone is not answering", True)]


def test_a_cancelled_repair_puts_the_alert_back(tmp_path, monkeypatch):
    fake_avahi_by_type(tmp_path)
    fake_adb(tmp_path, monkeypatch, "exit 1\n")
    cancelled = tmp_path / "kdialog"
    cancelled.write_text("#!/bin/sh\nexit 1\n")
    cancelled.chmod(0o755)

    notifier = FakeNotifier()
    phone = Phone("mdns:SERIAL")
    cli.notify_phone_changes(phone, notifier)

    async def fail_then_cancel():
        await phone.on_change("failed to connect to 10.0.0.9:40779")
        await notifier.press("Re-pair")()

    asyncio.run(fail_then_cancel())
    assert notifier.closed == 0  # nothing was fixed, so nothing is dismissed
    # The alert is shown again, button and all, so it can be tried later.
    assert notifier.offered == [
        [(cli.REPAIR_ACTION, "Re-pair")],
        [(cli.REPAIR_ACTION, "Re-pair")],
    ]
    assert "cancelled" in notifier.bodies[-1]


def test_pair_command_takes_a_code(tmp_path, monkeypatch, capsys):
    fake_avahi_by_type(tmp_path)
    fake_adb(tmp_path, monkeypatch, 'echo "Successfully paired to $2 [guid=adb-SERIAL]"\n')
    monkeypatch.setenv("CAMERA_SIGN_PHONE", "mdns:SERIAL")
    assert cli.main(["pair", "--code", "979 548"]) == 0
    assert capsys.readouterr().out.strip() == "paired with SERIAL at 10.0.0.9:43039"


def test_pair_command_rejects_a_code_that_cannot_be_one(tmp_path, monkeypatch):
    fake_avahi_by_type(tmp_path)
    fake_adb(tmp_path, monkeypatch, "exit 1\n")
    monkeypatch.setenv("CAMERA_SIGN_PHONE", "mdns:SERIAL")
    assert cli.main(["pair", "--code", "nope"]) == 1


def test_pair_needs_a_serial_not_an_address(monkeypatch):
    monkeypatch.setenv("CAMERA_SIGN_PHONE", "10.0.0.9:5555")
    with pytest.raises(SystemExit):
        parse_args(["pair"])
    monkeypatch.delenv("CAMERA_SIGN_PHONE", raising=False)
    with pytest.raises(SystemExit):
        parse_args(["pair"])
    monkeypatch.setenv("CAMERA_SIGN_PHONE", "mdns:SERIAL")
    assert parse_args(["pair"]).command == "pair"


def test_code_belongs_to_pair_alone(monkeypatch):
    monkeypatch.setenv("CAMERA_SIGN_HOST", "10.0.0.9")
    with pytest.raises(SystemExit):
        parse_args(["status", "--code", "979548"])


def test_plug_change_notifications():
    shown = []

    class FakeNotifier:
        async def show(self, summary, _body="", *, urgent=False):
            shown.append((summary, urgent))

    plug = Plug("10.0.0.9", None, None)
    cli.notify_plug_changes(plug, FakeNotifier())

    async def flap():
        await plug.on_change("timed out")
        await plug.on_change(None)

    asyncio.run(flap())
    assert shown == [
        ("On-air sign plug is not answering", True),
        ("On-air sign plug is back", False),
    ]
