"""Export leads and the send log to Excel files in exports\\."""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from mailer.config import PROJECT_ROOT
from mailer.db import list_send_log

logger = logging.getLogger("exporter")

EXPORT_DIR = PROJECT_ROOT / "exports"

LEAD_COLUMNS = [
    "email", "business_name", "category", "country", "location", "website", "instagram", "email_type",
    "priority", "quality", "language", "status", "step", "last_sent_at", "next_due_at", "is_test",
]
FORMULA_STARTS = ("=", "+", "-", "@")


def _safe_cell(value: Any) -> Any:
    """Stop lead text that starts like a formula from running when the file is opened in Excel."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS):
        return f"'{value}"
    return value


def _write(rows: Sequence[sqlite3.Row], columns: List[str], name: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"{name}_{stamp}.xlsx"
    data = [[_safe_cell(row[column]) for column in columns] for row in rows]
    pd.DataFrame(data, columns=columns).to_excel(path, index=False, engine="openpyxl")
    logger.info("Exported %d rows to %s", len(rows), path.name)
    return path


def export_send_log(conn: sqlite3.Connection, filters: Optional[Dict[str, Any]] = None) -> Path:
    """Write the send log, with any filters applied, to Excel."""
    rows, _ = list_send_log(filters or {}, None, 0, conn)
    columns = ["ts", "inbox", "lead_email", "step_no", "subject", "result", "error_text", "gmail_message_id", "thread_id"]
    return _write(rows, columns, "send_log")


def export_leads(conn: sqlite3.Connection) -> Path:
    """Write every lead with its status to Excel."""
    rows = conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
    return _write(rows, LEAD_COLUMNS, "leads")
