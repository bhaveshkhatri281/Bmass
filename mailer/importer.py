"""Lead import from Excel/CSV: cleaning, dedupe, suppression check, and summary."""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

from mailer.db import open_conn, transaction, utc_now_iso

logger = logging.getLogger("importer")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Lead field -> default column name, from Mobisa's merged sheet.
DEFAULT_COLUMN_MAP = {
    "email": "Professional email",
    "business_name": "Business name",
    "category": "Product category",
    "country": "Country",
    "location": "Location",
    "website": "Website",
    "instagram": "Company Instagram",
    "email_type": "Email type",
    "priority": "Priority",
    "quality": "Status",
}

# Sheets named after a quality carry no Status column, so the name is the quality.
QUALITY_SHEETS = ("Good", "Weak")

DEFAULT_PRIORITY = 5
DEFAULT_LANGUAGE = "en"
BLANK_COUNTRY = "(blank)"

TEST_LEAD_NAME = "Test Lead"
TEST_LEAD_CATEGORY = "skincare"
TEST_LEAD_FILENAME = "Test leads (pasted)"
TEST_LEAD_QUALITY = "Test"


class ImportFileError(ValueError):
    """Raised when a file cannot be read or mapped."""


@dataclass
class ImportResult:
    import_id: int
    imported: int
    skipped: Dict[str, int] = field(default_factory=dict)
    unknown_countries: Dict[str, int] = field(default_factory=dict)


def list_sheets(data: bytes) -> List[str]:
    """Return the sheet names of an .xlsx file."""
    try:
        with pd.ExcelFile(io.BytesIO(data), engine="openpyxl") as workbook:
            return list(workbook.sheet_names)
    except zipfile.BadZipFile:
        raise ImportFileError("This is not a valid .xlsx file.")


def read_table(data: bytes, filename: str, sheet: Optional[str] = None) -> pd.DataFrame:
    """Read an .xlsx sheet or a .csv into a DataFrame of strings, dropping fully empty rows."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        try:
            df = pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0, dtype=str, keep_default_na=False, engine="openpyxl")
        except zipfile.BadZipFile:
            raise ImportFileError("This is not a valid .xlsx file.")
    elif suffix == ".csv":
        df = _read_csv(data)
    else:
        raise ImportFileError(f"Unsupported file type: {suffix or filename}. Use .xlsx or .csv.")

    df.columns = [str(column).strip() for column in df.columns]
    return df[df.astype(str).apply(lambda column: column.str.strip() != "").any(axis=1)]


def _read_csv(data: bytes) -> pd.DataFrame:
    # Excel on Windows saves CSV as cp1252 unless told otherwise.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ImportFileError("Could not read the CSV file. Save it as UTF-8.")


def _parse_priority(value: str) -> int:
    try:
        return int(float(value))
    except ValueError:
        return DEFAULT_PRIORITY


def _column(df: pd.DataFrame, mapping: Mapping[str, Optional[str]], lead_field: str) -> List[str]:
    column = mapping.get(lead_field)
    if not column:
        return [""] * len(df)
    if column not in df.columns:
        raise ImportFileError(f"Column '{column}' is not in this file.")
    return [str(value).strip() for value in df[column].tolist()]


def import_rows(
    df: pd.DataFrame,
    mapping: Mapping[str, Optional[str]],
    filename: str,
    sheet: Optional[str],
    skip_quality: Sequence[str],
    default_quality: str = "",
    conn: Optional[sqlite3.Connection] = None,
    is_test: bool = False,
) -> ImportResult:
    """Clean, filter and insert rows in one transaction, and record the import."""
    if not mapping.get("email"):
        raise ImportFileError("The email column must be mapped.")
    if conn is None:
        with open_conn() as c:
            return import_rows(df, mapping, filename, sheet, skip_quality, default_quality, c, is_test)

    columns = {lead_field: _column(df, mapping, lead_field) for lead_field in DEFAULT_COLUMN_MAP}

    existing = {row[0] for row in conn.execute("SELECT email FROM leads")}
    suppressed = {row[0].lower() for row in conn.execute("SELECT email FROM suppression")}
    countries = {row["country"].strip().lower(): row for row in conn.execute("SELECT * FROM country_settings")}
    skip_qualities = {quality.strip().lower() for quality in skip_quality}

    skipped: Dict[str, int] = {}
    unknown_countries: Dict[str, int] = {}
    seen: set[str] = set()
    now = utc_now_iso()
    rows = []

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for i, raw_email in enumerate(columns["email"]):
        email = raw_email.lower()
        if not EMAIL_RE.match(email):
            skip("invalid email")
            continue
        if email in seen:
            skip("duplicate in file")
            continue
        seen.add(email)
        if email in existing:
            skip("already in leads")
            continue
        if email in suppressed:
            skip("suppressed")
            continue

        quality = columns["quality"][i] or default_quality.strip()
        if quality.lower() in skip_qualities:
            skip("skipped quality")
            continue

        country = columns["country"][i]
        setting = countries.get(country.lower())
        if setting is not None and setting["skip"]:
            skip("skipped country")
            continue
        if setting is None:
            label = country or BLANK_COUNTRY
            unknown_countries[label] = unknown_countries.get(label, 0) + 1

        rows.append((
            email,
            columns["business_name"][i],
            columns["category"][i],
            country,
            columns["location"][i],
            columns["website"][i],
            columns["instagram"][i],
            columns["email_type"][i],
            _parse_priority(columns["priority"][i]),
            quality,
            setting["language"] if setting is not None else DEFAULT_LANGUAGE,
            now,
            now,
        ))

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO imports (filename, sheet, imported_count, skipped_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (filename, sheet, len(rows), json.dumps(skipped), now),
        )
        import_id = int(cursor.lastrowid)
        conn.executemany(
            f"""INSERT INTO leads (email, business_name, category, country, location, website, instagram, email_type,
                                   priority, quality, language, created_at, updated_at, import_id, is_test)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {import_id}, {1 if is_test else 0})""",
            rows,
        )

    logger.info("Imported %d leads from %s (%s), skipped %s", len(rows), filename, sheet, skipped)
    return ImportResult(import_id=import_id, imported=len(rows), skipped=skipped, unknown_countries=unknown_countries)


def import_test_addresses(addresses: str, country: str, conn: sqlite3.Connection) -> ImportResult:
    """Add pasted addresses, one per line, as test leads. Only test leads can be emailed while the lock is on."""
    emails = [line.strip() for line in addresses.splitlines() if line.strip()]
    df = pd.DataFrame({
        "email": emails,
        "business_name": [TEST_LEAD_NAME] * len(emails),
        "category": [TEST_LEAD_CATEGORY] * len(emails),
        "country": [country] * len(emails),
    }, dtype=str)
    mapping = {column: column for column in df.columns}
    return import_rows(df, mapping, TEST_LEAD_FILENAME, None, [], TEST_LEAD_QUALITY, conn, is_test=True)
