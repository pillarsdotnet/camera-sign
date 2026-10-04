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

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from kasa import Device, Discover, KasaException

from camera_sign import __version__
from camera_sign.debounce import Debouncer

REFCNT = Path("/sys/module/uvcvideo/refcnt")
PLUG_ERRORS = (KasaException, OSError, TimeoutError)
ADB_TIMEOUT = 3.0
RECONNECT_EVERY = 30.0

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


def set_phone_enabled(on: bool) -> None:
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
        raise ValueError("no camera client list in dumpsys output")
    clients, _, _ = rest.partition("Allowed user IDs:")
    return "(Camera ID:" in clients


class Phone:
    """An Android phone whose cameras are read over `adb`.

    A serial of the form `host:port` is a phone on Wi-Fi.  The `adb` server
    forgets such a phone when the network drops or the server restarts, so
    while it fails to answer it is reconnected now and then, in the
    background, so that the webcam is never kept waiting.
    """

    def __init__(self, serial: str):
        self.serial = serial
        self._failing = False
        self._reconnect: asyncio.Task | None = None
        self._last_reconnect = -RECONNECT_EVERY

    async def _adb(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), ADB_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise OSError(err.decode(errors="replace").strip() or f"adb exited {proc.returncode}")
        return out.decode(errors="replace")

    async def _connect(self) -> None:
        with contextlib.suppress(OSError, TimeoutError):
            log.debug(
                "adb connect %s: %s", self.serial, (await self._adb("connect", self.serial)).strip()
            )

    def _start_reconnect(self) -> None:
        now = time.monotonic()
        if ":" not in self.serial or self._reconnect is not None and not self._reconnect.done():
            return
        if now - self._last_reconnect < RECONNECT_EVERY:
            return
        self._last_reconnect = now
        self._reconnect = asyncio.create_task(self._connect())

    async def in_use(self) -> bool:
        """Return whether any app on the phone has a camera open.

        An unreachable phone counts as idle, so the webcam still works alone.
        """
        try:
            busy = phone_cameras_open(
                await self._adb("-s", self.serial, "shell", "dumpsys", "media.camera")
            )
        except (OSError, TimeoutError, ValueError) as exc:
            # Warn once per outage rather than on every check.
            level = logging.DEBUG if self._failing else logging.WARNING
            log.log(
                level, "cannot read the cameras on %s: %s", self.serial, str(exc) or "timed out"
            )
            self._failing = True
            self._start_reconnect()
            return False
        if self._failing:
            log.info("phone %s is answering again", self.serial)
            self._failing = False
        return busy


class Plug:
    """A `Kasa` smart plug, reconnected on demand after any failure."""

    def __init__(self, host: str, username: str | None, password: str | None):
        self.host = host
        self.username = username
        self.password = password
        self._device: Device | None = None
        self._failing = False

    async def _connect(self) -> Device:
        if self._device is None:
            device = await Discover.discover_single(
                self.host, username=self.username, password=self.password
            )
            if device is None:
                raise KasaException(f"no Kasa device answered at {self.host}")
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

    async def set(self, on: bool) -> bool:
        """Switch the plug; return whether it worked."""
        try:
            device = await self._connect()
            await (device.turn_on() if on else device.turn_off())
        except PLUG_ERRORS as exc:
            # Warn once per outage rather than on every retry.
            level = logging.DEBUG if self._failing else logging.WARNING
            log.log(level, "could not switch %s %s: %s", self.host, _word(on), exc)
            self._failing = True
            await self.close()
            return False
        if self._failing:
            log.info("plug at %s is answering again", self.host)
            self._failing = False
        return True


class SleepGuard:
    """Run a callback before the machine suspends or hibernates.

    Holds a `logind` delay lock, which makes the machine wait (up to
    `InhibitDelayMaxSec`, five seconds by default) until the lock is
    released.  The lock is taken again on resume.
    """

    def __init__(self, before_sleep: Callable[[], Awaitable[None]]):
        self.before_sleep = before_sleep
        self._manager = None
        self._fd: int | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Connect to `logind` and take the first lock."""
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        path = "/org/freedesktop/login1"
        introspection = await bus.introspect("org.freedesktop.login1", path)
        proxy = bus.get_proxy_object("org.freedesktop.login1", path, introspection)
        self._manager = proxy.get_interface("org.freedesktop.login1.Manager")
        self._manager.on_prepare_for_sleep(self._on_prepare_for_sleep)
        await self._lock()

    async def _lock(self) -> None:
        self._fd = await self._manager.call_inhibit(
            "sleep", "camera-sign", "Switch the on-air sign off", "delay"
        )

    def _on_prepare_for_sleep(self, going_down: bool) -> None:
        task = asyncio.create_task(self._handle(going_down))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, going_down: bool) -> None:
        if going_down:
            try:
                await self.before_sleep()
            finally:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
        else:
            await self._lock()


def _word(on: bool) -> str:
    return "on" if on else "off"


async def any_camera_in_use(phone: Phone | None) -> bool:
    """Return whether the webcam, or the phone if there is one and it is on, is in use."""
    if camera_in_use():
        return True
    return phone is not None and phone_enabled() and await phone.in_use()


async def watch(args: argparse.Namespace, plug: Plug, phone: Phone | None = None) -> None:
    """Keep the plug in step with the camera until told to stop."""
    debouncer = Debouncer(on_delay=args.on_delay, off_delay=args.off_delay)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(signum, stop.set)

    sent: bool | None = None  # what the plug was last told, if it heard
    last_sent = 0.0

    async def send(on: bool) -> None:
        nonlocal sent, last_sent
        if await plug.set(on):
            if on != sent:
                log.info("sign %s", _word(on))
            sent, last_sent = on, time.monotonic()
        else:
            sent = None

    async def before_sleep() -> None:
        log.info("suspending")
        debouncer.lit = False
        await send(False)

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
        want = debouncer.update(in_use, now)
        # Tell the plug again now and then, in case someone pressed its button
        # or it lost power; a failed send is retried on the next tick.
        if want != sent or now - last_sent >= args.refresh:
            await send(want)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), args.interval)

    log.info("stopping")
    await send(False)
    await plug.close()


async def switch(plug: Plug, on: bool) -> int:
    """Switch the plug once, for testing it by hand."""
    ok = await plug.set(on)
    await plug.close()
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read options, falling back to `CAMERA_SIGN_*` environment variables."""
    env = os.environ.get
    parser = argparse.ArgumentParser(prog="camera-sign", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "command",
        nargs="?",
        default="watch",
        choices=["watch", "on", "off", "status", "phone"],
        help="watch the camera (default), switch the sign once, report the camera,"
        " or report or switch watching the phone",
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
        help="also watch the cameras of this adb device (serial, or host:port over Wi-Fi)",
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.setting is not None and args.command != "phone":
        parser.error(f"{args.command} takes no on or off")
    if args.command not in ("status", "phone") and not args.host:
        parser.error("set --host or CAMERA_SIGN_HOST to the plug's address")
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

    if args.command == "phone":
        if args.setting is not None:
            set_phone_enabled(args.setting == "on")
        print(f"watching the phone: {_word(phone_enabled())}")
        return 0
    phone = Phone(args.phone) if args.phone else None
    if args.command == "status":
        print("camera in use" if asyncio.run(any_camera_in_use(phone)) else "camera idle")
        return 0
    plug = Plug(args.host, args.username, args.password)
    if args.command == "watch":
        asyncio.run(watch(args, plug, phone))
        return 0
    return asyncio.run(switch(plug, args.command == "on"))


if __name__ == "__main__":
    sys.exit(main())
