"""Background sending worker entry point."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import time
from typing import Optional

import yaml

from mailer import detection, engine, safety
from mailer.config import ConfigError, Settings, load_settings
from mailer.db import get_app_state, init_db, open_conn, set_app_state, utc_now_iso
from mailer.logs import setup_logging
from mailer.scheduling import in_app_window
from mailer.seed import seed_database

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
    with open_conn() as conn:
        # Hold the write lock while checking, so two starts cannot both pass.
        conn.execute("BEGIN IMMEDIATE")
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
            except ValueError as e:
                logger.warning("Error checking existing worker state: %s", e)

        set_app_state("worker_pid", str(cur_pid), conn=conn)
        set_app_state("worker_heartbeat", utc_now_iso(), conn=conn)
        return True


def release_worker_lock() -> None:
    """Clear the lock on exit, so the dashboard shows a stopped worker straight away."""
    with open_conn() as conn:
        if get_app_state("worker_pid", conn=conn) == str(os.getpid()):
            set_app_state("worker_pid", None, conn=conn)
            set_app_state("worker_heartbeat", None, conn=conn)


def update_heartbeat() -> None:
    """Record current UTC timestamp into app_state worker_heartbeat."""
    set_app_state("worker_heartbeat", utc_now_iso())


def reload_settings(last_good: Optional[Settings]) -> Settings:
    """Read settings fresh, so dashboard changes apply on the next loop. Keep the last good copy on failure."""
    try:
        return load_settings()
    except (ConfigError, OSError, yaml.YAMLError) as e:
        if last_good is None:
            raise
        logger.error("Could not read settings, keeping the previous ones: %s", e)
        return last_good


def detection_is_due(settings: Settings) -> bool:
    """Return True if the last detection run is older than the configured gap."""
    last_run = get_app_state(detection.LAST_RUN_KEY)
    if not last_run:
        return True
    last_time = datetime.datetime.fromisoformat(last_run.replace("Z", "+00:00"))
    age = datetime.datetime.now(datetime.timezone.utc) - last_time
    return age >= datetime.timedelta(minutes=settings.worker.detection_every_minutes)


def run_detection(settings: Settings) -> None:
    """Check inboxes and apply the safety rules. A failure here must not stop sending."""
    try:
        with open_conn() as conn:
            detection.run_all_inboxes(settings, conn=conn)
            safety.evaluate_all_inboxes(settings, conn)
    except Exception as e:
        logger.exception("Detection failed: %s", e)


def run_once() -> None:
    """Run a single engine tick."""
    logger.info("Executing worker in single-tick mode (--once).")
    init_db()
    seed_database()
    if not acquire_worker_lock():
        return
    try:
        sent = engine.tick(load_settings())
        logger.info("Single tick complete. Email logged: %s", sent)
    finally:
        release_worker_lock()


def run_loop() -> None:
    """Run the main sending loop inside the configured IST sending window."""
    init_db()
    seed_database()
    if not acquire_worker_lock():
        return

    logger.info("Worker started with PID %d.", os.getpid())
    settings: Optional[Settings] = None

    try:
        while True:
            update_heartbeat()
            settings = reload_settings(settings)

            if not in_app_window(settings=settings):
                logger.info("Outside of IST sending window (%s to %s). Worker stopping.",
                            settings.worker.app_window_ist.start, settings.worker.app_window_ist.end)
                break

            # This runs even during a global pause, so opt-outs are never missed.
            if detection_is_due(settings):
                run_detection(settings)

            if get_app_state(safety.PAUSED_ALL_KEY, "0") == "1":
                time.sleep(settings.worker.idle_sleep_seconds)
                continue

            try:
                sent_something = engine.tick(settings)
            except Exception as e:
                # One bad tick must never stop the worker for the night.
                logger.exception("Tick failed: %s", e)
                sent_something = False

            time.sleep(1 if sent_something else settings.worker.idle_sleep_seconds)
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")
    except Exception as e:
        logger.exception("Unexpected error in worker loop: %s", e)
    finally:
        release_worker_lock()
        logger.info("Worker exited.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobisa Mailer Background Worker")
    parser.add_argument("--run", action="store_true", help="Run long-lived worker loop inside sending window")
    parser.add_argument("--once", action="store_true", help="Run a single engine tick")
    args = parser.parse_args()

    setup_logging()

    if args.once:
        run_once()
    elif args.run:
        run_loop()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
