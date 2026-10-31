"""Background sending worker entry point."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import time
from pathlib import Path

from mailer.config import load_settings
from mailer.db import get_app_state, get_conn, init_db, set_app_state, utc_now_iso
from mailer.seed import seed_database
from mailer.ui import is_in_app_window

# Configure logging
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
today_str = datetime.date.today().isoformat()
log_file = LOG_DIR / f"mailer_{today_str}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("worker")


def is_pid_alive(pid: int) -> bool:
    """Check if a process ID is currently running on Windows."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        # If open succeeded, check exit code
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        STILL_ACTIVE = 259
        return exit_code.value == STILL_ACTIVE
    except Exception:
        return False


def acquire_worker_lock() -> bool:
    """Ensure single instance of the background worker."""
    cur_pid = os.getpid()
    with get_conn() as conn:
        last_pid_str = get_app_state("worker_pid", conn=conn)
        last_hb_str = get_app_state("worker_heartbeat", conn=conn)

        if last_pid_str and last_hb_str:
            try:
                last_pid = int(last_pid_str)
                last_hb = datetime.datetime.fromisoformat(last_hb_str.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                age = (now - last_hb).total_seconds()
                if age < 60 and is_pid_alive(last_pid):
                    logger.warning("Another active worker (PID %s, heartbeat %.1fs ago) is running. Exiting.", last_pid, age)
                    return False
            except Exception as e:
                logger.warning("Error checking existing worker state: %s", e)

        set_app_state("worker_pid", str(cur_pid), conn=conn)
        set_app_state("worker_heartbeat", utc_now_iso(), conn=conn)
        return True


def update_heartbeat() -> None:
    """Record current UTC timestamp into app_state worker_heartbeat."""
    set_app_state("worker_heartbeat", utc_now_iso())


def run_once() -> None:
    """Run a single engine tick."""
    logger.info("Executing worker in single-tick mode (--once).")
    init_db()
    seed_database()
    update_heartbeat()
    # Engine tick will be hooked in Milestone 4
    logger.info("Single tick complete.")


def run_loop() -> None:
    """Run the main sending loop inside the configured IST sending window."""
    if not acquire_worker_lock():
        return

    logger.info("Worker started with PID %d.", os.getpid())
    init_db()
    seed_database()

    try:
        settings = load_settings()
        while True:
            update_heartbeat()
            # Check app window
            in_window = is_in_app_window(
                settings.worker.app_window_ist.start,
                settings.worker.app_window_ist.end,
            )
            if not in_window:
                logger.info("Outside of IST sending window (%s to %s). Worker stopping.",
                            settings.worker.app_window_ist.start, settings.worker.app_window_ist.end)
                break

            paused_all = get_app_state("paused_all", "0") == "1"
            if paused_all:
                time.sleep(settings.worker.idle_sleep_seconds)
                continue

            # In Milestone 4: engine.tick()
            time.sleep(settings.worker.idle_sleep_seconds)
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")
    except Exception as e:
        logger.exception("Unexpected error in worker loop: %s", e)
    finally:
        logger.info("Worker exited.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobisa Mailer Background Worker")
    parser.add_argument("--run", action="store_true", help="Run long-lived worker loop inside sending window")
    parser.add_argument("--once", action="store_true", help="Run a single engine tick")
    args = parser.parse_args()

    if args.once:
        run_once()
    elif args.run:
        run_loop()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

