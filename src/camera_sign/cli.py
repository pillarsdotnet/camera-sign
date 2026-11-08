# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
"""Watch the webcam and switch a `Kasa` smart plug to match.

The camera counts as live whenever any process holds a video device
open.  The `uvcvideo` driver's module reference count rises by one for
every open handle, so reading a single file under `/sys` answers the
question for every user on the machine without special permissions.

An Android phone reachable over `adb` can be watched too: its camera
service lists every app holding a camera open.  `camera-sign phone off`
pauses that, and `camera-sign phone on` resumes it, in a running watcher
as well, by way of a flag file under `$XDG_STATE_HOME`.

The watcher raises a desktop notification whenever the plug or the
phone stops answering, and again when it answers once more, since both
failures are silent otherwise: a camera opens, and the sign stays dark.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

if TYPE_CHECKING:
    from dbus_fast.aio.proxy_object import ProxyInterface
from kasa import Device, Discover, KasaException

from camera_sign import __version__, pair
from camera_sign.debounce import Debouncer
from camera_sign.notify import Notifier
from camera_sign.process import run

REFCNT = Path("/sys/module/uvcvideo/refcnt")
PLUG_ERRORS = (KasaException, OSError, TimeoutError)
ADB_TIMEOUT = 3.0
RECONNECT_EVERY = 30.0
MDNS_PREFIX = "mdns:"
REPAIR_ACTION = "repair"

log = logging.getLogger("camera-sign")


def camera_in_use(refcnt: Path = REFCNT) -> bool:
    """Return whether any process has the webcam open."""
    try:
        return int(refcnt.read_text()) > 0
    except FileNotFoundError:
        # The driver is not loaded, so no camera can be open.
        return False


def phone_off_flag() -> Path:
    """Return the file whose presence pauses watching the phone."""
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "camera-sign" / "phone-off"


def phone_enabled() -> bool:
    """Return whether the phone is to be watched."""
    return not phone_off_flag().exists()


def set_phone_enabled(*, on: bool) -> None:
    """Resume or pause watching the phone, for every watcher of this user."""
    flag = phone_off_flag()
    if on:
        flag.unlink(missing_ok=True)
    else:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()


def phone_cameras_open(dump: str) -> bool:
    """Return whether `dumpsys media.camera` output lists an open camera.

    The section reads `Active Camera Clients:` then `[]` when idle, or one
    `(Camera ID: ...)` line per app holding a camera.
    """
    _, found, rest = dump.partition("Active Camera Clients:")
    if not found:
        msg = "no camera client list in dumpsys output"
        raise ValueError(msg)
    clients, _, _ = rest.partition("Allowed user IDs:")
    return "(Camera ID:" in clients


class Phone:
    """An Android phone whose cameras are read over `adb`.

    A serial of the form `host:port` is a phone on Wi-Fi.  The `adb` server
    forgets such a phone when the network drops or the server restarts, so
    while it fails to answer it is reconnected now and then, in the
    background, so that the webcam is never kept waiting.

    A serial of the form `mdns:SERIAL` is a phone with wireless debugging
    on, which listens on a new port after every reboot and advertises it
    over mDNS.  The address and port are read from `avahi-browse`, on the
    first check and again whenever the phone stops answering.

    `on_change`, if given, is awaited whenever the phone stops answering,
    with the error, and whenever it answers again, with `None`.
    """

    def __init__(
        self,
        serial: str,
        on_change: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> None:
        """Remember the serial; nothing is contacted until the first check."""
        self.serial = serial
        self.on_change = on_change
        self._mdns = serial.removeprefix(MDNS_PREFIX) if serial.startswith(MDNS_PREFIX) else None
        self._target: str | None = None if self._mdns else serial
        self._failing = False
        self._reconnect: asyncio.Task | None = None
        self._last_reconnect = -RECONNECT_EVERY

    @property
    def wireless_serial(self) -> str | None:
        """The phone's own serial, for one reached over mDNS; otherwise None."""
        return self._mdns

    async def _adb(self, *args: str) -> str:
        return await run("adb", *args, timeout=ADB_TIMEOUT)

    async def _discover(self) -> str | None:
        """Return the phone's wireless-debugging `host:port`, found over mDNS."""
        # self._mdns is set whenever this is reached, by the caller's guard.
        return await pair.advertised(self._mdns or "", pair.CONNECT_TYPE, timeout=ADB_TIMEOUT)

    async def advertised(self) -> str | None:
        """Return where wireless debugging is advertised, looked up afresh.

        None for a phone not reached over mDNS, and for one advertising
        nothing -- out of the house, or with wireless debugging switched
        off at the phone.  A phone that answers here while `adb` cannot
        reach it has dropped this machine's key, which is the one cause
        of an outage that pairing again puts right.
        """
        if self._mdns is None:
            return None
        try:
            return await pair.advertised(self._mdns, pair.CONNECT_TYPE, timeout=ADB_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            log.debug("cannot browse for %s: %s", self.serial, str(exc) or "timed out")
            return None

    def reconnect_now(self) -> None:
        """Reconnect at once rather than waiting out the retry interval.

        Pairing again leaves the phone reachable immediately, and making
        the sign wait up to `RECONNECT_EVERY` seconds to notice would
        look like the pairing had not worked.
        """
        self._last_reconnect = -RECONNECT_EVERY
        self._start_reconnect()

    async def _connect(self) -> None:
        self._last_reconnect = time.monotonic()
        with contextlib.suppress(OSError, TimeoutError):
            if self._mdns is not None:
                self._target = await self._discover() or self._target
            if self._target is not None:
                log.debug(
                    "adb connect %s: %s",
                    self._target,
                    (await self._adb("connect", self._target)).strip(),
                )

    def _start_reconnect(self) -> None:
        now = time.monotonic()
        if ":" not in self.serial or (self._reconnect is not None and not self._reconnect.done()):
            return
        if now - self._last_reconnect < RECONNECT_EVERY:
            return
        self._last_reconnect = now
        self._reconnect = asyncio.create_task(self._connect())

    async def _dump_cameras(self) -> str:
        """Return the phone's camera service dump, connecting first if need be."""
        if self._target is None and not self._failing:
            await self._connect()  # the first look for the phone waits for mDNS
        if self._target is None:
            msg = "wireless debugging is not advertised on the network"
            raise OSError(msg)
        return await self._adb("-s", self._target, "shell", "dumpsys", "media.camera")

    async def in_use(self) -> bool:
        """Return whether any app on the phone has a camera open.

        An unreachable phone counts as idle, so the webcam still works alone.
        """
        try:
            busy = phone_cameras_open(await self._dump_cameras())
        except (OSError, TimeoutError, ValueError) as exc:
            # Warn once per outage rather than on every check.
            level = logging.DEBUG if self._failing else logging.WARNING
            log.log(
                level, "cannot read the cameras on %s: %s", self.serial, str(exc) or "timed out"
            )
            self._start_reconnect()
            if not self._failing:
                self._failing = True
                if self.on_change is not None:
                    await self.on_change(str(exc) or "timed out")
            return False
        if self._failing:
            log.info("phone %s is answering again", self.serial)
            self._failing = False
            if self.on_change is not None:
                await self.on_change(None)
        return busy


class Plug:
    """A `Kasa` smart plug, reconnected on demand after any failure.

    `on_change`, if given, is awaited whenever the plug stops answering,
    with the error, and whenever it answers again, with `None`.
    """

    def __init__(
        self,
        host: str,
        username: str | None,
        password: str | None,
        on_change: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> None:
        """Remember the address and login; the plug is contacted on demand."""
        self.host = host
        self.username = username
        self.password = password
        self.on_change = on_change
        self._device: Device | None = None
        self._failing = False

    async def _connect(self) -> Device:
        if self._device is None:
            device = await Discover.discover_single(
                self.host, username=self.username, password=self.password
            )
            if device is None:
                msg = f"no Kasa device answered at {self.host}"
                raise KasaException(msg)
            try:
                await device.update()
            except BaseException:
                # Not kept yet, so close() cannot reach it; release its session here.
                with contextlib.suppress(*PLUG_ERRORS):
                    await device.disconnect()
                raise
            self._device = device
        return self._device

    async def close(self) -> None:
        """Drop the connection, if there is one."""
        if self._device is not None:
            with contextlib.suppress(*PLUG_ERRORS):
                await self._device.disconnect()
            self._device = None

    async def set(self, *, on: bool) -> bool:
        """Switch the plug; return whether it worked."""
        try:
            device = await self._connect()
            await (device.turn_on() if on else device.turn_off())
        except PLUG_ERRORS as exc:
            # Warn once per outage rather than on every retry.
            level = logging.DEBUG if self._failing else logging.WARNING
            log.log(level, "could not switch %s %s: %s", self.host, _word(on=on), exc)
            await self.close()
            if not self._failing:
                self._failing = True
                if self.on_change is not None:
                    await self.on_change(str(exc) or type(exc).__name__)
            return False
        if self._failing:
            log.info("plug at %s is answering again", self.host)
            self._failing = False
            if self.on_change is not None:
                await self.on_change(None)
        return True


class SleepGuard:
    """Run a callback before the machine suspends or hibernates.

    Holds a `logind` delay lock, which makes the machine wait (up to
    `InhibitDelayMaxSec`, five seconds by default) until the lock is
    released.  The lock is taken again on resume.
    """

    def __init__(self, before_sleep: Callable[[], Awaitable[None]]) -> None:
        """Remember the callback; the lock is taken by `start`."""
        self.before_sleep = before_sleep
        self._manager: ProxyInterface | None = None  # set by start()
        self._fd: int | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Connect to `logind` and take the first lock."""
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        path = "/org/freedesktop/login1"
        introspection = await bus.introspect("org.freedesktop.login1", path)
        proxy = bus.get_proxy_object("org.freedesktop.login1", path, introspection)
        self._manager = proxy.get_interface("org.freedesktop.login1.Manager")
        # dbus_fast adds on_<signal> methods at runtime, from introspection.
        self._manager.on_prepare_for_sleep(self._on_prepare_for_sleep)  # type: ignore[attr-defined]
        await self._lock()

    async def _lock(self) -> None:
        # start() connects before the first lock, so _manager is never None here.
        self._fd = await self._manager.call_inhibit(  # type: ignore[union-attr]
            "sleep", "camera-sign", "Switch the on-air sign off", "delay"
        )

    def _on_prepare_for_sleep(self, going_down: bool) -> None:  # noqa: FBT001 -- signature set by the logind signal
        task = asyncio.create_task(self._handle(going_down=going_down))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, *, going_down: bool) -> None:
        if going_down:
            try:
                await self.before_sleep()
            finally:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
        else:
            await self._lock()


def _word(*, on: bool) -> str:
    return "on" if on else "off"


def notify_plug_changes(plug: Plug, notifier: Notifier) -> None:
    """Show a notification each time the plug stops or starts answering."""

    async def on_change(error: str | None) -> None:
        if error is None:
            await notifier.show(
                "On-air sign plug is back", f"The plug at {plug.host} is answering again."
            )
        else:
            await notifier.show(
                "On-air sign plug is not answering",
                f"The sign cannot be switched until the plug at {plug.host} answers. {error}",
                urgent=True,
            )

    plug.on_change = on_change


def notify_phone_changes(phone: Phone, notifier: Notifier) -> None:
    """Show a notification each time the phone stops or starts answering.

    While the phone is on the network but refusing `adb`, the alert
    carries a button that pairs wireless debugging again.  That is the
    one cause of an outage this machine can put right, and it cannot be
    put right unattended: the six-digit code is on the phone's screen,
    so the button has to ask for it.  A phone that is merely away
    advertises nothing over mDNS and gets no button, since there would
    be nothing to pair with.
    """
    reason = ""

    async def alert(note: str = "") -> None:
        body = (
            f"The phone's cameras cannot light the sign until {phone.serial} answers;"
            f" the webcam still can. {reason}"
        )
        actions = ((REPAIR_ACTION, "Re-pair", repair),) if await phone.advertised() else ()
        await notifier.show(
            "On-air sign phone is not answering",
            f"{body}\n\n{note}" if note else body,
            urgent=True,
            actions=actions,
        )

    async def repair() -> None:
        """Pair again, then dismiss the alert; put it back if it did not work."""
        serial = phone.wireless_serial
        if serial is None:  # no button is offered for such a phone
            return
        try:
            code = await pair.ask_for_code(serial)
        except LookupError as exc:
            # Nowhere to type the code, so the button cannot help here.
            await notifier.show("On-air sign cannot pair", str(exc), urgent=True)
            return
        except ValueError as exc:
            await alert(f"{exc}. Press Re-pair to try again.")
            return
        if code is None:
            await alert("Pairing was cancelled. Press Re-pair to start again.")
            return
        try:
            endpoint = await pair.pair_with_code(serial, code)
        except (OSError, TimeoutError) as exc:
            await alert(f"Pairing failed: {str(exc) or 'timed out'}")
            return
        # pair_with_code has already logged the pairing itself.
        log.info("reconnecting to %s at once, after pairing", endpoint)
        # Do not wait out the retry interval: the sign should follow the
        # phone's camera again by the next check, not in half a minute.
        phone.reconnect_now()
        await notifier.close()

    async def on_change(error: str | None) -> None:
        nonlocal reason
        if error is None:
            await notifier.show(
                "On-air sign phone is back", f"The phone at {phone.serial} is answering again."
            )
            return
        reason = error
        await alert()

    phone.on_change = on_change


async def any_camera_in_use(phone: Phone | None) -> bool:
    """Return whether the webcam, or the phone if there is one and it is on, is in use."""
    if camera_in_use():
        return True
    return phone is not None and phone_enabled() and await phone.in_use()


async def watch(  # noqa: C901 -- one cohesive loop reads better than pieces split to meet the limit
    args: argparse.Namespace, plug: Plug, phone: Phone | None = None
) -> None:
    """Keep the plug in step with the camera until told to stop."""
    debouncer = Debouncer(on_delay=args.on_delay, off_delay=args.off_delay)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(signum, stop.set)

    sent: bool | None = None  # what the plug was last told, if it heard
    last_sent = 0.0

    async def send(*, on: bool) -> None:
        nonlocal sent, last_sent
        if await plug.set(on=on):
            if on != sent:
                log.info("sign %s", _word(on=on))
            sent, last_sent = on, time.monotonic()
        else:
            sent = None

    async def before_sleep() -> None:
        log.info("suspending")
        debouncer.lit = False
        await send(on=False)

    try:
        await SleepGuard(before_sleep).start()
    except Exception as exc:  # noqa: BLE001 -- best effort, the watcher still works
        log.warning("cannot watch for suspend, the sign may stay lit through it: %s", exc)

    log.info("watching %s, plug at %s", REFCNT, plug.host)
    phone_on = None
    while not stop.is_set():
        if phone is not None and phone_on != (phone_on := phone_enabled()):
            if phone_on:
                log.info("watching the cameras on phone %s", phone.serial)
            else:
                log.info("not watching phone %s: switched off with camera-sign phone", phone.serial)
        in_use = await any_camera_in_use(phone)
        now = time.monotonic()
        want = debouncer.update(in_use=in_use, now=now)
        # Tell the plug again now and then, in case someone pressed its button
        # or it lost power; a failed send is retried on the next tick.
        if want != sent or now - last_sent >= args.refresh:
            await send(on=want)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), args.interval)

    log.info("stopping")
    await send(on=False)
    await plug.close()


async def repair_once(serial: str, typed: str | None) -> int:
    """Pair with a phone's wireless debugging, run by hand.

    `serial` is the phone's own serial, as `adb devices` prints it, with
    no `mdns:` prefix and no address: the mDNS name to look for is built
    from it.  `typed` skips the dialog, for a code read off the phone
    and passed on the command line.
    """
    if typed is None:
        try:
            code = await pair.ask_for_code(serial)
        except (LookupError, ValueError) as exc:
            log.error("%s", exc)  # noqa: TRY400 -- a command's complaint, not a traceback
            return 1
        if code is None:
            return 1  # cancelled, and already logged
    else:
        code = pair.normalise(typed)
        if code is None:
            log.error("%r is not a six-digit pairing code", typed)
            return 1
    try:
        endpoint = await pair.pair_with_code(serial, code)
    except (OSError, TimeoutError) as exc:
        failure = str(exc) or "timed out"
    else:
        # Intentional command-line output, not stray debugging.
        print(f"paired with {serial} at {endpoint}")  # noqa: T201
        return 0
    log.error("could not pair with %s: %s", serial, failure)
    return 1


async def switch(plug: Plug, *, on: bool) -> int:
    """Switch the plug once, for testing it by hand."""
    ok = await plug.set(on=on)
    await plug.close()
    return 0 if ok else 1


def config_env_file() -> Path:
    """Return the settings file the service reads through `EnvironmentFile=`."""
    config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config) / "camera-sign" / "env"


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Read the `CAMERA_SIGN_*` settings from the service's env file.

    Run by hand, the command sees the same settings the service gets from
    `EnvironmentFile=`, so `camera-sign on` works without sourcing the
    file first.  Variables already in the environment still win.
    """
    values: dict[str, str] = {}
    try:
        text = (path or config_env_file()).read_text()
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if line.startswith("#") or not sep or not key.startswith("CAMERA_SIGN_"):
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read options, falling back to `CAMERA_SIGN_*` variables and the env file."""
    settings = load_env_file()
    settings |= {k: v for k, v in os.environ.items() if k.startswith("CAMERA_SIGN_")}
    env = settings.get
    parser = argparse.ArgumentParser(prog="camera-sign", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "command",
        nargs="?",
        default="watch",
        choices=["watch", "on", "off", "status", "phone", "pair"],
        help="watch the camera (default), switch the sign once, report the camera,"
        " report or switch watching the phone, or pair the phone's wireless debugging",
    )
    parser.add_argument(
        "setting", nargs="?", choices=["on", "off"], help="after phone: watch it or not"
    )
    parser.add_argument("--host", default=env("CAMERA_SIGN_HOST"), help="plug address")
    parser.add_argument("--username", default=env("CAMERA_SIGN_USERNAME"))
    parser.add_argument("--password", default=env("CAMERA_SIGN_PASSWORD"))
    parser.add_argument(
        "--phone",
        default=env("CAMERA_SIGN_PHONE") or None,
        metavar="SERIAL",
        help="also watch the cameras of this adb device (serial, host:port over Wi-Fi,"
        " or mdns:serial to find its wireless debugging over mDNS)",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between checks")
    parser.add_argument(
        "--on-delay", type=float, default=2.0, help="seconds the camera must stay open"
    )
    parser.add_argument(
        "--off-delay", type=float, default=5.0, help="seconds the camera must stay closed"
    )
    parser.add_argument(
        "--refresh", type=float, default=60.0, help="seconds between repeats of the state"
    )
    parser.add_argument(
        "--code",
        help="the phone's six-digit pairing code, for pair, instead of being asked for it",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.setting is not None and args.command != "phone":
        parser.error(f"{args.command} takes no on or off")
    if args.command not in ("status", "phone", "pair") and not args.host:
        parser.error("set --host or CAMERA_SIGN_HOST to the plug's address")
    if args.code is not None and args.command != "pair":
        parser.error(f"{args.command} takes no --code")
    if args.command == "pair":
        if not args.phone:
            parser.error("set --phone or CAMERA_SIGN_PHONE to the phone's serial")
        # Pairing finds the phone by the mDNS name built from its serial,
        # so an address cannot stand in for one here.
        if ":" in args.phone.removeprefix(MDNS_PREFIX):
            parser.error(
                "pairing needs the phone's own serial, as in --phone mdns:52221JEBF23974;"
                f" {args.phone!r} names an address"
            )
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `camera-sign` command."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        # python-kasa logs every reconnect attempt; keep the journal readable.
        logging.getLogger("kasa").setLevel(logging.WARNING)

    if args.command == "pair":
        return asyncio.run(repair_once(args.phone.removeprefix(MDNS_PREFIX), args.code))
    if args.command == "phone":
        if args.setting is not None:
            set_phone_enabled(on=args.setting == "on")
        # Intentional command-line output, not stray debugging.
        print(f"watching the phone: {_word(on=phone_enabled())}")  # noqa: T201
        return 0
    phone = Phone(args.phone) if args.phone else None
    if args.command == "status":
        # Intentional command-line output, not stray debugging.
        print("camera in use" if asyncio.run(any_camera_in_use(phone)) else "camera idle")  # noqa: T201
        return 0
    plug = Plug(args.host, args.username, args.password)
    if args.command == "watch":
        # Separate notifiers, so a phone notice never replaces a plug one.
        notify_plug_changes(plug, Notifier())
        if phone is not None:
            notify_phone_changes(phone, Notifier())
        asyncio.run(watch(args, plug, phone))
        return 0
    return asyncio.run(switch(plug, on=args.command == "on"))


if __name__ == "__main__":
    sys.exit(main())
