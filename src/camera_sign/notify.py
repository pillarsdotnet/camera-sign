# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
"""Show desktop notifications through the session bus.

Each notification replaces the one before it, so the screen shows only
the latest news.  An urgent one stays up until it is dismissed, on
desktops that honour the urgency hint.
"""

import asyncio
import logging

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

NOTIFY_TIMEOUT = 2.0
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2

log = logging.getLogger("camera-sign")


class Notifier:
    """Post notifications, best effort: a missing desktop is logged, not raised."""

    def __init__(self, app_name: str = "camera-sign"):
        self.app_name = app_name
        self._bus: MessageBus | None = None
        self._last_id = 0
        self._failing = False

    async def _call(self, summary: str, body: str, urgent: bool) -> int:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus().connect()
        urgency = URGENCY_CRITICAL if urgent else URGENCY_NORMAL
        reply = await self._bus.call(
            Message(
                destination="org.freedesktop.Notifications",
                path="/org/freedesktop/Notifications",
                interface="org.freedesktop.Notifications",
                member="Notify",
                signature="susssasa{sv}i",
                body=[
                    self.app_name,
                    self._last_id,
                    "video-display",
                    summary,
                    body,
                    [],
                    {"urgency": Variant("y", urgency)},
                    0 if urgent else -1,
                ],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise OSError(f"{reply.error_name}: {reply.body}")
        return reply.body[0]

    async def show(self, summary: str, body: str = "", urgent: bool = False) -> None:
        """Show a notification in place of the last one."""
        try:
            self._last_id = await asyncio.wait_for(
                self._call(summary, body, urgent), NOTIFY_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 -- best effort, the watcher still works
            # Warn once per outage rather than on every notification.
            level = logging.DEBUG if self._failing else logging.WARNING
            log.log(level, "cannot show a notification: %s", exc)
            self._failing = True
            if self._bus is not None:
                self._bus.disconnect()
                self._bus = None
            return
        self._failing = False
