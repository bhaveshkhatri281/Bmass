"""Tests for the daily log file and its 30-day cleanup."""

import datetime
import logging

import pytest

from mailer.logs import DailyFileHandler


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        today = datetime.date(2026, 11, 4)

    monkeypatch.setattr(DailyFileHandler, "_today", lambda self: Clock.today)
    return Clock


def record(message):
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)


def test_writes_to_todays_file(tmp_path, clock):
    handler = DailyFileHandler(tmp_path)
    handler.emit(record("hello"))
    handler.close()

    assert "hello" in (tmp_path / "mailer_2026-11-04.log").read_text(encoding="utf-8")


def test_moves_to_a_new_file_at_midnight(tmp_path, clock):
    handler = DailyFileHandler(tmp_path)
    handler.emit(record("before midnight"))

    clock.today = datetime.date(2026, 11, 5)
    handler.emit(record("after midnight"))
    handler.close()

    assert "after midnight" not in (tmp_path / "mailer_2026-11-04.log").read_text(encoding="utf-8")
    assert "after midnight" in (tmp_path / "mailer_2026-11-05.log").read_text(encoding="utf-8")


def test_keeps_30_days_and_leaves_other_files(tmp_path, clock):
    old = tmp_path / "mailer_2026-10-04.log"
    kept = tmp_path / "mailer_2026-10-05.log"
    other = tmp_path / "notes.log"
    for path in (old, kept, other):
        path.write_text("x", encoding="utf-8")

    DailyFileHandler(tmp_path).close()

    assert not old.exists()
    assert kept.exists()
    assert other.exists()
