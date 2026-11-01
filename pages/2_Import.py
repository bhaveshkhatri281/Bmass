"""Import page: upload Excel/CSV, column mapping, and import history."""

import json

import streamlit as st

from mailer.importer import (
    DEFAULT_COLUMN_MAP,
    QUALITY_SHEETS,
    ImportFileError,
    import_rows,
    import_test_addresses,
    list_sheets,
    read_table,
)
from mailer.ui import setup_page

NOT_MAPPED = "(not in this file)"

FIELD_LABELS = {
    "email": "Email (required)",
    "business_name": "Business name",
    "category": "Category",
    "country": "Country",
    "location": "Location",
    "website": "Website",
    "instagram": "Instagram",
    "email_type": "Email type",
    "priority": "Priority",
    "quality": "Quality",
}


@st.cache_data(show_spinner=False)
def load_sheets(data: bytes) -> list[str]:
    return list_sheets(data)


@st.cache_data(show_spinner=False)
def load_table(data: bytes, filename: str, sheet: str | None):
    return read_table(data, filename, sheet)


conn, settings = setup_page("Import Leads")

st.markdown("### 📥 Import Leads from Excel or CSV")
uploaded = st.file_uploader("Lead file", type=["xlsx", "csv"])

if uploaded is not None:
    data = uploaded.getvalue()
    filename = uploaded.name
    sheet = None
    df = None

    try:
        if filename.lower().endswith(".xlsx"):
            sheets = load_sheets(data)
            sheet = st.selectbox("Sheet", sheets, index=sheets.index("Good") if "Good" in sheets else 0)
        df = load_table(data, filename, sheet)
    except (ImportFileError, ValueError) as e:
        st.error(f"Could not read this file: {e}")

    if df is not None:
        st.write(f"**{len(df):,}** rows found.")

        st.markdown("#### Column mapping")
        options = [NOT_MAPPED, *df.columns]
        map_columns = st.columns(2)
        mapping = {}
        for i, (lead_field, label) in enumerate(FIELD_LABELS.items()):
            default = DEFAULT_COLUMN_MAP[lead_field]
            index = options.index(default) if default in options else 0
            choice = map_columns[i % 2].selectbox(label, options, index=index, key=f"map_{lead_field}_{sheet}")
            mapping[lead_field] = None if choice == NOT_MAPPED else choice

        default_quality = ""
        if mapping["quality"] is None:
            default_quality = st.text_input(
                "Quality for every row",
                value=sheet if sheet in QUALITY_SHEETS else "",
                key=f"default_quality_{sheet}",
                help="This file has no quality column. Good is sent before Weak.",
            )

        is_test = st.checkbox(
            "These are test leads (my own addresses)",
            help="While the test leads lock is on, LIVE mode can only email leads marked as test leads.",
        )

        if st.button("📥 Import", type="primary"):
            try:
                st.session_state["import_result"] = import_rows(
                    df,
                    mapping,
                    filename,
                    sheet,
                    settings.import_.skip_quality,
                    default_quality,
                    conn,
                    is_test,
                )
            except ImportFileError as e:
                st.error(str(e))

with st.expander("🧪 Add test leads (my own addresses)"):
    st.write("Paste one address per line. These are the only leads LIVE mode can email while the test leads lock is on.")
    country_names = [row["country"] for row in conn.execute("SELECT country FROM country_settings WHERE skip = 0 ORDER BY sort_order")]
    with st.form("test_leads_form", clear_on_submit=True):
        test_addresses = st.text_area("Addresses", height=120)
        test_country = st.selectbox("Country", country_names, index=country_names.index("United Kingdom"))
        if st.form_submit_button("➕ Add test leads"):
            st.session_state["import_result"] = import_test_addresses(test_addresses, test_country, conn)

result = st.session_state.get("import_result")
if result is not None:
    st.markdown("#### Summary")
    st.success(f"Imported **{result.imported:,}** leads.")
    if result.skipped:
        st.write("**Skipped:**")
        st.dataframe(
            [{"Reason": reason, "Rows": count} for reason, count in sorted(result.skipped.items())],
            width="stretch",
        )
    if result.unknown_countries:
        st.warning("Unknown countries: these leads use language en and timezone Europe/London. Fix them on the Campaign page.")
        st.dataframe(
            [{"Country": country, "Rows": count} for country, count in sorted(result.unknown_countries.items())],
            width="stretch",
        )

st.markdown("### 📜 Import History")
imports = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 20").fetchall()
if not imports:
    st.write("No imports recorded yet.")
else:
    st.dataframe(
        [
            {
                "Date": row["created_at"],
                "File": row["filename"],
                "Sheet": row["sheet"],
                "Imported": row["imported_count"],
                "Skipped": ", ".join(f"{reason}: {count}" for reason, count in json.loads(row["skipped_json"]).items()),
            }
            for row in imports
        ],
        width="stretch",
    )
