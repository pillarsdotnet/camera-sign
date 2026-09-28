# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
from camera_sign.debounce import Debouncer


def test_brief_open_is_ignored():
    d = Debouncer(on_delay=2, off_delay=5)
    assert d.update(True, 0) is False
    assert d.update(True, 1) is False
    assert d.update(False, 1.5) is False
    assert d.update(True, 3) is False  # the clock restarts after a gap


def test_lights_after_on_delay():
    d = Debouncer(on_delay=2, off_delay=5)
    d.update(True, 0)
    assert d.update(True, 2) is True


def test_brief_close_keeps_it_lit():
    d = Debouncer(on_delay=2, off_delay=5, lit=True)
    assert d.update(False, 0) is True
    assert d.update(False, 4) is True
    assert d.update(True, 4.5) is True
    assert d.update(False, 6) is True  # the clock restarts after a gap


def test_goes_dark_after_off_delay():
    d = Debouncer(on_delay=2, off_delay=5, lit=True)
    d.update(False, 10)
    assert d.update(False, 15) is False
