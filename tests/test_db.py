"""Tests for DB connections and seed data."""

import threading

import pytest

from mailer.db import get_app_state, get_conn, open_conn, set_app_state
from mailer.seed import DEFAULT_STEPS


def test_dashboard_connection_works_across_threads():
    conn = get_conn(check_same_thread=False)
    results = []
    thread = threading.Thread(target=lambda: results.append(conn.execute("SELECT 1").fetchone()[0]))
    thread.start()
    thread.join()
    conn.close()
    assert results == [1]


def test_open_conn_commits_and_closes():
    with open_conn() as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('k', 'v')")

    assert get_app_state("k") == "v"
    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")


def test_app_state_upsert():
    set_app_state("paused_all", "1")
    set_app_state("paused_all", "0")
    assert get_app_state("paused_all") == "0"


def test_default_steps_are_day_0_3_7():
    delays = [delay for _, delay, _ in DEFAULT_STEPS]
    assert delays == [0, 3, 4]
    assert [sum(delays[: i + 1]) for i in range(len(delays))] == [0, 3, 7]

    with open_conn() as conn:
        rows = conn.execute("SELECT delay_days FROM steps ORDER BY step_no").fetchall()
    assert [r["delay_days"] for r in rows] == delays
