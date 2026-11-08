# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
"""Show desktop notifications through the session bus.

Each notification replaces the one before it, so the screen shows only
the latest news.  An urgent one stays up until it is dismissed, on
desktops that honour the urgency hint.

The notification server can be slow to answer -- at login it may still
be starting -- so a show waits for it only briefly and then carries on,
while the call runs to completion in the background.  Shows are chained,
each waiting for the reply to the one before, so even a late reply
yields the id on screen and the next notification replaces that one
instead of piling up beside it.

A notification can carry buttons.  Each is a key, a label and something
to await when it is pressed; the server reports the press back as an
`ActionInvoked` signal, which is matched only once a notification has
asked for a button, so the plain path adds no bus traffic.  A desktop
whose server does not list the "actions" capability would show the
button nowhere, so the buttons are dropped and the text still appears.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

SHOW_WAIT = 2.0  # how long show() waits for the server before carrying on
REPLY_TIMEOUT = 30.0  # how long a call may go on waiting in the background
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2
INTERFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
ACTION_MATCH = f"type='signal',interface='{INTERFACE}',member='ActionInvoked'"

# A button: the key the server hands back, the label it shows, and what
# to await when it is pressed.
Action = tuple[str, str, Callable[[], Awaitable[None]]]

log = logging.getLogger("camera-sign")


class Notifier:
    """Post notifications, best effort: a missing desktop is logged, not raised."""

    def __init__(self, app_name: str = "camera-sign") -> None:
        """Remember the app name; the bus is connected on the first show."""
        self.app_name = app_name
        self._bus: MessageBus | None = None
        self._last_id = 0
        self._failing = False
        self._chain: asyncio.Task | None = None
        self._actions: dict[str, Callable[[], Awaitable[None]]] = {}
        self._listening = False
        self._supports_actions: bool | None = None
        self._tasks: set[asyncio.Task] = set()

    async def _ask(self, member: str, signature: str = "", body: list | None = None) -> list:
        """Call one method on the notification server, raising on an error reply."""
        # Every caller connects the bus before asking anything of it.
        reply = await self._bus.call(  # type: ignore[union-attr]
            Message(
                destination=INTERFACE,
                path=PATH,
                interface=INTERFACE,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        if reply is None:  # a no-reply call, which none of these are
            return []
        if reply.message_type == MessageType.ERROR:
            msg = f"{reply.error_name}: {reply.body}"
            raise OSError(msg)
        return reply.body

    async def _start_listening(self) -> None:
        """Ask the bus for `ActionInvoked` signals, once per connection."""
        if self._listening:
            return
        # Registered only now, on the first notification that wants a
        # button, so a watcher that never shows one adds no match rule.
        self._bus.add_message_handler(self._on_signal)  # type: ignore[union-attr]
        await self._bus.call(  # type: ignore[union-attr]
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[ACTION_MATCH],
            )
        )
        self._listening = True

    def _on_signal(self, message: Message) -> None:
        """Run the callback of a button the person pressed."""
        if message.message_type is not MessageType.SIGNAL:
            return
        if message.interface != INTERFACE or message.member != "ActionInvoked":
            return
        notification_id, key = message.body[0], message.body[1]
        callback = self._actions.get(key)
        # A press on an older notification, or on a button this one does
        # not carry, is not ours to act on.
        if callback is None or notification_id != self._last_id:
            return
        task = asyncio.create_task(self._invoke(key, callback))
        # Hold a reference, or the loop may collect the task mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _invoke(self, key: str, callback: Callable[[], Awaitable[None]]) -> None:
        try:
            await callback()
        except Exception:
            # A button's work failing must not take the watcher with it.
            log.exception("the %s button failed", key)

    async def _call(
        self, summary: str, body: str, actions: Sequence[Action], *, urgent: bool
    ) -> int:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus().connect()
            # A new connection carries neither the handler nor the match.
            self._listening = False
            self._supports_actions = None
        if actions:
            if self._supports_actions is None:
                capabilities = await self._ask("GetCapabilities")
                self._supports_actions = "actions" in (capabilities[0] if capabilities else [])
                if not self._supports_actions:
                    log.warning(
                        "this desktop's notification server shows no buttons;"
                        " re-pair by hand with camera-sign pair"
                    )
            if self._supports_actions:
                await self._start_listening()
            else:
                actions = ()
        urgency = URGENCY_CRITICAL if urgent else URGENCY_NORMAL
        flat: list[str] = []
        for key, label, _ in actions:
            flat += [key, label]
        reply = await self._ask(
            "Notify",
            "susssasa{sv}i",
            [
                self.app_name,
                self._last_id,
                "video-display",
                summary,
                body,
                flat,
                {"urgency": Variant("y", urgency)},
                0 if urgent else -1,
            ],
        )
        self._actions = {key: callback for key, _, callback in actions}
        return reply[0]

    async def _show(
        self,
        prev: asyncio.Task | None,
        summary: str,
        body: str,
        actions: Sequence[Action],
        *,
        urgent: bool,
    ) -> None:
        if prev is not None:
            # Wait for the last reply, however late, so _last_id names
            # the notification on screen and this one replaces it.
            await asyncio.wait({prev})
        try:
            self._last_id = await asyncio.wait_for(
                self._call(summary, body, actions, urgent=urgent), REPLY_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 -- best effort, the watcher still works
            # Warn once per outage rather than on every notification.
            level = logging.DEBUG if self._failing else logging.WARNING
            log.log(level, "cannot show a notification: %s", str(exc) or type(exc).__name__)
            self._failing = True
            if self._bus is not None:
                self._bus.disconnect()
                self._bus = None
            return
        self._failing = False

    async def show(
        self,
        summary: str,
        body: str = "",
        *,
        urgent: bool = False,
        actions: Sequence[Action] = (),
    ) -> None:
        """Show a notification in place of the last one, waiting only briefly."""
        self._chain = task = asyncio.create_task(
            self._show(self._chain, summary, body, actions, urgent=urgent)
        )
        await asyncio.wait({task}, timeout=SHOW_WAIT)

    async def close(self) -> None:
        """Dismiss the notification on screen, if one is still up."""
        if self._chain is not None:
            # The id of a notification whose reply is still in flight is
            # not known yet, and that is the one to close.
            await asyncio.wait({self._chain})
        self._actions = {}
        if self._last_id == 0 or self._bus is None or not self._bus.connected:
            return
        try:
            await asyncio.wait_for(
                self._ask("CloseNotification", "u", [self._last_id]), REPLY_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 -- the sign still works with a stale alert up
            log.warning("cannot dismiss the notification: %s", str(exc) or type(exc).__name__)
            return
        self._last_id = 0
