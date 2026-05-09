"""Hourly reconciliation heartbeat. Runs inside the web dyno."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler


def start_scheduler(app) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)

    def _hourly():
        with app.app_context():
            from .sync.reconcile import heartbeat
            heartbeat()

    scheduler.add_job(_hourly, "interval", hours=1, id="hourly_reconcile", replace_existing=True)
    scheduler.start()
    return scheduler
