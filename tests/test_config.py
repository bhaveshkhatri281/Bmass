"""Tests for settings loading: dry run must fail safe."""

import pytest

from mailer.config import ConfigError, Settings, load_settings, save_settings


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


def test_name_exceptions_default_and_round_trip(tmp_path):
    assert load_from(tmp_path, "").names.keep_suffix == ["Nails Inc."]

    settings = load_from(tmp_path, "names:\n  keep_suffix: []\n")
    assert settings.names.keep_suffix == []

    settings.names.keep_suffix = ["Nails Inc.", "Acme Ltd"]
    path = tmp_path / "out.yaml"
    save_settings(settings, path)
    assert load_settings(path).names.keep_suffix == ["Nails Inc.", "Acme Ltd"]


def test_delay_min_above_max_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_from(tmp_path, "worker:\n  delay_between_sends_seconds: { min: 300, max: 100 }\n")


def test_real_settings_file_is_in_dry_run():
    assert load_settings().mode.dry_run is True
    assert Settings().mode.dry_run is True
