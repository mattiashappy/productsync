"""RQ task definitions. Imported by the worker process.

Each task creates its own Flask app context so SQLAlchemy works.
Webhook handler enqueues `process_webhook_event(event_id)`; sync UI enqueues
`enqueue_push_product(product_id)`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import create_app
from .channels import get_channel
from .channels.base import ChannelEvent, ChannelError
from .extensions import db
from .models import ChannelAccount, ChannelLink, WebhookEvent
from .sync.push import mirror_inventory, push_product

_app = None


def _get_app():
    global _app
    if _app is None:
        _app = create_app()
    return _app


def process_webhook_event(event_id: int) -> None:
    """Pulled off the queue by the worker. Re-applies the verified event."""
    app = _get_app()
    with app.app_context():
        event_row = db.session.get(WebhookEvent, event_id)
        if event_row is None or not event_row.signature_valid:
            return
        if event_row.processed_at is not None:
            return  # idempotent

        account = event_row.channel_account
        if account is None:
            return
        try:
            adapter = get_channel(account)
            event = ChannelEvent(
                topic=event_row.topic,
                signature_valid=True,
                payload=event_row.payload or {},
            )
            adapter.apply_webhook(event)

            # Mirror inventory across other channels
            for line in (event_row.payload or {}).get("line_items", []):
                sku = (line.get("sku") or "").strip()
                if not sku:
                    continue
                source_link = (
                    db.session.query(ChannelLink)
                    .filter_by(channel_account_id=account.id, remote_sku=sku)
                    .first()
                )
                if source_link is None:
                    continue
                # Read the freshly-decremented variant quantity if we have a variant.
                variant = source_link.product.variants[0] if source_link.product and source_link.product.variants else None
                if variant is None:
                    continue
                mirror_inventory(source_link, sku, variant.inventory_quantity or 0)

            event_row.processed_at = datetime.utcnow()
            event_row.process_error = None
            db.session.commit()
        except (ChannelError, Exception) as exc:  # noqa: BLE001
            db.session.rollback()
            event_row = db.session.get(WebhookEvent, event_id)
            if event_row is not None:
                event_row.process_error = str(exc)
                db.session.commit()
            raise


def enqueue_push_product(product_id: int, channel_account_id: int | None = None) -> None:
    app = _get_app()
    with app.app_context():
        push_product(product_id, channel_account_id)


def reconcile_all() -> None:
    app = _get_app()
    with app.app_context():
        from .sync.reconcile import heartbeat
        heartbeat()
