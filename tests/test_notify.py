# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio

from dbus_fast import MessageType

from camera_sign import notify
from camera_sign.notify import Notifier


def test_missing_desktop_warns_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path}/no-bus")

    async def twice():
        notifier = Notifier()
        await notifier.show("one")
        await notifier.show("two")

    asyncio.run(twice())
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "cannot show a notification" in warnings[0].getMessage()


def test_late_reply_still_replaces(monkeypatch):
    # At login the notification server can answer after show() has moved
    # on.  The alert is on screen all the same, so its late id must still
    # be learned, or the next notification piles up beside it instead of
    # replacing it.
    replaces_ids = []

    class FakeReply:
        message_type = MessageType.METHOD_RETURN

        def __init__(self, notification_id):
            self.body = [notification_id]

    class FakeBus:
        connected = True

        async def call(self, message):
            replaces_ids.append(message.body[1])
            if len(replaces_ids) == 1:
                await asyncio.sleep(0.05)  # slower than the show wait below
            return FakeReply(len(replaces_ids) + 40)

    class FakeMessageBus:
        async def connect(self):
            return FakeBus()

    monkeypatch.setattr(notify, "MessageBus", FakeMessageBus)
    monkeypatch.setattr(notify, "SHOW_WAIT", 0.01)

    async def outage_then_recovery():
        notifier = Notifier()
        await notifier.show("phone is not answering", urgent=True)  # reply comes late
        await notifier.show("phone is answering again")
        await asyncio.wait({notifier._chain})

    asyncio.run(outage_then_recovery())
    assert replaces_ids == [0, 41]  # the second show replaced the first, late id and all
