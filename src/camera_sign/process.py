# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
"""Run a child process and collect its output.

The phone checks and the pairing flow both shell out -- to `adb`, to
`avahi-browse`, to whichever dialog the desktop has -- and both need the
same two things.  A hard timeout, because the watch loop runs once a
second and must never be left waiting on a child that has hung.  And the
child's standard error in the exception, because a failure that reaches
the journal as "it did not work" costs an afternoon to diagnose.

`run` is for a command that is expected to succeed, where a non-zero
exit is an error.  `run_status` is for one where it is not: a dialog
exits non-zero when the person presses Cancel, and `adb pair` prints its
refusal on standard output, so both need the code and the output rather
than an exception.

Each takes the timeout rather than leaving it to `asyncio.timeout` at
the call site, because expiry has to kill the child.  A cancellation
arriving from outside unwinds this coroutine and leaves the process
running, holding its pipes; the timeout is owned here so that it does
not.
"""

import asyncio

__all__ = ["run", "run_status"]


async def run_status(
    *argv: str,
    # Owned here rather than left to asyncio.timeout at the call site: see
    # the module docstring -- expiry has to kill the child, and a
    # cancellation from outside would leave it running.
    timeout: float,  # noqa: ASYNC109
) -> tuple[int, str, str]:
    """Return the command's exit code, standard output and standard error.

    Raises `FileNotFoundError` if the command is not installed, and
    `TimeoutError` if it outlives `timeout`, killing it first so no
    child is left behind.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    # returncode is set once communicate() has reaped the child.
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def run(*argv: str, timeout: float) -> str:  # noqa: ASYNC109 -- as run_status above
    """Return the command's standard output, raising if it fails.

    A non-zero exit raises `OSError` carrying the command's standard
    error, or a plain mention of the code when it said nothing.
    """
    status, out, err = await run_status(*argv, timeout=timeout)
    if status != 0:
        raise OSError(err.strip() or f"{argv[0]} exited {status}")
    return out
