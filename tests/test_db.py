"""Tests for DB connections and seed data."""

import threading

import pytest

from mailer.db import get_app_state, get_conn, open_conn, set_app_state
from mailer.seed import CATEGORY_PHRASE_GROUPS, DEFAULT_STEPS


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


def test_category_phrases_are_seeded_lowercase_without_duplicates():
    keys = [key for group_keys, _, _ in CATEGORY_PHRASE_GROUPS for key in group_keys]
    assert len(keys) == len(set(keys))
    assert all(key == key.lower().strip() for key in keys)
    assert all(en == en.lower() and fr == fr.lower() for _, en, fr in CATEGORY_PHRASE_GROUPS)

    with open_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM category_phrases").fetchone()[0] == len(keys)


def test_default_steps_are_day_0_3_7():
    delays = [delay for _, delay, _ in DEFAULT_STEPS]
    assert delays == [0, 3, 4]
    assert [sum(delays[: i + 1]) for i in range(len(delays))] == [0, 3, 7]

    with open_conn() as conn:
        rows = conn.execute("SELECT delay_days FROM steps ORDER BY step_no").fetchall()
    assert [r["delay_days"] for r in rows] == delays
