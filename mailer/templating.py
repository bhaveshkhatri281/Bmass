"""Templating: merge fields, spintax, footer, name cleanup, preview, and template editing rules."""

from __future__ import annotations

import hashlib
import random
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from mailer.config import Settings
from mailer.db import get_template, open_conn, transaction
from mailer.scheduling import US_COUNTRY_NAMES

ALLOWED_FIELDS = (
    "business_name",
    "category",
    "country",
    "sender_name",
    "sender_title",
    "website",
    "booking_link",
    "portfolio_link",
    "business_address",
    "custom_line",
    "us_address",
)

MERGE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# One spintax option may hold merge fields, so {Hi {{business_name}}|Hello} works.
_OPTION = r"(?:[^{}]|\{\{\s*\w+\s*\}\})*"
SPINTAX_RE = re.compile(r"(?<!\{)\{(?!\{)(" + _OPTION + r"\|" + _OPTION + r")\}(?!\})")

LINK_RE = re.compile(r"http|www\.|\.com/", re.IGNORECASE)
LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2,5}$")

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍⃣]")
_NAME_SYMBOL_RE = re.compile("[®™©\"“”„«»]")
_POSSESSIVE_RE = re.compile(r"(?<=[^\W\d_])['’]S\b")
_BRACKETED_RE = re.compile(r"\([^()\[\]]*\)|\[[^()\[\]]*\]")

LEGAL_SUFFIXES = (
    "SASU", "SAS", "SARL", "EURL", "SA", "SCI", "SNC",
    "Ltd", "Limited", "LLC", "Inc", "GmbH", "BV", "AB", "Srl", "SL",
)
_LEGAL_FORM = "(?:" + "|".join(r"\.?".join(suffix) + r"\.?" for suffix in LEGAL_SUFFIXES) + ")"
# Bare at the very end, or wrapped in its own brackets. A form inside a longer
# bracket, like (Schnitzler Concept GmbH), stays: removing it would split the bracket.
_LEGAL_SUFFIX_RE = re.compile(
    r"(?:\s*[,;/]\s*|\s+[-–—]\s+|\s+)" + _LEGAL_FORM + r"$"
    r"|\s*[(\[]\s*" + _LEGAL_FORM + r"\s*[)\]]$",
    re.IGNORECASE,
)

# Only French forms are stripped from the front. AB and SA are ordinary brand words there.
LEGAL_PREFIXES = ("SASU", "SAS", "SARL", "EURL")
_FRENCH_FORM = "(?:" + "|".join(r"\.?".join(prefix) + r"\.?" for prefix in LEGAL_PREFIXES) + ")"
_LEGAL_PREFIX_RE = re.compile(
    r"^(?:" + _FRENCH_FORM + r"|[(\[]\s*" + _FRENCH_FORM + r"\s*[)\]])(?:\s*[,:]\s*|\s+[-–—]\s+|\s+)(?=\S)",
    re.IGNORECASE,
)


class TemplateError(ValueError):
    """Raised when a template cannot be rendered."""


@dataclass
class RenderedEmail:
    subject: str
    body: str
    language: str
    warnings: List[str] = field(default_factory=list)


@dataclass
class PreviewResult:
    lead: sqlite3.Row
    email: RenderedEmail


# ==============================================================================
# Text helpers
# ==============================================================================

def _strip_legal_suffix(name: str) -> str:
    """Remove trailing legal forms like SAS or Ltd. A name that is only a legal form stays as is."""
    text = name
    while True:
        stripped = _LEGAL_SUFFIX_RE.sub("", text).rstrip(" ,;-–—/")
        if stripped == text:
            return text
        if not stripped:
            return name
        text = stripped


def _strip_bracketed(name: str) -> str:
    """Remove (...) and [...] text, inner pairs first. A name that is only brackets stays as is."""
    text = name
    while True:
        stripped = " ".join(_BRACKETED_RE.sub(" ", text).split())
        if stripped == text:
            return text
        if not stripped:
            return name
        text = stripped


def _name_key(name: str) -> str:
    """Normalize a name for exception matching, so case and a final dot do not matter."""
    return " ".join(name.split()).rstrip(". ").casefold()


def clean_business_name(name: Optional[str], keep_suffix: Sequence[str] = ()) -> str:
    """Strip symbols, emojis, quotes, brackets and legal forms, collapse spaces, and Title Case long all-caps names.

    Names listed in keep_suffix keep their bracketed text and legal form.
    """
    text = _NAME_SYMBOL_RE.sub("", _EMOJI_RE.sub("", name or ""))
    text = " ".join(text.split()).strip("'‘’` ").strip()
    if _name_key(text) not in {_name_key(exception) for exception in keep_suffix}:
        text = _strip_legal_suffix(_LEGAL_PREFIX_RE.sub("", _strip_bracketed(text)))
    if len(text) > 4 and text.isupper():
        text = _POSSESSIVE_RE.sub(lambda m: m.group().lower(), text.title())
    return text


def category_phrase(raw: Optional[str], language: str, conn: sqlite3.Connection) -> Tuple[str, bool]:
    """Return the phrase for a raw category and whether it came from the phrase table."""
    key = (raw or "").strip().lower()
    first_part = key.split(";")[0].strip()
    column = "phrase_fr" if language == "fr" else "phrase_en"
    for candidate in (key, first_part):
        row = conn.execute(f"SELECT {column} AS phrase FROM category_phrases WHERE raw = ?", (candidate,)).fetchone()
        if row:
            return row["phrase"], True
    return first_part, False


def validate_template(text: str) -> List[str]:
    """Return validation errors for template text. An empty list means it can be saved."""
    errors = [f"Unknown merge field: {{{{{name}}}}}" for name in MERGE_RE.findall(text) if name not in ALLOWED_FIELDS]
    leftover = MERGE_RE.sub("", SPINTAX_RE.sub("", text))
    if "{" in leftover or "}" in leftover:
        errors.append("Stray brace: use {{field}} for merge fields and {a|b} for spintax.")
    return list(dict.fromkeys(errors))


def has_link(body: str) -> bool:
    """Return True if a step 1 body contains a link, which deserves a warning on save."""
    return LINK_RE.search(body) is not None


def resolve_spintax(text: str, rng: random.Random) -> str:
    """Replace each {a|b|c} with one option picked by rng."""
    return SPINTAX_RE.sub(lambda m: rng.choice(m.group(1).split("|")), text)


def merge_fields(text: str, values: Mapping[str, str]) -> str:
    """Replace {{field}} markers in one pass, so merged values are never re-read as template text."""
    return MERGE_RE.sub(lambda m: values[m.group(1)], text)


def _fill(text: str, values: Mapping[str, str], rng: random.Random) -> str:
    # Spintax runs before merging: a business name containing | or { must stay literal.
    merged = merge_fields(resolve_spintax(text, rng), values)
    return re.sub(r"[ \t]{2,}", " ", merged)


def _rng(email: str, step_no: int) -> random.Random:
    digest = hashlib.sha256(f"{email}{step_no}".encode("utf-8")).hexdigest()
    return random.Random(int(digest, 16))


# ==============================================================================
# Rendering
# ==============================================================================

def _value(row: Any, key: str) -> str:
    return str(row[key] or "") if row is not None and key in row.keys() else ""


def _merge_values(
    lead: Any,
    inbox: Any,
    settings: Settings,
    language: str,
    conn: sqlite3.Connection,
    warnings: List[str],
) -> Dict[str, str]:
    raw_category = _value(lead, "category")
    phrase, matched = category_phrase(raw_category, language, conn)
    if not raw_category.strip():
        warnings.append("Lead has no category, so {{category}} is empty.")
    elif not matched:
        warnings.append(f"No phrase for category '{raw_category}', using '{phrase}' as is.")

    custom_line = _value(lead, "custom_line").strip()
    country = _value(lead, "country")
    website = re.sub(r"^https?://", "", settings.sender.website.strip(), flags=re.IGNORECASE).rstrip("/")

    return {
        "business_name": clean_business_name(_value(lead, "business_name"), settings.names.keep_suffix),
        "category": phrase,
        "country": country,
        # US leads see the postal address in the footer. Everyone else sees the website.
        "us_address": settings.sender.business_address if country.strip().lower() in US_COUNTRY_NAMES else website,
        "sender_name": _value(inbox, "display_name") or settings.sender.default_display_name,
        "sender_title": _value(inbox, "sender_title") or settings.sender.sender_title,
        "website": settings.sender.website,
        "booking_link": settings.sender.booking_link,
        "portfolio_link": settings.sender.portfolio_link,
        "business_address": settings.sender.business_address,
        "custom_line": f"{custom_line}\n\n" if custom_line else "",
    }


def _resolve_template(
    step_no: int,
    language: str,
    conn: sqlite3.Connection,
    warnings: List[str],
) -> Tuple[sqlite3.Row, str]:
    template = get_template(step_no, language, conn)
    if template is not None and template["body"].strip():
        return template, language

    if language != "en":
        warnings.append(f"No {language} template for step {step_no}, using en.")
        template = get_template(step_no, "en", conn)
        if template is not None and template["body"].strip():
            return template, "en"

    raise TemplateError(f"No template for step {step_no}.")


def _first_subject(lead: Any, values: Mapping[str, str], conn: sqlite3.Connection) -> str:
    """Return the stored step 1 subject, or work it out for a lead that has not been emailed."""
    stored = _value(lead, "first_subject")
    if stored:
        return stored
    template, _ = _resolve_template(1, lead["language"] or "en", conn, [])
    return _fill(template["subject"], values, _rng(lead["email"], 1))


def render(
    lead: Any,
    step_no: int,
    inbox: Any,
    settings: Settings,
    conn: Optional[sqlite3.Connection] = None,
) -> RenderedEmail:
    """Render one email. Raises TemplateError if it cannot be rendered safely."""
    if conn is None:
        with open_conn() as c:
            return render(lead, step_no, inbox, settings, c)

    warnings: List[str] = []
    template, language = _resolve_template(step_no, lead["language"] or "en", conn, warnings)

    language_row = conn.execute("SELECT footer_text FROM languages WHERE code = ?", (language,)).fetchone()
    if language_row is None:
        raise TemplateError(f"Language {language} has no footer.")
    footer = language_row["footer_text"]

    subject_template = template["subject"] if step_no == 1 else ""
    errors = validate_template(subject_template + template["body"] + footer)
    if errors:
        raise TemplateError("; ".join(errors))

    values = _merge_values(lead, inbox, settings, language, conn, warnings)
    rng = _rng(lead["email"], step_no)

    if step_no == 1:
        subject = _fill(subject_template, values, rng)
    else:
        subject = f"Re: {_first_subject(lead, values, conn)}"

    body = _fill(template["body"], values, rng)
    # An empty {{us_address}} must not leave a dangling separator at the end of the line.
    full_footer = re.sub(r"[ ·]+$", "", _fill(footer, values, rng), flags=re.MULTILINE)
    full_body = f"{body}\n\n{full_footer}"

    used_fields = MERGE_RE.findall(subject_template + template["body"] + footer)
    for name in dict.fromkeys(used_fields):
        if not values[name] and name not in ("custom_line", "category"):
            warnings.append(f"{{{{{name}}}}} is empty.")

    return RenderedEmail(subject=subject, body=full_body, language=language, warnings=warnings)


def preview_random_lead(
    step_no: int,
    language: str,
    settings: Settings,
    inbox: Any = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[PreviewResult]:
    """Render a step for a random real lead in this language, or None if there are no such leads."""
    if conn is None:
        with open_conn() as c:
            return preview_random_lead(step_no, language, settings, inbox, c)

    lead = conn.execute("SELECT * FROM leads WHERE language = ? ORDER BY RANDOM() LIMIT 1", (language,)).fetchone()
    if lead is None:
        return None
    return PreviewResult(lead=lead, email=render(lead, step_no, inbox, settings, conn))


# ==============================================================================
# Template page edits
# ==============================================================================

def save_template(step_no: int, language: str, subject: str, body: str, conn: sqlite3.Connection) -> List[str]:
    """Validate and save one template, returning errors. Nothing is saved if there are any."""
    errors = validate_template(body)
    if step_no == 1:
        errors += validate_template(subject)
        if not subject.strip():
            errors.append("Step 1 needs a subject.")
    if not body.strip():
        errors.append("Body cannot be empty.")
    if errors:
        return errors

    with transaction(conn):
        conn.execute(
            """INSERT INTO templates (step_no, language, subject, body) VALUES (?, ?, ?, ?)
               ON CONFLICT(step_no, language) DO UPDATE SET subject = excluded.subject, body = excluded.body""",
            (step_no, language, subject, body),
        )
    return []


def save_steps(rows: List[Dict[str, Any]], conn: sqlite3.Connection) -> List[str]:
    """Validate and save step delays and active flags, returning errors."""
    errors: List[str] = []
    for row in rows:
        step_no, delay = int(row["step_no"]), int(row["delay_days"])
        if delay < 0:
            errors.append(f"Step {step_no}: delay cannot be negative.")
        if step_no == 1 and delay != 0:
            errors.append("Step 1 delay must be 0.")
        if step_no == 1 and not row["active"]:
            errors.append("Step 1 must stay active.")
        template = get_template(step_no, "en", conn)
        if row["active"] and (template is None or not template["body"].strip()):
            errors.append(f"Step {step_no}: write the en template before activating it.")
    if errors:
        return errors

    with transaction(conn):
        conn.executemany(
            "UPDATE steps SET delay_days = ?, active = ? WHERE step_no = ?",
            [(int(r["delay_days"]), 1 if r["active"] else 0, int(r["step_no"])) for r in rows],
        )
    return []


def add_step(conn: sqlite3.Connection) -> int:
    """Add a new inactive step. It stays inactive until its template exists."""
    step_no = conn.execute("SELECT COALESCE(MAX(step_no), 0) + 1 FROM steps").fetchone()[0]
    with transaction(conn):
        conn.execute("INSERT INTO steps (step_no, delay_days, active) VALUES (?, 3, 0)", (step_no,))
    return int(step_no)


def remove_last_step(conn: sqlite3.Connection) -> Optional[str]:
    """Remove the highest step and its templates. Returns an error message if that is not allowed."""
    step_no = conn.execute("SELECT MAX(step_no) FROM steps").fetchone()[0]
    if step_no is None or step_no <= 1:
        return "Step 1 cannot be removed."
    with transaction(conn):
        conn.execute("DELETE FROM templates WHERE step_no = ?", (step_no,))
        conn.execute("DELETE FROM steps WHERE step_no = ?", (step_no,))
    return None


def add_language(code: str, name: str, footer: str, conn: sqlite3.Connection) -> List[str]:
    """Validate and add a language, returning errors."""
    clean_code = code.strip().lower()
    errors = validate_template(footer)
    if not LANGUAGE_CODE_RE.match(clean_code):
        errors.append("Code must be 2 to 5 lowercase letters, like es.")
    if not name.strip():
        errors.append("Name cannot be empty.")
    if not footer.strip():
        errors.append("Footer cannot be empty.")
    if conn.execute("SELECT 1 FROM languages WHERE code = ?", (clean_code,)).fetchone():
        errors.append(f"Language {clean_code} already exists.")
    if errors:
        return errors

    with transaction(conn):
        conn.execute("INSERT INTO languages (code, name, footer_text) VALUES (?, ?, ?)", (clean_code, name.strip(), footer))
    return []


def save_footer(code: str, footer: str, conn: sqlite3.Connection) -> List[str]:
    """Validate and save a language footer, returning errors."""
    errors = validate_template(footer)
    if not footer.strip():
        errors.append("Footer cannot be empty.")
    if errors:
        return errors

    with transaction(conn):
        conn.execute("UPDATE languages SET footer_text = ? WHERE code = ?", (footer, code))
    return []


def save_category_phrases(rows: List[Dict[str, Any]], conn: sqlite3.Connection) -> List[str]:
    """Replace the category phrase table, returning errors."""
    errors: List[str] = []
    cleaned: Dict[str, Tuple[str, str]] = {}
    for row in rows:
        raw = str(row.get("raw") or "").strip().lower()
        phrase_en = str(row.get("phrase_en") or "").strip()
        phrase_fr = str(row.get("phrase_fr") or "").strip()
        if not raw and not phrase_en and not phrase_fr:
            continue
        if not raw or not phrase_en or not phrase_fr:
            errors.append(f"Row '{raw or phrase_en or phrase_fr}' needs raw, phrase_en and phrase_fr.")
        elif raw in cleaned:
            errors.append(f"Duplicate category: {raw}")
        cleaned[raw] = (phrase_en, phrase_fr)
    if errors:
        return errors

    with transaction(conn):
        conn.execute("DELETE FROM category_phrases")
        conn.executemany(
            "INSERT INTO category_phrases (raw, phrase_en, phrase_fr) VALUES (?, ?, ?)",
            [(raw, en, fr) for raw, (en, fr) in cleaned.items()],
        )
    return []
