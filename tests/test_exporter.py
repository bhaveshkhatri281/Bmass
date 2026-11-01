"""Tests for the Excel exports."""

import pandas as pd
import pytest

import mailer.exporter as exporter
from mailer.db import open_conn
from mailer.exporter import export_leads, export_send_log


@pytest.fixture(autouse=True)
def export_dir(tmp_path, monkeypatch):
    target = tmp_path / "exports"
    monkeypatch.setattr(exporter, "EXPORT_DIR", target)
    return target


def add_rows(conn):
    conn.execute("INSERT INTO inboxes (id, email, display_name, ramp_start_date, created_at) VALUES (1, 'a@mobisa.in', 'A', '2026-11-01', 'x')")
    conn.execute(
        """INSERT INTO leads (id, email, business_name, country, status, step, created_at, updated_at)
           VALUES (1, 'lead@acme.com', '=HYPERLINK("x")', 'France', 'replied', 2, 'x', 'x')"""
    )
    conn.executemany(
        "INSERT INTO send_log (ts, inbox_id, lead_id, lead_email, step_no, subject, result) VALUES (?, 1, 1, 'lead@acme.com', ?, 'idea', ?)",
        [("2026-11-04T15:00:00Z", 1, "sent"), ("2026-11-05T15:00:00Z", 2, "dry_run")],
    )


def test_export_leads_writes_a_timestamped_excel_file(export_dir):
    with open_conn() as conn:
        add_rows(conn)
        path = export_leads(conn)

    assert path.parent == export_dir
    assert path.name.startswith("leads_") and path.suffix == ".xlsx"
    df = pd.read_excel(path)
    assert df.loc[0, "email"] == "lead@acme.com"
    assert df.loc[0, "status"] == "replied"
    assert df.loc[0, "step"] == 2


def test_export_stops_text_running_as_a_formula():
    with open_conn() as conn:
        add_rows(conn)
        path = export_leads(conn)

    assert pd.read_excel(path).loc[0, "business_name"].startswith("'=")


def test_export_send_log_includes_the_inbox_address():
    with open_conn() as conn:
        add_rows(conn)
        path = export_send_log(conn)

    df = pd.read_excel(path)
    assert len(df) == 2
    assert set(df["inbox"]) == {"a@mobisa.in"}
    assert set(df["result"]) == {"sent", "dry_run"}


def test_export_send_log_applies_filters():
    with open_conn() as conn:
        add_rows(conn)
        path = export_send_log(conn, {"result": "sent"})

    assert list(pd.read_excel(path)["result"]) == ["sent"]


def test_export_with_no_rows_still_makes_a_file():
    with open_conn() as conn:
        assert export_send_log(conn).exists()
