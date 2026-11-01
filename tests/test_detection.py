"""Tests for classifying inbox messages, using FakeGmailClient."""

import datetime

import pytest

from mailer import detection
from mailer.config import Settings
from mailer.db import get_app_state, is_suppressed, open_conn
from mailer.detection import is_opt_out, run_all_inboxes, strip_quoted_text
from mailer.gmail_client import AuthExpired, GmailClientError, MessageMeta, RateLimited
from tests.fakes import FakeGmailClient

INBOX = "sales@mobisa.in"
LEAD = "lead@acme.com"
THREAD = "thread_1"
NOW = datetime.datetime(2026, 11, 4, 15, 0, tzinfo=datetime.timezone.utc)
STAMP = "2026-11-01T00:00:00Z"


@pytest.fixture
def settings():
    cfg = Settings()
    cfg.sender.unsubscribe_inbox = INBOX
    return cfg


@pytest.fixture
def client():
    fake = FakeGmailClient()
    fake.connect_inbox(INBOX)
    return fake


@pytest.fixture
def lead_id():
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO inboxes (id, email, display_name, ramp_start_date, created_at) VALUES (1, ?, 'Beedu', '2026-11-01', ?)",
            (INBOX, STAMP),
        )
        cursor = conn.execute(
            """INSERT INTO leads (email, status, step, inbox_id, thread_id, first_subject, next_due_at, created_at, updated_at)
               VALUES (?, 'in_sequence', 1, 1, ?, 'idea', '2026-11-10T00:00:00Z', ?, ?)""",
            (LEAD, THREAD, STAMP, STAMP),
        )
        return cursor.lastrowid


def message(msg_id="m1", from_=f"Lead <{LEAD}>", subject="Re: idea", thread_id=THREAD, to=INBOX, snippet="hello", **headers):
    return MessageMeta(id=msg_id, thread_id=thread_id, snippet=snippet, from_=from_, to=to, subject=subject, **headers)


def deliver(client, meta, text="Sounds good"):
    client.inject_incoming_message(INBOX, meta, text)


def run(settings, client):
    return run_all_inboxes(settings, client, NOW)


def lead_status(lead_id):
    with open_conn() as conn:
        return conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()[0]


def events():
    with open_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]


def labels(client):
    return [row["label_id"] for row in client.applied_labels]


# ------------------------------------------------------------------------------
# Bounces
# ------------------------------------------------------------------------------

def test_bounce_with_failed_recipients_header(settings, client, lead_id):
    deliver(client, message(from_="mailer-daemon@googlemail.com", subject="Failure", x_failed_recipients=LEAD, thread_id="other"), "")

    assert run(settings, client) == {"bounce": 1}

    assert lead_status(lead_id) == "bounced"
    assert is_suppressed(LEAD)
    assert labels(client) == ["lbl_mobisa_bounced"]
    event = events()[0]
    assert (event["type"], event["lead_id"], event["email"]) == ("bounce", lead_id, LEAD)


def test_bounce_parsed_from_the_text(settings, client, lead_id):
    text = f"Address not found\nYour message to {LEAD} could not be delivered.\nFrom: {INBOX}"
    deliver(client, message(from_="postmaster@acme.com", subject="Undeliverable: idea", thread_id="other"), text)

    run(settings, client)

    assert lead_status(lead_id) == "bounced"


def test_bounce_text_skips_addresses_that_are_not_leads(settings, client, lead_id):
    text = f"From: {INBOX}\nTo: someone@else.com\nFailed: <{LEAD}>"
    deliver(client, message(from_="mailer-daemon@x.com", subject="Delivery Status Notification", thread_id="other"), text)

    run(settings, client)

    assert lead_status(lead_id) == "bounced"


@pytest.mark.parametrize(
    "subject",
    ["Delivery Status Notification (Failure)", "Undeliverable: x", "Mail delivery failed", "Address not found", "Returned mail: y"],
)
def test_bounce_subjects(settings, client, lead_id, subject):
    deliver(client, message(from_="someone@relay.com", subject=subject, thread_id="other"), f"failed for {LEAD}")

    run(settings, client)

    assert lead_status(lead_id) == "bounced"


def test_bounce_for_unknown_address_suppresses_nothing(settings, client, lead_id):
    deliver(client, message(from_="mailer-daemon@x.com", subject="Failure", thread_id="other", x_failed_recipients="nobody@else.com"), "")

    assert run(settings, client) == {"bounce": 1}

    assert lead_status(lead_id) == "in_sequence"
    assert not is_suppressed("nobody@else.com")
    assert events()[0]["lead_id"] is None
    assert labels(client) == []


# ------------------------------------------------------------------------------
# Auto-replies
# ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fields",
    [
        {"subject": "Out of office until Monday"},
        {"subject": "Automatic reply: idea"},
        {"subject": "Auto-reply: idea"},
        {"subject": "Réponse automatique : idea"},
        {"subject": "Absence du bureau"},
        {"auto_submitted": "auto-replied"},
        {"x_autoreply": "yes"},
        {"x_autorespond": "yes"},
        {"precedence": "auto_reply"},
        {"precedence": "Bulk"},
    ],
)
def test_auto_reply_is_ignored_and_the_sequence_continues(settings, client, lead_id, fields):
    deliver(client, message(**fields))

    assert run(settings, client) == {"auto_reply_ignored": 1}

    assert lead_status(lead_id) == "in_sequence"
    assert events()[0]["type"] == "auto_reply_ignored"
    assert labels(client) == []


def test_auto_submitted_no_is_a_real_reply(settings, client, lead_id):
    deliver(client, message(auto_submitted="no"))

    assert run(settings, client) == {"reply": 1}


def test_out_of_office_saying_stop_is_not_an_opt_out(settings, client, lead_id):
    deliver(client, message(subject="Out of office"), "I am away. Mail will stop until Monday.")

    run(settings, client)

    assert lead_status(lead_id) == "in_sequence"
    assert not is_suppressed(LEAD)


# ------------------------------------------------------------------------------
# Replies
# ------------------------------------------------------------------------------

def test_normal_reply_by_thread_id(settings, client, lead_id):
    deliver(client, message(from_="Colleague <other@acme.com>", snippet="Yes please"))

    assert run(settings, client) == {"reply": 1}

    assert lead_status(lead_id) == "replied"
    assert labels(client) == ["lbl_mobisa_replied"]
    event = events()[0]
    assert (event["lead_id"], event["subject"], event["snippet"]) == (lead_id, "Re: idea", "Yes please")


def test_reply_by_sender_address_in_a_new_thread(settings, client, lead_id):
    deliver(client, message(thread_id="brand_new"))

    assert run(settings, client) == {"reply": 1}

    assert lead_status(lead_id) == "replied"


def test_message_from_a_stranger_is_ignored(settings, client, lead_id):
    deliver(client, message(from_="stranger@else.com", thread_id="other"))

    assert run(settings, client) == {}

    assert events() == []
    assert lead_status(lead_id) == "in_sequence"


def test_already_processed_message_is_skipped(settings, client, lead_id):
    deliver(client, message())
    run(settings, client)

    assert run(settings, client) == {}

    assert len(events()) == 1
    assert len(client.applied_labels) == 1


def test_later_reply_does_not_undo_an_opt_out(settings, client, lead_id):
    deliver(client, message("m1"), "stop")
    run(settings, client)
    deliver(client, message("m2"), "Thanks for your help")

    run(settings, client)

    assert lead_status(lead_id) == "unsubscribed"


def test_replied_lead_can_still_opt_out(settings, client, lead_id):
    deliver(client, message("m1"), "Tell me more")
    run(settings, client)
    deliver(client, message("m2"), "Actually, stop")

    run(settings, client)

    assert lead_status(lead_id) == "unsubscribed"


# ------------------------------------------------------------------------------
# Opt-outs
# ------------------------------------------------------------------------------

def test_french_stop_opts_out(settings, client, lead_id):
    deliver(client, message(), "Merci de ne plus recevoir vos messages.")

    assert run(settings, client) == {"unsubscribe": 1}

    assert lead_status(lead_id) == "unsubscribed"
    assert is_suppressed(LEAD)
    assert labels(client) == ["lbl_mobisa_unsubscribed"]


@pytest.mark.parametrize(
    "body",
    [
        "stop",
        "STOP",
        "Stop.",
        "Please stop emailing me",
        "unsubscribe",
        "Unsubscribe me please",
        "remove me",
        "Remove  me from your list",
        "opt out",
        "I want to opt out",
        "Je veux me désabonner",
        "Comment me désinscrire ?",
        "Je ne veux ne plus recevoir ça",
        "Merci\n\nstop\n",
        "Stop merci",
        "please just stop this now thank you so",  # 8 words
        "Take me off your list",
        "take  me  off",
    ],
)
def test_opt_out_phrases(body):
    assert is_opt_out(body)


LONG_REPLY = "Thanks for reaching out, we already work with an agency and are happy with them, but "


@pytest.mark.parametrize(
    "ending",
    [
        "please unsubscribe us.",
        "remove me from this list.",
        "I would like to opt out.",
        "please stop emailing this address.",
        "please stop sending these.",
        "please take me off your list.",
        "merci de nous désabonner.",
        "je souhaite me désinscrire.",
        "je ne veux ne plus recevoir ces messages.",
    ],
)
def test_clear_phrase_counts_in_a_long_reply(ending):
    assert is_opt_out(LONG_REPLY + ending)


@pytest.mark.parametrize(
    "body",
    [
        "please stop by our booth",
        "Please stop by our booth at the show next week",
        "Stop by anytime",
        "stop in on Friday",
        "stop over at our stand",
        "stop past our office",
        "please just stop this now thank you so much",  # 9 words
        "We will stop running Meta ads next month and move everything to TikTok",
        LONG_REPLY + "we may stop using them next year.",
    ],
)
def test_stop_alone_is_not_enough_in_longer_or_friendly_replies(body):
    assert not is_opt_out(body)


def test_word_limit_counts_only_the_new_words():
    quoted = "\n".join(f"> line {i} of the old message" for i in range(10))
    assert is_opt_out(f"stop\n\n{quoted}")


def test_footer_lines_do_not_count_toward_the_word_limit():
    body = 'stop\nReply "stop" to opt out now please thank you very much'
    assert is_opt_out(body, ['Reply "stop" to opt out'])


def test_booth_invitation_through_a_full_run_is_a_reply(settings, client, lead_id):
    deliver(client, message(), "please stop by our booth")

    assert run(settings, client) == {"reply": 1}

    assert lead_status(lead_id) == "replied"
    assert not is_suppressed(LEAD)


@pytest.mark.parametrize(
    "body",
    [
        "Yes please, send the sample",
        "We never stopped running ads",
        "A non-stop campaign sounds good",
        "stops and starts",
        "Unstoppable brand",
        "Please remove the link from the sample",
        "remove",
        "We opted out of Meta last year",
        "",
    ],
)
def test_not_opt_outs(body):
    assert not is_opt_out(body)


def test_subject_is_never_read_for_opt_outs(settings, client, lead_id):
    deliver(client, message(subject="Re: idea for Non Stop Nutrition, remove me"), "Yes, send it")

    assert run(settings, client) == {"reply": 1}

    assert not is_suppressed(LEAD)


def test_snippet_is_never_read_for_opt_outs(settings, client, lead_id):
    deliver(client, message(snippet='Yes On Monday Beedu wrote: Reply "stop" to opt out.'), "Yes, send it")

    assert run(settings, client) == {"reply": 1}


@pytest.mark.parametrize(
    "body",
    [
        'Yes please!\n\nOn Mon, Nov 2, 2026 at 5:00 PM Beedu <sales@mobisa.in> wrote:\n> Reply "stop" to opt out.',
        'Yes please!\n\nOn Mon, Nov 2, 2026 at 5:00 PM Beedu from Mobisa <\nsales@mobisa.in> wrote:\n\nReply "stop" to opt out.',
        'Oui merci\n\nLe lun. 2 nov. 2026 à 17:00, Beedu <sales@mobisa.in> a écrit :\n> Répondez « stop » pour ne plus recevoir nos e-mails.',
        'Oui merci\n\nLe lun. 2 nov. 2026 à 17:00, Beedu <sales@mobisa.in> a écrit :\nstop',
        'Yes\n\n-----Original Message-----\nReply "stop" to opt out.',
        'Yes\n\n> stop',
        'Yes\n\nFrom: Beedu <sales@mobisa.in>\nSent: Monday\nTo: Lead\n\nReply "stop" to opt out.',
        'Oui\n\nDe : Beedu <sales@mobisa.in>\nEnvoyé : lundi\nÀ : Lead\n\nstop',
    ],
)
def test_quoted_text_is_never_read(body):
    assert not is_opt_out(body)


def test_words_before_the_quote_still_count():
    assert is_opt_out('stop\n\nOn Mon, Nov 2, 2026 at 5:00 PM Beedu <sales@mobisa.in> wrote:\n> hello')


def test_our_own_footer_line_in_a_reply_is_not_an_opt_out(settings, client, lead_id):
    deliver(client, message(), 'Yes please.\nBeedu from Mobisa · mobisa.in\nReply "stop" to opt out.')

    assert run(settings, client) == {"reply": 1}


def test_strip_quoted_text_keeps_the_new_words():
    assert strip_quoted_text("New words\n> old\nmore new").split("\n") == ["New words", "more new"]


def test_message_to_the_alias_is_an_opt_out_whatever_it_says(settings, client, lead_id):
    deliver(client, message(to="unsubscribe@mobisa.in", thread_id="new", subject="unsubscribe"), "Sent from my phone")

    assert run(settings, client) == {"unsubscribe": 1}

    assert is_suppressed(LEAD)


def test_alias_from_a_stranger_is_ignored(settings, client, lead_id):
    deliver(client, message(from_="stranger@else.com", to="unsubscribe@mobisa.in", thread_id="new"))

    assert run(settings, client) == {}


# ------------------------------------------------------------------------------
# The whole run
# ------------------------------------------------------------------------------

def test_query_for_a_normal_inbox_and_the_alias_inbox(settings, client, lead_id):
    client.connect_inbox("other@mobisa.in")
    with open_conn() as conn:
        conn.execute(
            "INSERT INTO inboxes (id, email, display_name, role, ramp_start_date, created_at) VALUES (2, 'other@mobisa.in', 'O', 'monitor', '2026-11-01', ?)",
            (STAMP,),
        )

    run(settings, client)

    assert client.list_queries == {
        INBOX: "{in:inbox to:unsubscribe@mobisa.in} newer_than:3d -from:me",
        "other@mobisa.in": "in:inbox newer_than:3d -from:me",
    }


def test_monitor_inbox_is_checked(settings, client, lead_id):
    with open_conn() as conn:
        conn.execute("UPDATE inboxes SET role = 'monitor'")
    deliver(client, message())

    assert run(settings, client) == {"reply": 1}


def test_inbox_without_a_token_is_skipped(settings, lead_id):
    assert run(settings, FakeGmailClient()) == {}


def test_last_run_is_recorded(settings, client, lead_id):
    run(settings, client)

    assert get_app_state(detection.LAST_RUN_KEY) == "2026-11-04T15:00:00Z"


def test_finished_sequences_close_during_detection(settings, client, lead_id):
    with open_conn() as conn:
        conn.execute("UPDATE leads SET step = 3, next_due_at = '2026-11-04T14:00:00Z'")

    run(settings, client)

    assert lead_status(lead_id) == "finished"


def test_expired_auth_pauses_the_inbox(settings, client, lead_id):
    client.should_raise = AuthExpired("expired")

    assert run(settings, client) == {}

    with open_conn() as conn:
        assert conn.execute("SELECT paused_reason FROM inboxes").fetchone()[0] == "Reconnect inbox"


def test_rate_limit_skips_the_inbox_without_crashing(settings, client, lead_id):
    client.should_raise = RateLimited("slow")

    assert run(settings, client) == {}


def test_message_that_cannot_be_read_is_retried_next_run(settings, client, lead_id, monkeypatch):
    deliver(client, message())
    real_get_text = client.get_message_text

    def fail(email_addr, msg_id):
        raise GmailClientError("broken")

    monkeypatch.setattr(client, "get_message_text", fail)
    assert run(settings, client) == {}
    assert events() == []

    monkeypatch.setattr(client, "get_message_text", real_get_text)
    assert run(settings, client) == {"reply": 1}


def test_failed_label_keeps_the_detection(settings, client, lead_id, monkeypatch):
    deliver(client, message())

    def fail(email_addr, name):
        raise GmailClientError("no label")

    monkeypatch.setattr(client, "ensure_label", fail)

    assert run(settings, client) == {"reply": 1}
    assert lead_status(lead_id) == "replied"


def test_detection_never_sends_anything(settings, client, lead_id):
    deliver(client, message())

    run(settings, client)

    assert client.sent_messages == []
    assert client.send_attempts == 0
