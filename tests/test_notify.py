# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio

from dbus_fast import Message, MessageType

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


class FakeReply:
    message_type = MessageType.METHOD_RETURN

    def __init__(self, body):
        self.body = body


class ActionBus:
    """A notification server that answers, and remembers what it was asked."""

    connected = True

    def __init__(self, capabilities=("actions", "body")):
        self.calls = []
        self.handlers = []
        self.capabilities = list(capabilities)

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    async def call(self, message):
        self.calls.append(message)
        if message.member == "GetCapabilities":
            return FakeReply([self.capabilities])
        if message.member == "Notify":
            return FakeReply([7])
        return FakeReply([])

    def members(self):
        return [message.member for message in self.calls]

    def notify_body(self):
        for message in self.calls:
            if message.member == "Notify":
                return message.body
        msg = "nothing was shown"
        raise AssertionError(msg)


def with_bus(monkeypatch, bus):
    class FakeMessageBus:
        async def connect(self):
            return bus

    monkeypatch.setattr(notify, "MessageBus", FakeMessageBus)
    return bus


def invoked(notification_id, key):
    """The signal a notification server sends when a button is pressed."""
    return Message(
        message_type=MessageType.SIGNAL,
        path=notify.PATH,
        interface=notify.INTERFACE,
        member="ActionInvoked",
        signature="us",
        body=[notification_id, key],
    )


def test_a_button_reaches_the_server_and_its_press_comes_back(monkeypatch):
    bus = with_bus(monkeypatch, ActionBus())
    pressed = []

    async def on_press():
        pressed.append(True)

    async def show_then_press():
        notifier = Notifier()
        await notifier.show("phone", "body", urgent=True, actions=[("repair", "Re-pair", on_press)])
        await asyncio.wait({notifier._chain})
        # The server is sent flat key, label pairs; the callback stays here.
        assert bus.notify_body()[5] == ["repair", "Re-pair"]
        for handler in bus.handlers:
            handler(invoked(7, "repair"))
        await asyncio.sleep(0)  # let the callback's task run
        await asyncio.gather(*notifier._tasks)

    asyncio.run(show_then_press())
    assert pressed == [True]
    assert "AddMatch" in bus.members()


def test_a_press_on_another_notification_is_ignored(monkeypatch):
    bus = with_bus(monkeypatch, ActionBus())
    pressed = []

    async def on_press():
        pressed.append(True)

    async def show_then_press():
        notifier = Notifier()
        await notifier.show("phone", actions=[("repair", "Re-pair", on_press)])
        await asyncio.wait({notifier._chain})
        for handler in bus.handlers:
            handler(invoked(999, "repair"))  # some other app's notification
            handler(invoked(7, "other"))  # a button this one does not carry
        await asyncio.sleep(0)

    asyncio.run(show_then_press())
    assert pressed == []


def test_a_server_without_buttons_still_shows_the_text(monkeypatch, caplog):
    bus = with_bus(monkeypatch, ActionBus(capabilities=("body",)))

    async def on_press():
        raise AssertionError  # pragma: no cover -- never reachable here

    async def show():
        notifier = Notifier()
        await notifier.show("phone", "body", actions=[("repair", "Re-pair", on_press)])
        await asyncio.wait({notifier._chain})

    asyncio.run(show())
    assert bus.notify_body()[5] == []  # no buttons offered
    assert bus.notify_body()[3] == "phone"  # but the alert still arrived
    assert "shows no buttons" in caplog.text
    # Nothing to listen for, so no match rule was added.
    assert "AddMatch" not in bus.members()


def test_close_dismisses_what_is_on_screen(monkeypatch):
    bus = with_bus(monkeypatch, ActionBus())

    async def show_then_close():
        notifier = Notifier()
        await notifier.show("phone", urgent=True)
        await notifier.close()
        return notifier

    notifier = asyncio.run(show_then_close())
    closed = [m for m in bus.calls if m.member == "CloseNotification"]
    assert [m.body for m in closed] == [[7]]
    # The id is spent: closing twice must not dismiss a stranger's alert.
    assert notifier._last_id == 0
