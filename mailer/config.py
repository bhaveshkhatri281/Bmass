"""Configuration loader and validator for settings.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "settings.yaml"


class ConfigError(Exception):
    """Raised when configuration validation fails."""


@dataclass
class SenderSettings:
    default_display_name: str = "Beedu from Mobisa"
    sender_title: str = ""
    website: str = ""
    booking_link: str = ""
    portfolio_link: str = ""
    business_address: str = ""
    list_unsubscribe_address: str = "unsubscribe@mobisa.in"
    unsubscribe_inbox: str = ""


@dataclass
class ModeSettings:
    dry_run: bool = True
    test_leads_only: bool = True


@dataclass
class DelaySettings:
    min: int = 90
    max: int = 240


@dataclass
class AppWindowSettings:
    start: str = "18:00"
    end: str = "00:30"


@dataclass
class RecipientWindowSettings:
    start: str = "09:00"
    end: str = "17:00"
    weekdays_only: bool = True


@dataclass
class USReservationSettings:
    percent: int = 50
    until_ist: str = "22:30"


@dataclass
class WorkerSettings:
    app_window_ist: AppWindowSettings = field(default_factory=AppWindowSettings)
    recipient_window: RecipientWindowSettings = field(default_factory=RecipientWindowSettings)
    delay_between_sends_seconds: DelaySettings = field(default_factory=DelaySettings)
    detection_every_minutes: int = 10
    idle_sleep_seconds: int = 5
    hard_daily_cap: int = 150
    us_reservation: USReservationSettings = field(default_factory=USReservationSettings)


@dataclass
class SafetySettings:
    bounce_pause_threshold: int = 3
    bounce_pause_window: int = 50
    pause_on_quota_hours: int = 24


@dataclass
class ImportSettings:
    skip_quality: List[str] = field(default_factory=lambda: ["Bad"])


@dataclass
class NameSettings:
    keep_suffix: List[str] = field(default_factory=lambda: ["Nails Inc."])


@dataclass
class Settings:
    sender: SenderSettings = field(default_factory=SenderSettings)
    mode: ModeSettings = field(default_factory=ModeSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    import_: ImportSettings = field(default_factory=ImportSettings)
    names: NameSettings = field(default_factory=NameSettings)


def _validate_time_str(val: str, field_name: str) -> None:
    if not isinstance(val, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", val):
        raise ConfigError(f"Invalid time format for {field_name}: '{val}'. Expected 'HH:MM' (24-hour).")


def validate_settings(settings: Settings) -> None:
    """Validate settings constraints and business rules."""
    _validate_time_str(settings.worker.app_window_ist.start, "worker.app_window_ist.start")
    _validate_time_str(settings.worker.app_window_ist.end, "worker.app_window_ist.end")
    _validate_time_str(settings.worker.recipient_window.start, "worker.recipient_window.start")
    _validate_time_str(settings.worker.recipient_window.end, "worker.recipient_window.end")
    _validate_time_str(settings.worker.us_reservation.until_ist, "worker.us_reservation.until_ist")

    if not 0 <= settings.worker.us_reservation.percent <= 100:
        raise ConfigError("worker.us_reservation.percent must be between 0 and 100.")

    delays = settings.worker.delay_between_sends_seconds
    if delays.min < 0:
        raise ConfigError("worker.delay_between_sends_seconds.min must be non-negative.")
    if delays.max < delays.min:
        raise ConfigError("worker.delay_between_sends_seconds.max must be >= min.")

    if settings.worker.hard_daily_cap <= 0:
        raise ConfigError("worker.hard_daily_cap must be > 0.")
    if settings.worker.detection_every_minutes <= 0:
        raise ConfigError("worker.detection_every_minutes must be > 0.")
    if settings.worker.idle_sleep_seconds <= 0:
        raise ConfigError("worker.idle_sleep_seconds must be > 0.")

    if settings.safety.bounce_pause_threshold <= 0:
        raise ConfigError("safety.bounce_pause_threshold must be > 0.")
    if settings.safety.bounce_pause_window <= 0:
        raise ConfigError("safety.bounce_pause_window must be > 0.")
    if settings.safety.pause_on_quota_hours <= 0:
        raise ConfigError("safety.pause_on_quota_hours must be > 0.")


def live_blockers(settings: Settings) -> List[str]:
    """Return what must be filled in before LIVE mode is allowed."""
    required = {
        "Business address": settings.sender.business_address,
        "Portfolio link": settings.sender.portfolio_link,
        "Website": settings.sender.website,
        "Unsubscribe address": settings.sender.list_unsubscribe_address,
    }
    return [f"{label} is empty." for label, value in required.items() if not value.strip()]


def load_settings(path: Path | str | None = None) -> Settings:
    """Load and validate settings from a YAML file."""
    config_path = Path(path) if path else DEFAULT_SETTINGS_PATH
    if not config_path.exists():
        settings = Settings()
        save_settings(settings, config_path)
        return settings

    with open(config_path, "r", encoding="utf-8") as f:
        data: Dict[str, Any] = yaml.safe_load(f) or {}

    sender_data = data.get("sender") or {}
    mode_data = data.get("mode") or {}
    worker_data = data.get("worker") or {}
    safety_data = data.get("safety") or {}
    import_data = data.get("import") or {}
    names_data = data.get("names") or {}

    app_win_data = worker_data.get("app_window_ist") or {}
    recip_win_data = worker_data.get("recipient_window") or {}
    delay_data = worker_data.get("delay_between_sends_seconds") or {}
    reservation_data = worker_data.get("us_reservation") or {}

    settings = Settings(
        sender=SenderSettings(
            default_display_name=sender_data.get("default_display_name", "Beedu from Mobisa"),
            sender_title=sender_data.get("sender_title", "") or "",
            website=sender_data.get("website", "") or "",
            booking_link=sender_data.get("booking_link", "") or "",
            portfolio_link=sender_data.get("portfolio_link", "") or "",
            business_address=sender_data.get("business_address", "") or "",
            list_unsubscribe_address=sender_data.get("list_unsubscribe_address", "unsubscribe@mobisa.in") or "",
            unsubscribe_inbox=(sender_data.get("unsubscribe_inbox") or "").strip().lower(),
        ),
        mode=ModeSettings(
            # Only an explicit false goes live. Blank or unreadable values stay in dry run.
            dry_run=mode_data.get("dry_run", True) is not False,
            # Same rule: real leads stay locked unless this is explicitly false.
            test_leads_only=mode_data.get("test_leads_only", True) is not False,
        ),
        worker=WorkerSettings(
            app_window_ist=AppWindowSettings(
                start=app_win_data.get("start", "18:00"),
                end=app_win_data.get("end", "00:30"),
            ),
            recipient_window=RecipientWindowSettings(
                start=recip_win_data.get("start", "09:00"),
                end=recip_win_data.get("end", "17:00"),
                weekdays_only=bool(recip_win_data.get("weekdays_only", True)),
            ),
            delay_between_sends_seconds=DelaySettings(
                min=int(delay_data.get("min", 90)),
                max=int(delay_data.get("max", 240)),
            ),
            detection_every_minutes=int(worker_data.get("detection_every_minutes", 10)),
            idle_sleep_seconds=int(worker_data.get("idle_sleep_seconds", 5)),
            hard_daily_cap=int(worker_data.get("hard_daily_cap", 150)),
            us_reservation=USReservationSettings(
                percent=int(reservation_data.get("percent", 50)),
                until_ist=reservation_data.get("until_ist", "22:30"),
            ),
        ),
        safety=SafetySettings(
            bounce_pause_threshold=int(safety_data.get("bounce_pause_threshold", 3)),
            bounce_pause_window=int(safety_data.get("bounce_pause_window", 50)),
            pause_on_quota_hours=int(safety_data.get("pause_on_quota_hours", 24)),
        ),
        import_=ImportSettings(
            skip_quality=list(import_data.get("skip_quality", ["Bad"])),
        ),
        names=NameSettings(
            keep_suffix=list(names_data.get("keep_suffix", ["Nails Inc."]) or []),
        ),
    )

    validate_settings(settings)
    return settings


def save_settings(settings: Settings, path: Path | str | None = None) -> None:
    """Validate and write settings back to YAML."""
    validate_settings(settings)
    config_path = Path(path) if path else DEFAULT_SETTINGS_PATH

    raw: Dict[str, Any] = {
        "sender": asdict(settings.sender),
        "mode": asdict(settings.mode),
        "worker": {
            "app_window_ist": asdict(settings.worker.app_window_ist),
            "recipient_window": asdict(settings.worker.recipient_window),
            "delay_between_sends_seconds": asdict(settings.worker.delay_between_sends_seconds),
            "detection_every_minutes": settings.worker.detection_every_minutes,
            "idle_sleep_seconds": settings.worker.idle_sleep_seconds,
            "hard_daily_cap": settings.worker.hard_daily_cap,
            "us_reservation": asdict(settings.worker.us_reservation),
        },
        "safety": asdict(settings.safety),
        "import": asdict(settings.import_),
        "names": asdict(settings.names),
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    # The worker rereads this file on every loop, so it must never see half a write.
    temp_path = config_path.with_suffix(".yaml.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    os.replace(temp_path, config_path)

