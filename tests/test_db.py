"""Tests for DB connections and seed data."""

import threading

import pytest

import sqlite3

from mailer.db import (
    SCHEMA_SQL,
    add_suppression,
    get_app_state,
    get_conn,
    init_db,
    is_suppressed,
    list_leads,
    list_send_log,
    open_conn,
    remove_test_leads,
    reset_lead,
    set_app_state,
    set_custom_line,
    set_events_handled,
    suppress_lead,
)
from mailer.seed import CATEGORY_PHRASE_GROUPS, DEFAULT_STEPS


def add_lead(conn, email, **values):
    columns = {"email": email, "created_at": "x", "updated_at": "x", **values}
    cursor = conn.execute(
        f"INSERT INTO leads ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        list(columns.values()),
    )
    return cursor.lastrowid


def test_old_database_gains_the_test_lead_column(tmp_path):
    new_column = ",\n  is_test          INTEGER NOT NULL DEFAULT 0"
    assert new_column in SCHEMA_SQL

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL.replace(new_column, ""))
    conn.execute("INSERT INTO leads (email, created_at, updated_at) VALUES ('real@acme.com', 'x', 'x')")
    conn.commit()
    conn.close()

    init_db(path)
    init_db(path)

    with open_conn(path) as migrated:
        assert migrated.execute("SELECT is_test FROM leads WHERE email = 'real@acme.com'").fetchone()[0] == 0


def test_suppress_lead_stops_it():
    with open_conn() as conn:
        lead_id = add_lead(conn, "a@acme.com")
        suppress_lead(lead_id, conn)

        assert is_suppressed("a@acme.com", conn)
        assert conn.execute("SELECT status FROM leads").fetchone()[0] == "skipped"
        assert conn.execute("SELECT reason FROM suppression").fetchone()[0] == "manual"


def test_reset_works_only_for_a_lead_never_emailed():
    with open_conn() as conn:
        lead_id = add_lead(conn, "a@acme.com", status="skipped")
        assert reset_lead(lead_id, conn) is None
        assert conn.execute("SELECT status FROM leads").fetchone()[0] == "queued"

        emailed = add_lead(conn, "b@acme.com", status="finished", step=1)
        assert "already emailed" in reset_lead(emailed, conn)

        logged = add_lead(conn, "c@acme.com", status="skipped")
        conn.execute("INSERT INTO send_log (ts, lead_id, lead_email, step_no, result) VALUES ('x', ?, 'c@acme.com', 1, 'sent')", (logged,))
        assert "already emailed" in reset_lead(logged, conn)


def test_dry_run_does_not_block_a_reset():
    with open_conn() as conn:
        lead_id = add_lead(conn, "a@acme.com", status="skipped")
        conn.execute("INSERT INTO send_log (ts, lead_id, lead_email, step_no, result) VALUES ('x', ?, 'a@acme.com', 1, 'dry_run')", (lead_id,))

        assert reset_lead(lead_id, conn) is None


def test_reset_refuses_a_suppressed_lead():
    with open_conn() as conn:
        lead_id = add_lead(conn, "a@acme.com", status="skipped")
        add_suppression("a@acme.com", "unsubscribed", conn)

        assert "suppression" in reset_lead(lead_id, conn)


def test_custom_line_is_saved_trimmed():
    with open_conn() as conn:
        lead_id = add_lead(conn, "a@acme.com")
        set_custom_line(lead_id, "  Loved your range. ", conn)

        assert conn.execute("SELECT custom_line FROM leads").fetchone()[0] == "Loved your range."


def test_remove_test_leads_clears_them_and_leaves_real_leads():
    with open_conn() as conn:
        real = add_lead(conn, "real@acme.com")
        test = add_lead(conn, "me@example.com", is_test=1)
        conn.execute("INSERT INTO send_log (ts, lead_id, lead_email, step_no, result) VALUES ('x', ?, 'me@example.com', 1, 'sent')", (test,))
        conn.execute("INSERT INTO send_log (ts, lead_id, lead_email, step_no, result) VALUES ('x', ?, 'real@acme.com', 1, 'dry_run')", (real,))
        conn.execute("INSERT INTO events (ts, lead_id, type, gmail_message_id) VALUES ('x', ?, 'reply', 'm1')", (test,))
        add_suppression("me@example.com", "unsubscribed", conn)
        add_suppression("real@acme.com", "manual", conn)

        assert remove_test_leads(conn) == 1

        assert [r[0] for r in conn.execute("SELECT email FROM leads")] == ["real@acme.com"]
        assert [r[0] for r in conn.execute("SELECT lead_email FROM send_log")] == ["real@acme.com"]
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT email FROM suppression")] == ["real@acme.com"]


def test_lead_filters_and_paging():
    with open_conn() as conn:
        add_lead(conn, "a@acme.com", business_name="Acme", country="France", status="queued", quality="Good")
        add_lead(conn, "b@beta.com", business_name="Beta", country="France", status="replied", quality="Weak", step=1)
        add_lead(conn, "c@gamma.com", business_name="Gamma", country="Spain", status="queued", quality="Good", is_test=1)

        emails = lambda filters: [r["email"] for r in list_leads(filters, 100, 0, conn)[0]]
        assert len(emails({})) == 3
        assert emails({"status": "replied"}) == ["b@beta.com"]
        assert emails({"country": "Spain"}) == ["c@gamma.com"]
        assert emails({"quality": "Weak"}) == ["b@beta.com"]
        assert emails({"step": 1}) == ["b@beta.com"]
        assert emails({"search": "acme"}) == ["a@acme.com"]
        assert emails({"search": "gamma"}) == ["c@gamma.com"]
        assert emails({"is_test": True}) == ["c@gamma.com"]

        rows, total = list_leads({}, 1, 1, conn)
        assert total == 3
        assert [r["email"] for r in rows] == ["b@beta.com"]


def test_send_log_filters():
    with open_conn() as conn:
        conn.execute("INSERT INTO inboxes (id, email, display_name, ramp_start_date, created_at) VALUES (1, 'a@m.in', 'A', 'x', 'x')")
        conn.executemany(
            "INSERT INTO send_log (ts, inbox_id, lead_email, step_no, result) VALUES (?, ?, 'x@y.z', 1, ?)",
            [("2026-11-01T00:00:00Z", 1, "sent"), ("2026-11-02T00:00:00Z", None, "error"), ("2026-11-03T00:00:00Z", 1, "dry_run")],
        )

        results = lambda filters: [r["result"] for r in list_send_log(filters, 100, 0, conn)[0]]
        assert results({}) == ["dry_run", "error", "sent"]
        assert results({"result": "sent"}) == ["sent"]
        assert results({"inbox_id": 1}) == ["dry_run", "sent"]
        assert results({"date_from": "2026-11-02T00:00:00Z", "date_to": "2026-11-03T00:00:00Z"}) == ["error"]
        assert list_send_log({}, 1, 0, conn)[1] == 3
        assert len(list_send_log({}, None, 0, conn)[0]) == 3


def test_events_handled_tick():
    with open_conn() as conn:
        conn.execute("INSERT INTO events (id, ts, type, gmail_message_id) VALUES (1, 'x', 'reply', 'm1')")
        set_events_handled({1: True}, conn)
        assert conn.execute("SELECT handled FROM events").fetchone()[0] == 1
        set_events_handled({1: False}, conn)
        assert conn.execute("SELECT handled FROM events").fetchone()[0] == 0


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
