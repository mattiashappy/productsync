"""Inbound webhook routes.

Both endpoints follow the same pattern:
  1. Look up the ChannelAccount by URL parameter
  2. Pass raw headers + body to the channel adapter to verify signature
  3. Persist raw WebhookEvent (always — even if signature invalid, for debugging)
  4. If valid, enqueue async processing
  5. Return 200 OK fast

Webhook endpoints are intentionally NOT login-required.
"""
from __future__ import annotations

from flask import Blueprint, abort, current_app, request

from ..channels import get_channel
from ..extensions import db
from ..models import ChannelAccount, WebhookEvent

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@bp.route("/<channel>/<int:account_id>", methods=["POST"])
def receive(channel: str, account_id: int):
    account = db.session.get(ChannelAccount, account_id)
    if account is None or account.channel != channel:
        abort(404)

    body = request.get_data(cache=False)
    headers = {k: v for k, v in request.headers.items()}

    try:
        adapter = get_channel(account)
        event = adapter.parse_webhook(headers, body)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Webhook parse failure: %s", exc)
        # Still persist for postmortem
        db.session.add(
            WebhookEvent(
                channel_account_id=account.id,
                topic="error.parse",
                signature_valid=False,
                payload={"error": str(exc), "headers": headers},
            )
        )
        db.session.commit()
        return ("", 200)

    row = WebhookEvent(
        channel_account_id=account.id,
        topic=event.topic,
        signature_valid=event.signature_valid,
        payload=event.payload,
    )
    db.session.add(row)
    db.session.commit()

    if event.signature_valid:
        try:
            from ..queue import get_queue
            from ..jobs import process_webhook_event
            get_queue().enqueue(process_webhook_event, row.id, retry=None, job_timeout=120)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.warning("RQ enqueue failed (Redis down?): %s", exc)
            # Fall back to inline processing so we don't lose the event in dev.
            try:
                from ..jobs import process_webhook_event as _proc
                _proc(row.id)
            except Exception:
                current_app.logger.exception("Inline webhook processing failed")

    return ("", 200)
