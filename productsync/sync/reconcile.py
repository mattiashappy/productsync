"""Periodic reconciliation: scheduled safety net.

For now this is a heartbeat that touches `last_sync_at`. Per-product drift
detection (pull remote, diff against dashboard, re-push if drift) is left as a
Phase-2 step once we have at least one channel actually pulling cleanly.
"""
from __future__ import annotations

from datetime import datetime

from ..extensions import db
from ..models import ChannelAccount


def heartbeat() -> None:
    accounts = db.session.query(ChannelAccount).filter_by(status="active").all()
    for account in accounts:
        account.last_sync_at = datetime.utcnow()
    db.session.commit()
