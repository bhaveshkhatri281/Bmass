"""Tests for merge fields, spintax, footers, name cleanup, and template edits."""

import random

import pytest

from mailer.config import Settings
from mailer.db import open_conn
from mailer.templating import (
    TemplateError,
    add_language,
    add_step,
    category_phrase,
    clean_business_name,
    has_link,
    preview_random_lead,
    remove_last_step,
    render,
    resolve_spintax,
    save_category_phrases,
    save_steps,
    save_template,
    validate_template,
)

INBOX = {"display_name": "Beedu from Mobisa", "sender_title": "Founder"}


def make_settings():
    settings = Settings()
    settings.sender.business_address = "1 Test Road, Mumbai"
    settings.sender.portfolio_link = "https://mobisa.in/work"
    settings.sender.website = "mobisa.in"
    return settings


def make_lead(**overrides):
    lead = {
        "email": "hello@acme.com",
        "business_name": "Acme",
        "category": "skincare",
        "country": "United Kingdom",
        "language": "en",
        "custom_line": "",
        "first_subject": None,
    }
    lead.update(overrides)
    return lead


def rendered(lead=None, step=1, settings=None):
    return render(lead or make_lead(), step, INBOX, settings or make_settings())


def set_template(step, language, subject, body):
    with open_conn() as conn:
        assert save_template(step, language, subject, body, conn) == []


def test_merge_fields_in_body_and_footer():
    email = rendered()
    assert email.body.startswith("Hi Acme team,\n\nWho handles your Meta and TikTok ads?")
    assert "for skincare brands" in email.body
    assert "Want me to send a 15-second sample made for Acme?" in email.body
    assert email.body.endswith('Beedu from Mobisa · mobisa.in\nReply "stop" to opt out.')
    assert email.subject in ("quick question for Acme", "idea for Acme")
    assert email.warnings == []


@pytest.mark.parametrize("country", ["United States", " united states ", "USA", "US"])
def test_us_leads_get_the_address_in_the_footer(country):
    email = rendered(make_lead(country=country))
    assert email.body.endswith('Beedu from Mobisa · 1 Test Road, Mumbai\nReply "stop" to opt out.')
    assert "mobisa.in" not in email.body


@pytest.mark.parametrize("country", ["United Kingdom", "Canada", "Germany", "", "Atlantis"])
def test_other_leads_get_the_website_and_no_address(country):
    email = rendered(make_lead(country=country))
    assert "1 Test Road" not in email.body
    assert email.body.endswith('Beedu from Mobisa · mobisa.in\nReply "stop" to opt out.')


def test_french_footer_uses_the_website():
    email = rendered(make_lead(language="fr", country="France"))
    assert email.body.endswith("Beedu from Mobisa · mobisa.in\nRépondez « stop » pour ne plus recevoir nos e-mails.")


@pytest.mark.parametrize("website", ["https://mobisa.in", "http://mobisa.in/", "HTTPS://mobisa.in/", " mobisa.in "])
def test_footer_website_has_no_scheme(website):
    settings = make_settings()
    settings.sender.website = website
    assert rendered(settings=settings).body.endswith('Beedu from Mobisa · mobisa.in\nReply "stop" to opt out.')


@pytest.mark.parametrize(
    "country, blank_setting",
    [("United States", "business_address"), ("France", "website")],
)
def test_empty_footer_value_warns_and_leaves_no_separator(country, blank_setting):
    settings = make_settings()
    setattr(settings.sender, blank_setting, "")

    email = rendered(make_lead(country=country), settings=settings)

    assert "{{us_address}} is empty." in email.warnings
    assert email.body.endswith('Beedu from Mobisa\nReply "stop" to opt out.')


def test_all_allowed_fields_render():
    set_template(
        2, "en", "",
        "{{business_name}}|{{category}}|{{country}}|{{sender_name}}|{{sender_title}}|{{website}}|{{booking_link}}|{{portfolio_link}}|{{business_address}}",
    )
    settings = make_settings()
    settings.sender.website = "mobisa.in"
    settings.sender.booking_link = "book.mobisa.in"
    email = rendered(step=2, settings=settings)
    first_line = email.body.split("\n")[0]
    assert first_line == (
        "Acme|skincare|United Kingdom|Beedu from Mobisa|Founder|mobisa.in|book.mobisa.in"
        "|https://mobisa.in/work|1 Test Road, Mumbai"
    )


def test_unknown_field_is_a_validation_error():
    assert validate_template("Hi {{first_name}}") == ["Unknown merge field: {{first_name}}"]
    assert validate_template("Hi {{business_name}}") == []


def test_unknown_field_blocks_saving_and_rendering():
    with open_conn() as conn:
        assert save_template(1, "en", "hi", "Hi {{nope}}", conn) == ["Unknown merge field: {{nope}}"]
        conn.execute("UPDATE templates SET body = 'Hi {{nope}}' WHERE step_no = 2 AND language = 'en'")
    with pytest.raises(TemplateError):
        rendered(step=2)


@pytest.mark.parametrize("text", ["Hi {business_name}", "Hi {{business_name}", "open { brace", "{a|{b|c}}"])
def test_stray_braces_are_errors(text):
    assert any("Stray brace" in error for error in validate_template(text))


@pytest.mark.parametrize("text", ["{a|b} {{business_name}}", "{Hi {{business_name}}|Hello} there", "plain"])
def test_valid_templates_pass(text):
    assert validate_template(text) == []


def test_empty_custom_line_leaves_no_blank_line():
    email = rendered()
    assert "team,\n\nWho handles" in email.body
    assert "\n\n\n" not in email.body


def test_custom_line_is_followed_by_blank_line():
    email = rendered(make_lead(custom_line="Loved your new range."))
    assert "team,\n\nLoved your new range.\n\nWho handles" in email.body


def test_spintax_is_deterministic_per_lead_and_step():
    assert rendered().subject == rendered().subject

    subjects = {rendered(make_lead(email=f"lead{i}@acme.com")).subject for i in range(30)}
    assert subjects == {"quick question for Acme", "idea for Acme"}


def test_spintax_differs_by_step():
    set_template(2, "en", "", "{a|b|c|d|e|f|g|h}")
    set_template(3, "en", "", "{a|b|c|d|e|f|g|h}")
    emails = [f"x{i}@acme.com" for i in range(20)]
    picks = [(rendered(make_lead(email=e), step=2).body[0], rendered(make_lead(email=e), step=3).body[0]) for e in emails]
    assert any(step2 != step3 for step2, step3 in picks)


def test_spintax_option_can_hold_merge_field():
    set_template(2, "en", "", "{Hi {{business_name}}|Hello {{business_name}}}!")
    assert rendered(step=2).body.split("\n")[0] in ("Hi Acme!", "Hello Acme!")


def test_spintax_ignores_merge_fields():
    assert resolve_spintax("{{business_name}}", random.Random(1)) == "{{business_name}}"


def test_business_name_with_template_characters_stays_literal():
    email = rendered(make_lead(business_name="Sun {Skin|Care}"))
    assert "Hi Sun {Skin|Care} team," in email.body


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Yes She Will!" Organics®', "Yes She Will! Organics"),
        ("LUXE SKIN", "Luxe Skin"),
        ("NYX", "NYX"),
        ("LUXE", "LUXE"),
        ("Acme™", "Acme"),
        ("Acme© Beauty", "Acme Beauty"),
        ("🌿 Green  Leaf 🌿", "Green Leaf"),
        ("“Quoted Name”", "Quoted Name"),
        ("'Single Quoted'", "Single Quoted"),
        ("  Too   Many    Spaces ", "Too Many Spaces"),
        ("L'Oréal", "L'Oréal"),
        ("L'OCCITANE", "L'Occitane"),
        ("JOHN'S SOAP", "John's Soap"),
        ("CAFÉ DE PARIS", "Café De Paris"),
        ("Mixed CASE Name", "Mixed CASE Name"),
        ("", ""),
        (None, ""),
    ],
)
def test_business_name_cleanup(raw, expected):
    assert clean_business_name(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Acme SAS", "Acme"),
        ("Acme SASU", "Acme"),
        ("Acme SARL", "Acme"),
        ("Acme EURL", "Acme"),
        ("Acme SA", "Acme"),
        ("Acme SCI", "Acme"),
        ("Acme SNC", "Acme"),
        ("Acme Ltd", "Acme"),
        ("Acme Limited", "Acme"),
        ("Acme LLC", "Acme"),
        ("Acme Inc", "Acme"),
        ("Acme GmbH", "Acme"),
        ("Acme BV", "Acme"),
        ("Acme AB", "Acme"),
        ("Acme Srl", "Acme"),
        ("Acme SL", "Acme"),
        ("Acme (SAS)", "Acme"),
        ("Acme [Ltd]", "Acme"),
        ("Acme ( SARL )", "Acme"),
        ("Acme Inc.", "Acme"),
        ("Acme, Inc.", "Acme"),
        ("Acme,Inc", "Acme"),
        ("Acme Ltd.", "Acme"),
        ("Acme S.A.S.", "Acme"),
        ("Acme S.A.S", "Acme"),
        ("Acme S.A.", "Acme"),
        ("Acme S.A.R.L.", "Acme"),
        ("Acme L.L.C.", "Acme"),
        ("Acme B.V.", "Acme"),
        ("Acme S.L.", "Acme"),
        ("Acme sas", "Acme"),
        ("Acme sarl", "Acme"),
        ("Acme - SAS", "Acme"),
        ("Acme – Ltd", "Acme"),
        ("Acme Holdings Ltd Inc", "Acme Holdings"),
        ("The Soap Co. LTD", "The Soap Co."),
        ("CHAMPAGNE CHARLES MIGNON (SAS)", "Champagne Charles Mignon"),
        ("MOULIN A HUILE (SARL)", "Moulin A Huile"),
        ("LUXE SKIN SAS", "Luxe Skin"),
        ("ACME SAS", "ACME"),
        ("Acme® Ltd", "Acme"),
    ],
)
def test_legal_suffixes_are_stripped(raw, expected):
    assert clean_business_name(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Casa",
        "Sauna",
        "Maison Lab",
        "Sasha",
        "Rosa",
        "Vinca",
        "Vertriebs-GmbH",
        "Limited Edition Soap",
        "AB Living Essentials",
        "SA Wines",
        "Inc Magazine",
        "Sarlat Soap",
        "Sasha Skin",
        "SKINthings (Schnitzler Concept GmbH)",
        "Unsa",
    ],
)
def test_names_without_a_strippable_legal_form_are_kept(name):
    assert clean_business_name(name) == name


@pytest.mark.parametrize("name", ["SAS", "Ltd", "(SA)", "Limited", "SARL", "S.A.S."])
def test_name_that_is_only_a_legal_form_is_kept(name):
    assert clean_business_name(name) == name


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("SARL Martin", "Martin"),
        ("SAS Dupont", "Dupont"),
        ("SASU 4ULTRA", "4Ultra"),
        ("EURL Groizeau", "Groizeau"),
        ("Sarl abat jour du moulin", "abat jour du moulin"),
        ("S.A.S. MEN", "MEN"),
        ("S.A.R.L. Martin", "Martin"),
        ("(SARL) Martin", "Martin"),
        ("SARL - Martin", "Martin"),
        ("SARL, Martin", "Martin"),
        ("SAS: Martin", "Martin"),
        ("Sarl GIOVANNI FERRONNERIE D'ART", "Giovanni Ferronnerie D'Art"),
        ("SARL Martin SAS", "Martin"),
    ],
)
def test_french_legal_prefixes_are_stripped(raw, expected):
    assert clean_business_name(raw) == expected


@pytest.mark.parametrize(
    "name, exceptions, expected",
    [
        ("Nails Inc.", ["Nails Inc."], "Nails Inc."),
        ("Nails Inc", ["Nails Inc."], "Nails Inc"),
        ("nails inc.", ["Nails Inc."], "nails inc."),
        ("Nails Inc.®", ["Nails Inc."], "Nails Inc."),
        ("NAILS INC.", ["Nails Inc."], "Nails Inc."),
        ("SARL Martin", ["SARL Martin"], "SARL Martin"),
        ("Other Inc.", ["Nails Inc."], "Other"),
        ("Nails Inc.", [], "Nails"),
    ],
)
def test_name_exceptions_keep_their_legal_form(name, exceptions, expected):
    assert clean_business_name(name, exceptions) == expected


def test_render_uses_name_exceptions_from_settings():
    assert "Hi Nails Inc. team," in rendered(make_lead(business_name="Nails Inc.")).body

    settings = make_settings()
    settings.names.keep_suffix = []
    assert "Hi Nails team," in rendered(make_lead(business_name="Nails Inc."), settings=settings).body


def test_french_template_footer_and_category():
    lead = make_lead(language="fr", category="food & beverage", country="France")
    email = rendered(lead)
    assert email.language == "fr"
    assert email.body.startswith("Bonjour l'équipe Acme,")
    assert "pour les marques alimentaire :" in email.body
    assert email.body.endswith("Répondez « stop » pour ne plus recevoir nos e-mails.")
    assert email.subject in ("petite question pour Acme", "une idée pour Acme")


def test_follow_up_subject_uses_first_subject():
    email = rendered(make_lead(first_subject="idea for Acme"), step=2)
    assert email.subject == "Re: idea for Acme"
    assert email.body.startswith("Quick bump on this.")


def test_follow_up_preview_before_step_1_matches_real_subject():
    lead = make_lead()
    assert rendered(lead, step=3).subject == f"Re: {rendered(lead).subject}"


def test_language_fallback_to_en_with_warning():
    with open_conn() as conn:
        assert add_language("es", "Spanish", "{{sender_name}}\nStop", conn) == []
    email = rendered(make_lead(language="es"))
    assert email.language == "en"
    assert email.body.startswith("Hi Acme team,")
    assert email.body.endswith('Reply "stop" to opt out.')
    assert "No es template for step 1, using en." in email.warnings


def test_missing_template_everywhere_raises():
    with open_conn() as conn:
        step = add_step(conn)
    with pytest.raises(TemplateError):
        rendered(step=step)


def test_empty_settings_field_is_warned():
    settings = make_settings()
    settings.sender.portfolio_link = ""
    settings.sender.business_address = ""
    email = rendered(make_lead(country="United States"), step=3, settings=settings)
    assert "{{portfolio_link}} is empty." in email.warnings
    assert "{{us_address}} is empty." in email.warnings


@pytest.mark.parametrize(
    "raw, language, expected, matched",
    [
        ("Food & Beverage", "en", "food & drink", True),
        ("food & beverage", "fr", "alimentaire", True),
        ("Skincare; Haircare", "en", "skincare", True),
        ("hair care; skincare", "fr", "soin capillaire", True),
        ("Zebra Care; Makeup", "fr", "zebra care", False),
        ("", "en", "", False),
        ("Multi-category beauty", "fr", "beauté", True),
        ("Multi-category beauty", "en", "beauty", True),
        ("Sun & Skin Protection; Makeup", "fr", "soin solaire", True),
        ("Skincare & Body Care", "en", "skincare", True),
        ("Men’s Grooming", "en", "men's grooming", True),
        ("Press-on nails & lash products", "fr", "ongles", True),
    ],
)
def test_category_phrase_rules(raw, language, expected, matched):
    with open_conn() as conn:
        assert category_phrase(raw, language, conn) == (expected, matched)


def test_blank_category_leaves_no_double_space_and_warns():
    email = rendered(make_lead(category=""))
    assert "video ads for brands:" in email.body
    assert "Lead has no category, so {{category}} is empty." in email.warnings


def test_unmatched_category_warns():
    email = rendered(make_lead(category="Zebra Care"))
    assert "for zebra care brands" in email.body
    assert any("No phrase for category" in warning for warning in email.warnings)


@pytest.mark.parametrize("body", ["See http://x.io", "go to www.acme", "acme.com/page", "HTTP"])
def test_link_warning_triggers(body):
    assert has_link(body)


def test_plain_body_has_no_link_warning():
    assert not has_link("Want me to send a sample?")


def test_save_requires_step_1_subject_and_a_body():
    with open_conn() as conn:
        assert save_template(1, "en", "", "Body", conn) == ["Step 1 needs a subject."]
        assert save_template(2, "en", "", "  ", conn) == ["Body cannot be empty."]


def test_follow_up_subject_field_is_not_validated():
    with open_conn() as conn:
        assert save_template(2, "en", "{{ignored}}", "Fine", conn) == []


def test_preview_random_lead():
    now = "2026-11-01T00:00:00Z"
    with open_conn() as conn:
        assert preview_random_lead(1, "en", make_settings(), INBOX, conn) is None
        conn.execute(
            "INSERT INTO leads (email, business_name, category, language, created_at, updated_at) VALUES (?, 'Acme', 'skincare', 'en', ?, ?)",
            ("a@acme.com", now, now),
        )
        picked = preview_random_lead(1, "en", make_settings(), INBOX, conn)
    assert picked.lead["email"] == "a@acme.com"
    assert picked.email.body.startswith("Hi Acme team,")


def test_steps_editor_rules():
    with open_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_no")]

        rows[1]["delay_days"] = 0
        assert save_steps(rows, conn) == []
        assert conn.execute("SELECT delay_days FROM steps WHERE step_no = 2").fetchone()[0] == 0

        rows[1]["delay_days"] = -1
        assert save_steps(rows, conn) == ["Step 2: delay cannot be negative."]

        rows[1]["delay_days"] = 3
        rows[0]["delay_days"] = 2
        assert save_steps(rows, conn) == ["Step 1 delay must be 0."]


def test_new_step_stays_inactive_until_it_has_a_template():
    with open_conn() as conn:
        step = add_step(conn)
        assert step == 4
        rows = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_no")]
        assert rows[3]["active"] == 0

        rows[3]["active"] = 1
        assert save_steps(rows, conn) == ["Step 4: write the en template before activating it."]

        assert save_template(4, "en", "", "One more note.", conn) == []
        assert save_steps(rows, conn) == []


def test_remove_last_step_removes_templates_but_never_step_1():
    with open_conn() as conn:
        assert remove_last_step(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM templates WHERE step_no = 3").fetchone()[0] == 0
        assert remove_last_step(conn) is None
        assert remove_last_step(conn) == "Step 1 cannot be removed."
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1


def test_add_language_rules():
    with open_conn() as conn:
        assert add_language("en", "English", "x", conn) == ["Language en already exists."]
        assert "Code must be 2 to 5 lowercase letters, like es." in add_language("e1", "Bad", "x", conn)
        assert add_language("es", "Spanish", "{{nope}}", conn) == ["Unknown merge field: {{nope}}"]
        assert add_language("ES ", "Spanish", "{{sender_name}}", conn) == []
        assert conn.execute("SELECT name FROM languages WHERE code = 'es'").fetchone()[0] == "Spanish"


def test_category_phrase_table_save():
    rows = [
        {"raw": " Skincare ", "phrase_en": "skincare", "phrase_fr": "soin"},
        {"raw": None, "phrase_en": None, "phrase_fr": None},
    ]
    with open_conn() as conn:
        assert save_category_phrases(rows, conn) == []
        assert [tuple(r) for r in conn.execute("SELECT * FROM category_phrases")] == [("skincare", "skincare", "soin")]

        dupes = [rows[0], {"raw": "skincare", "phrase_en": "a", "phrase_fr": "b"}]
        assert save_category_phrases(dupes, conn) == ["Duplicate category: skincare"]

        partial = [{"raw": "makeup", "phrase_en": "", "phrase_fr": "maquillage"}]
        assert save_category_phrases(partial, conn) == ["Row 'makeup' needs raw, phrase_en and phrase_fr."]
        assert conn.execute("SELECT COUNT(*) FROM category_phrases").fetchone()[0] == 1
