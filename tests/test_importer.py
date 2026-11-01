"""Tests for lead import: column mapping, cleaning, skips, and speed."""

import io
import json
import time

import pandas as pd
import pytest

from mailer.db import add_suppression, open_conn
from mailer.importer import (
    DEFAULT_COLUMN_MAP,
    ImportFileError,
    import_rows,
    import_test_addresses,
    list_sheets,
    read_table,
)


def row(email, country="United Kingdom", status="Good", priority="1", name="Acme"):
    return {
        "Professional email": email,
        "Business name": name,
        "Product category": "Skincare",
        "Country": country,
        "Location": "London, United Kingdom",
        "Website": "acme.com",
        "Company Instagram": "acme",
        "Email type": "General inbox",
        "Priority": priority,
        "Status": status,
    }


def run_import(rows, mapping=None, sheet="All leads", default_quality=""):
    return import_rows(
        pd.DataFrame(rows, dtype=str),
        mapping or DEFAULT_COLUMN_MAP,
        "leads.xlsx",
        sheet,
        ["Bad"],
        default_quality,
    )


def leads():
    with open_conn() as conn:
        return {r["email"]: dict(r) for r in conn.execute("SELECT * FROM leads")}


def test_default_mapping_fills_every_lead_field():
    result = run_import([row("  Hello@Acme.COM ")])

    assert result.imported == 1
    lead = leads()["hello@acme.com"]
    assert lead["business_name"] == "Acme"
    assert lead["category"] == "Skincare"
    assert lead["country"] == "United Kingdom"
    assert lead["location"] == "London, United Kingdom"
    assert lead["website"] == "acme.com"
    assert lead["instagram"] == "acme"
    assert lead["email_type"] == "General inbox"
    assert lead["priority"] == 1
    assert lead["quality"] == "Good"
    assert lead["language"] == "en"
    assert lead["status"] == "queued"
    assert lead["step"] == 0
    assert lead["import_id"] == result.import_id


def test_custom_column_mapping():
    df_rows = [{"Mail": "a@acme.com", "Company": "Acme", "Land": "France"}]
    mapping = {"email": "Mail", "business_name": "Company", "country": "Land"}

    result = run_import(df_rows, mapping)

    assert result.imported == 1
    lead = leads()["a@acme.com"]
    assert lead["business_name"] == "Acme"
    assert lead["language"] == "fr"
    assert lead["priority"] == 5
    assert lead["quality"] == ""


def test_only_email_is_required():
    assert run_import([{"Mail": "a@acme.com"}], {"email": "Mail"}).imported == 1
    with pytest.raises(ImportFileError):
        run_import([{"Mail": "a@acme.com"}], {"business_name": "Mail"})


def test_mapped_column_must_exist():
    with pytest.raises(ImportFileError):
        run_import([{"Mail": "a@acme.com"}], {"email": "Missing"})


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "a b@acme.com", "a@acme.c", "a@@acme.com"])
def test_invalid_email_is_skipped(email):
    result = run_import([row(email)])

    assert result.imported == 0
    assert result.skipped == {"invalid email": 1}


def test_duplicates_inside_the_file_are_skipped():
    result = run_import([row("a@acme.com"), row("A@ACME.com "), row("b@acme.com")])

    assert result.imported == 2
    assert result.skipped == {"duplicate in file": 1}


def test_already_in_leads_is_skipped_on_second_import():
    run_import([row("a@acme.com")])

    result = run_import([row("a@acme.com"), row("b@acme.com")])

    assert result.imported == 1
    assert result.skipped == {"already in leads": 1}


def test_suppressed_email_is_skipped():
    add_suppression("A@Acme.com", "unsubscribed")

    result = run_import([row("a@acme.com")])

    assert result.imported == 0
    assert result.skipped == {"suppressed": 1}
    assert leads() == {}


def test_bad_quality_is_skipped_case_insensitively():
    result = run_import([row("a@acme.com", status="Bad"), row("b@acme.com", status="bad"), row("c@acme.com", status="Weak")])

    assert result.imported == 1
    assert result.skipped == {"skipped quality": 2}


def test_germany_is_skipped():
    result = run_import([row("a@acme.com", country="Germany")])

    assert result.imported == 0
    assert result.skipped == {"skipped country": 1}


def test_unknown_country_is_imported_and_reported():
    result = run_import([row("a@acme.com", country="Atlantis"), row("b@acme.com", country="Atlantis"), row("c@acme.com", country="")])

    assert result.imported == 3
    assert result.unknown_countries == {"Atlantis": 2, "(blank)": 1}
    assert leads()["a@acme.com"]["language"] == "en"


def test_language_comes_from_country_settings():
    run_import([row("a@acme.com", country="France"), row("b@acme.com", country="Belgium"), row("c@acme.com", country="Spain")])

    assert {email: lead["language"] for email, lead in leads().items()} == {
        "a@acme.com": "fr",
        "b@acme.com": "fr",
        "c@acme.com": "en",
    }


def test_sheet_without_status_column_uses_default_quality():
    no_status = {k: v for k, v in row("a@acme.com").items() if k != "Status"}
    mapping = {**DEFAULT_COLUMN_MAP, "quality": None}

    run_import([no_status], mapping, sheet="Good", default_quality="Good")

    assert leads()["a@acme.com"]["quality"] == "Good"


@pytest.mark.parametrize("raw, expected", [("1", 1), ("3.0", 3), ("", 5), ("high", 5)])
def test_priority_parsing(raw, expected):
    run_import([row("a@acme.com", priority=raw)])

    assert leads()["a@acme.com"]["priority"] == expected


def test_import_is_recorded_with_skip_counts():
    result = run_import([row("a@acme.com"), row("bad")])

    with open_conn() as conn:
        record = conn.execute("SELECT * FROM imports WHERE id = ?", (result.import_id,)).fetchone()
    assert record["filename"] == "leads.xlsx"
    assert record["sheet"] == "All leads"
    assert record["imported_count"] == 1
    assert json.loads(record["skipped_json"]) == {"invalid email": 1}


def test_failed_import_leaves_nothing_behind(monkeypatch):
    run_import([row("a@acme.com")])

    class FailingConn:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def executemany(self, *args):
            raise RuntimeError("disk full")

    with open_conn() as conn:
        with pytest.raises(RuntimeError):
            import_rows(pd.DataFrame([row("b@acme.com")]), DEFAULT_COLUMN_MAP, "x.xlsx", None, ["Bad"], "", FailingConn(conn))
        assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_normal_import_is_not_a_test_lead():
    run_import([row("a@acme.com")])

    assert leads()["a@acme.com"]["is_test"] == 0


def test_import_can_mark_test_leads():
    import_rows(pd.DataFrame([row("a@acme.com")]), DEFAULT_COLUMN_MAP, "t.csv", None, ["Bad"], "", None, True)

    assert leads()["a@acme.com"]["is_test"] == 1


def test_pasted_test_addresses_become_test_leads():
    with open_conn() as conn:
        result = import_test_addresses(" Me+A@Example.com \n\nme+b@example.com\nnot-an-email\n", "United Kingdom", conn)

    assert result.imported == 2
    assert result.skipped == {"invalid email": 1}
    lead = leads()["me+a@example.com"]
    assert lead["is_test"] == 1
    assert lead["quality"] == "Test"
    assert lead["language"] == "en"
    assert lead["business_name"] == "Test Lead"


def test_pasted_address_already_in_the_real_list_is_not_turned_into_a_test_lead():
    run_import([row("real@acme.com")])

    with open_conn() as conn:
        result = import_test_addresses("real@acme.com", "United Kingdom", conn)

    assert result.imported == 0
    assert leads()["real@acme.com"]["is_test"] == 0


def excel_bytes(sheets):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def test_excel_sheet_selection():
    data = excel_bytes({"Good": [row("a@acme.com")], "Weak": [row("b@acme.com"), row("c@acme.com")]})

    assert list_sheets(data) == ["Good", "Weak"]
    assert len(read_table(data, "leads.xlsx", "Weak")) == 2


def test_excel_numbers_stay_strings_and_empty_rows_are_dropped():
    data = excel_bytes({"Good": [row("a@acme.com", priority=1), {key: None for key in row("x")}]})

    df = read_table(data, "leads.xlsx", "Good")

    assert len(df) == 1
    assert df.iloc[0]["Priority"] == "1"


def test_csv_utf8_with_bom_and_stripped_headers():
    data = "﻿ Professional email ,Business name\na@acme.com,Café\n".encode("utf-8")

    df = read_table(data, "leads.csv")

    assert list(df.columns) == ["Professional email", "Business name"]
    assert df.iloc[0]["Business name"] == "Café"


def test_csv_windows_encoding():
    data = "Professional email,Business name\na@acme.com,Café\n".encode("cp1252")

    assert read_table(data, "leads.csv").iloc[0]["Business name"] == "Café"


def test_unsupported_file_type():
    with pytest.raises(ImportFileError):
        read_table(b"x", "leads.txt")


def test_15000_rows_import_under_10_seconds():
    rows = [row(f"lead{i}@acme.com", country="France" if i % 2 else "United States") for i in range(15000)]

    started = time.perf_counter()
    result = run_import(rows)
    elapsed = time.perf_counter() - started

    assert result.imported == 15000
    assert elapsed < 10
