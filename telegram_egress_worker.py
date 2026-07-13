"""Dedicated unprivileged supervisor for Telegram managed Xray tunnels."""

import os
import signal
import time

os.environ.setdefault("DISABLE_BACKGROUND_THREADS", "true")
os.environ.setdefault("EVE_PROCESS_ROLE", "telegram-egress")

from app import (  # noqa: E402
    TelegramEgressProfile, _decrypt_telegram_secret, app, db,
)
from telegram_diagnostics import redact_connection_error  # noqa: E402
from telegram_xray import XraySupervisor  # noqa: E402


running = True


def _stop(_signum, _frame):
    global running
    running = False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    directory = os.environ.get(
        "EVE_TELEGRAM_EGRESS_DIR",
        os.path.join(app.instance_path, "telegram-egress"),
    )
    supervisor = XraySupervisor(directory, os.environ.get("XRAY_BIN"))
    try:
        while running:
            with app.app_context():
                all_profiles = TelegramEgressProfile.query.all()
                profiles = [profile for profile in all_profiles if profile.enabled]
                supervisor.cleanup_orphans(profile.id for profile in all_profiles)
                active_ids = {profile.id for profile in profiles}
                for profile in all_profiles:
                    if not profile.enabled:
                        supervisor.stop(profile.id, remove_config=True)
                        profile.runtime_status = 'disabled'
                        profile.runtime_pid = None
                for profile_id in list(supervisor.processes):
                    if profile_id not in active_ids:
                        supervisor.stop(profile_id, remove_config=True)
                for profile in profiles:
                    uri = _decrypt_telegram_secret(profile.config_encrypted)
                    try:
                        result = supervisor.sync(profile.id, uri, profile.local_port)
                    except Exception as exc:
                        result = {"success": False, "state": "failed",
                                  "error": redact_connection_error(exc, (uri,))}
                    profile.runtime_status = result.get("state")
                    profile.runtime_pid = result.get("pid")
                    profile.last_error = redact_connection_error(result.get("error"), (uri,)) if result.get("error") else None
                    profile.last_heartbeat_at = db.func.now()
                db.session.commit()
            for _ in range(10):
                if not running:
                    break
                time.sleep(1)
    finally:
        supervisor.stop_all()


if __name__ == "__main__":
    main()
