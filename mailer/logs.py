"""Logging setup: a daily file in logs\\, kept for 30 days, plus the console."""

from __future__ import annotations

import datetime
import logging
import os
import re
import sys
from pathlib import Path

from mailer.config import PROJECT_ROOT

DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
KEEP_DAYS = 30
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_NAME_RE = re.compile(r"^mailer_(\d{4}-\d{2}-\d{2})\.log$")


class DailyFileHandler(logging.FileHandler):
    """Write to logs\\mailer_YYYY-MM-DD.log, moving to a new file when the date changes."""

    def __init__(self, log_dir: Path | str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_date = self._today()
        super().__init__(self._path_for(self.current_date), encoding="utf-8")
        self._remove_old_logs()

    def _today(self) -> datetime.date:
        return datetime.date.today()

    def _path_for(self, day: datetime.date) -> Path:
        return self.log_dir / f"mailer_{day.isoformat()}.log"

    def _remove_old_logs(self) -> None:
        cutoff = self.current_date - datetime.timedelta(days=KEEP_DAYS)
        for path in self.log_dir.glob("mailer_*.log"):
            match = _LOG_NAME_RE.match(path.name)
            if match and datetime.date.fromisoformat(match.group(1)) < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass  # The other process may hold it open. The next run retries.

    def emit(self, record: logging.LogRecord) -> None:
        today = self._today()
        if today != self.current_date:
            if self.stream:
                self.stream.close()
                self.stream = None
            self.current_date = today
            self.baseFilename = os.path.abspath(self._path_for(today))
            self._remove_old_logs()
        super().emit(record)


def setup_logging(log_dir: Path | str = DEFAULT_LOG_DIR) -> None:
    """Attach the daily file and console handlers once per process."""
    root = logging.getLogger()
    if any(isinstance(handler, DailyFileHandler) for handler in root.handlers):
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = DailyFileHandler(log_dir)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # pythonw.exe and detached processes have no stdout.
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)
