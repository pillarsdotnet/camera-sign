# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
"""Find and pair with an Android phone's wireless debugging.

Wireless debugging advertises itself over mDNS on a port that changes at
every phone restart, so the port to reach it on is looked up rather than
configured.  Two separate services are advertised: the connect port,
whenever wireless debugging is on, and the pairing port, only while the
phone's "Pair device with pairing code" screen is open.  Both are
advertised under a name of the form `adb-SERIAL-SUFFIX`, where the
suffix changes as freely as the port.

Pairing matters because wireless debugging keeps its own trust store,
separate from the one USB debugging uses.  When the phone drops this
machine's key, the failure is all but silent: mDNS still advertises the
phone, the port still accepts a connection, and `adb connect` reports
only "failed to connect", while the adb server's own log shows the
certificate being rejected after the handshake.  Nothing on this side
can be fixed to recover; the phone has to hand over a fresh six-digit
code, so pairing has to ask for it on screen.

Which also means the pairing screen has to be open before the pairing
port can be found.  The code is asked for first for that reason: by the
time it is typed, the phone is showing the screen that advertises the
port.
"""

import logging

from camera_sign.process import run, run_status

CONNECT_TYPE = "_adb-tls-connect._tcp"
PAIRING_TYPE = "_adb-tls-pairing._tcp"
BROWSE_TIMEOUT = 5.0
PAIR_TIMEOUT = 15.0
CODE_WAIT = 300.0  # time to walk to the phone, find the screen and read the code
CODE_LENGTH = 6
TITLE = "camera-sign"

INSTRUCTIONS = """\
The on-air sign cannot reach the phone {serial}.

It is on the network, but refusing this computer's key, so wireless
debugging has to be paired again.

On the phone:

1.  Settings -> System -> Developer options -> Wireless debugging
2.  Tap "Pair device with pairing code"
3.  Leave that screen open, and type the six-digit code below.
"""

# Each entry is the arguments before the prompt text and the arguments
# after it, for a dialog that asks for one line and prints it on standard
# output.  The first program installed wins; a desktop with neither
# leaves nowhere to type the code.  kdialog takes a default value after
# the text, zenity takes nothing.
DIALOGS = (
    (("kdialog", "--title", TITLE, "--inputbox"), ("",)),
    (("zenity", "--title", TITLE, "--entry", "--text"), ()),
)

log = logging.getLogger("camera-sign")


def endpoint_in(out: str, serial: str) -> str | None:
    """Return the `host:port` advertised for `serial`, from `avahi-browse` output.

    Reads the `--parsable` form, whose resolved records start with `=`
    and carry the address and port in the eighth and ninth fields.  Only
    IPv4 is taken: `adb` accepts either, but a link-local IPv6 address
    needs the scope that the output does not carry.
    """
    for line in out.splitlines():
        found, _, proto, name, _, _, _, address, port = (line.split(";") + [""] * 9)[:9]
        if found == "=" and proto == "IPv4" and name.startswith(f"adb-{serial}-"):
            return f"{address}:{port}"
    return None


async def advertised(
    serial: str,
    service_type: str,
    *,
    # The phone check and the pairing flow allow this different amounts of
    # time, so it stays a parameter; process.run owns the expiry itself.
    timeout: float = BROWSE_TIMEOUT,  # noqa: ASYNC109
) -> str | None:
    """Return the `host:port` the phone advertises for one service, or None."""
    out = await run(
        "avahi-browse", "--terminate", "--resolve", "--parsable", service_type, timeout=timeout
    )
    return endpoint_in(out, serial)


def normalise(code: str) -> str | None:
    """Return the typed code as bare digits, or None if it cannot be one.

    The phone shows the code in two groups, and it gets typed back with
    the gap left in, or pasted with a stray newline, so spaces and
    dashes are dropped before it is checked.
    """
    digits = code.strip().replace(" ", "").replace("-", "")
    if len(digits) != CODE_LENGTH or not digits.isdigit():
        return None
    return digits


async def ask_for_code(serial: str) -> str | None:
    """Put the instructions on screen and return the code typed back.

    Returns None if the dialog is cancelled or times out.  Raises
    `ValueError` if what came back cannot be a code, so the caller can
    say so, and `LookupError` if the desktop has no dialog program at
    all, since then there is nowhere to type the code and saying so
    beats waiting.
    """
    text = INSTRUCTIONS.format(serial=serial)
    for before, after in DIALOGS:
        try:
            status, out, err = await run_status(*before, text, *after, timeout=CODE_WAIT)
        except FileNotFoundError:
            continue  # not installed, try the next one
        except TimeoutError:
            log.warning("no pairing code was typed within %.0f seconds", CODE_WAIT)
            return None
        if status != 0:
            # Cancel, or the window closed.  Not an error worth a warning.
            log.info("pairing was cancelled: %s exited %d", before[0], status)
            if err.strip():
                log.debug("%s said: %s", before[0], err.strip())
            return None
        typed = normalise(out)
        if typed is None:
            msg = f"{out.strip()!r} is not a six-digit pairing code"
            raise ValueError(msg)
        return typed
    msg = (
        "neither kdialog nor zenity is installed, so there is nowhere to type"
        " the pairing code; install one, or run camera-sign pair --code CODE"
    )
    raise LookupError(msg)


async def pair_with_code(serial: str, code: str) -> str:
    """Pair with the phone's open pairing screen; return the endpoint used.

    Raises `OSError` if the screen is not open, so nothing advertises a
    pairing port, or if `adb pair` refuses the code.
    """
    endpoint = await advertised(serial, PAIRING_TYPE)
    if endpoint is None:
        msg = (
            "the phone is not advertising a pairing port; open"
            ' "Pair device with pairing code" on it and try again'
        )
        raise OSError(msg)
    status, out, err = await run_status("adb", "pair", endpoint, code, timeout=PAIR_TIMEOUT)
    # adb prints its refusal on standard output and has been known to
    # exit zero doing it, so the text is what decides, not the code.
    if status != 0 or "Successfully paired" not in out:
        detail = out.strip() or err.strip() or f"adb pair exited {status}"
        raise OSError(detail)
    log.info("paired with %s at %s", serial, endpoint)
    return endpoint
