"""Tests for the worker lock and loop."""

import datetime
import os
import subprocess
import sys

import pytest

import mailer.db as db
import worker
from mailer.config import PROJECT_ROOT, ConfigError, Settings
from mailer.db import get_app_state, set_app_state, to_iso


def set_lock(pid, age_seconds=0):
    heartbeat = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_seconds)
    set_app_state("worker_pid", str(pid))
    set_app_state("worker_heartbeat", to_iso(heartbeat))


@pytest.fixture
def loop(monkeypatch):
    """Run run_loop with a scripted app window, no sleeping, and a counting tick."""

    class Loop:
        windows = [True, True, False]
        ticks = []
        reloads = 0
        tick_result = staticmethod(lambda: False)

    state = Loop()
    state.ticks = []
    state.windows = [True, True, False]

    def fake_tick(settings):
        state.ticks.append(settings)
        return state.tick_result()

    def fake_load():
        state.reloads += 1
        return Settings()

    monkeypatch.setattr(worker.engine, "tick", fake_tick)
    monkeypatch.setattr(worker, "load_settings", fake_load)
    monkeypatch.setattr(worker, "in_app_window", lambda settings=None: state.windows.pop(0))
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    return state


def test_worker_module_does_not_load_streamlit():
    code = "import sys, worker; print('streamlit' in sys.modules)"
    output = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert output.stdout.strip() == "False"


def test_lock_taken_when_free():
    assert worker.acquire_worker_lock() is True
    assert get_app_state("worker_pid") == str(os.getpid())


def test_lock_refused_while_another_worker_is_alive():
    set_lock(os.getpid())
    assert worker.acquire_worker_lock() is False


def test_lock_taken_when_heartbeat_is_stale():
    set_lock(os.getpid(), age_seconds=120)
    assert worker.acquire_worker_lock() is True


def test_lock_taken_when_process_is_dead(monkeypatch):
    set_lock(99999)
    monkeypatch.setattr(worker, "is_pid_alive", lambda pid: False)
    assert worker.acquire_worker_lock() is True


def test_release_clears_the_lock():
    worker.acquire_worker_lock()
    worker.release_worker_lock()
    assert get_app_state("worker_pid") is None
    assert get_app_state("worker_heartbeat") is None


def test_release_leaves_another_workers_lock_alone():
    set_lock(99999)
    worker.release_worker_lock()
    assert get_app_state("worker_pid") == "99999"


def test_run_loop_works_on_a_brand_new_database(loop, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fresh.db")
    worker.run_loop()
    assert len(loop.ticks) == 2


def test_once_runs_one_tick_and_leaves_no_heartbeat(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.engine, "tick", lambda settings: calls.append(settings) or False)

    worker.run_once()

    assert len(calls) == 1
    assert get_app_state("worker_heartbeat") is None


def test_once_refuses_while_a_worker_is_running(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.engine, "tick", lambda settings: calls.append(settings) or False)
    set_lock(os.getpid())

    worker.run_once()

    assert calls == []


def test_run_loop_exits_at_once_outside_the_window(loop):
    loop.windows = [False]
    worker.run_loop()
    assert loop.ticks == []
    assert get_app_state("worker_pid") is None


def test_run_loop_rereads_settings_every_loop(loop):
    worker.run_loop()
    assert loop.reloads == 3
    assert loop.ticks[0] is not loop.ticks[1]


def test_run_loop_keeps_last_good_settings_when_the_file_breaks(loop, monkeypatch):
    results = iter([Settings(), ConfigError("bad"), Settings()])

    def flaky_load():
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(worker, "load_settings", flaky_load)
    worker.run_loop()
    assert len(loop.ticks) == 2


def test_run_loop_survives_a_failing_tick(loop):
    failures = iter([ValueError("boom")])

    def tick_result():
        error = next(failures, None)
        if error:
            raise error
        return True

    loop.tick_result = staticmethod(tick_result)
    worker.run_loop()
    assert len(loop.ticks) == 2


def test_run_loop_does_not_tick_while_paused(loop):
    set_app_state("paused_all", "1")
    worker.run_loop()
    assert loop.ticks == []


def test_run_loop_refuses_when_another_worker_runs(loop):
    set_lock(os.getpid())
    worker.run_loop()
    assert loop.ticks == []
    assert get_app_state("worker_pid") == str(os.getpid())
