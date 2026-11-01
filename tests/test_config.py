"""Tests for settings loading: dry run must fail safe."""

import pytest

from mailer.config import ConfigError, Settings, live_blockers, load_settings, save_settings


def load_from(tmp_path, text):
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return load_settings(path)


@pytest.mark.parametrize(
    "text",
    ["mode:\n  dry_run:\n", "mode:\n", "", 'mode:\n  dry_run: "false"\n', "mode:\n  dry_run: true\n"],
)
def test_dry_run_stays_on_unless_explicitly_false(tmp_path, text):
    assert load_from(tmp_path, text).mode.dry_run is True


def test_explicit_false_goes_live(tmp_path):
    assert load_from(tmp_path, "mode:\n  dry_run: false\n").mode.dry_run is False


def test_unsubscribe_inbox_round_trip(tmp_path):
    settings = load_from(tmp_path, "sender:\n  unsubscribe_inbox: ' Beedu@Mobisa.in '\n")
    assert settings.sender.unsubscribe_inbox == "beedu@mobisa.in"

    path = tmp_path / "out.yaml"
    save_settings(settings, path)
    assert load_settings(path).sender.unsubscribe_inbox == "beedu@mobisa.in"


@pytest.mark.parametrize(
    "text",
    ["", "mode:\n  test_leads_only:\n", 'mode:\n  test_leads_only: "false"\n', "mode:\n  test_leads_only: true\n"],
)
def test_test_leads_lock_stays_on_unless_explicitly_false(tmp_path, text):
    assert load_from(tmp_path, text).mode.test_leads_only is True


def test_test_leads_lock_can_be_turned_off_and_saved(tmp_path):
    settings = load_from(tmp_path, "mode:\n  test_leads_only: false\n")
    assert settings.mode.test_leads_only is False

    path = tmp_path / "out.yaml"
    save_settings(settings, path)
    assert load_settings(path).mode.test_leads_only is False


def test_real_settings_file_keeps_real_leads_locked():
    assert load_settings().mode.test_leads_only is True


def test_us_reservation_defaults_and_round_trip(tmp_path):
    settings = load_from(tmp_path, "")
    assert (settings.worker.us_reservation.percent, settings.worker.us_reservation.until_ist) == (50, "22:30")

    settings.worker.us_reservation.percent = 30
    settings.worker.us_reservation.until_ist = "21:00"
    path = tmp_path / "out.yaml"
    save_settings(settings, path)
    saved = load_settings(path).worker.us_reservation
    assert (saved.percent, saved.until_ist) == (30, "21:00")


@pytest.mark.parametrize("text", ["percent: 101", "percent: -1", 'until_ist: "25:00"'])
def test_bad_us_reservation_is_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_from(tmp_path, f"worker:\n  us_reservation: {{ {text} }}\n")


def test_name_exceptions_default_and_round_trip(tmp_path):
    assert load_from(tmp_path, "").names.keep_suffix == ["Nails Inc."]

    settings = load_from(tmp_path, "names:\n  keep_suffix: []\n")
    assert settings.names.keep_suffix == []

    settings.names.keep_suffix = ["Nails Inc.", "Acme Ltd"]
    path = tmp_path / "out.yaml"
    save_settings(settings, path)
    assert load_settings(path).names.keep_suffix == ["Nails Inc.", "Acme Ltd"]


def test_live_blockers():
    settings = Settings()
    assert live_blockers(settings) == [
        "Business address is empty.",
        "Portfolio link is empty.",
        "Website is empty.",
    ]

    settings.sender.business_address = "Jaipur"
    settings.sender.portfolio_link = "https://mobisa.in"
    settings.sender.website = "mobisa.in"
    assert live_blockers(settings) == []


def test_failed_save_leaves_the_file_alone(tmp_path):
    path = tmp_path / "settings.yaml"
    settings = Settings()
    save_settings(settings, path)
    original = path.read_text(encoding="utf-8")

    settings.worker.hard_daily_cap = 0
    with pytest.raises(ConfigError):
        save_settings(settings, path)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_delay_min_above_max_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_from(tmp_path, "worker:\n  delay_between_sends_seconds: { min: 300, max: 100 }\n")


def test_real_settings_file_is_in_dry_run():
    assert load_settings().mode.dry_run is True
    assert Settings().mode.dry_run is True
