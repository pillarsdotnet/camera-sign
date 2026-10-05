# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
import asyncio

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
