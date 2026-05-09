"""Shopify Admin REST API adapter.

Auth: custom-app token, sent as `X-Shopify-Access-Token`.
Webhooks: HMAC-SHA256 of the raw body with the shop's webhook secret,
base64-encoded, in `X-Shopify-Hmac-SHA256`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from ..crypto import decrypt
from ..extensions import db
from ..models import ChannelLink, Order, Product, ProductVariant
from .base import Channel, ChannelError, ChannelEvent, register


@register
class ShopifyChannel(Channel):
    name = "shopify"
    api_path = "/admin/api/2024-07"

    def _client(self) -> httpx.Client:
        token = decrypt(self.channel_account.api_key_enc)
        if not token:
            raise ChannelError("Shopify access token missing or unreadable.")
        base = self.channel_account.base_url.rstrip("/") + self.api_path
        return httpx.Client(
            base_url=base,
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    # ── Push ────────────────────────────────────────────────────────────────
    def push_product(self, product: Product, link: ChannelLink | None) -> ChannelLink:
        body = {"product": self._serialize_product(product)}
        with self._client() as client:
            if link and link.remote_product_id:
                resp = client.put(f"/products/{link.remote_product_id}.json", json=body)
            else:
                resp = client.post("/products.json", json=body)
            if resp.status_code >= 400:
                raise ChannelError(f"Shopify push failed [{resp.status_code}]: {resp.text[:300]}")
            data = resp.json().get("product", {})

        if link is None:
            link = ChannelLink(
                product_id=product.id,
                channel_account_id=self.channel_account.id,
            )
            db.session.add(link)
        link.remote_product_id = str(data.get("id"))
        link.remote_sku = product.sku
        link.last_pushed_at = datetime.utcnow()
        link.remote_version = str(data.get("updated_at") or "")
        return link

    def push_inventory(self, link: ChannelLink, quantity: int) -> None:
        # Shopify inventory needs an inventory_item_id + location_id; for MVP
        # we re-push the product, which Shopify also accepts as an inventory update
        # for simple products. A future iteration should switch to the dedicated
        # /inventory_levels/set.json endpoint for proper multi-location stock.
        if not link.remote_product_id:
            raise ChannelError("Cannot push inventory without remote_product_id.")
        with self._client() as client:
            resp = client.put(
                f"/products/{link.remote_product_id}.json",
                json={"product": {"id": int(link.remote_product_id), "variants": [
                    {"id": int(link.remote_variant_id), "inventory_quantity": int(quantity)}
                    if link.remote_variant_id else
                    {"inventory_quantity": int(quantity)}
                ]}},
            )
            if resp.status_code >= 400:
                raise ChannelError(f"Shopify inventory push failed [{resp.status_code}]: {resp.text[:300]}")

    # ── Webhook ─────────────────────────────────────────────────────────────
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> ChannelEvent:
        secret = decrypt(self.channel_account.webhook_secret_enc) or ""
        sig_header = headers.get("X-Shopify-Hmac-SHA256", "")
        topic = headers.get("X-Shopify-Topic", "unknown")

        expected = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        valid = bool(secret) and hmac.compare_digest(expected, sig_header)

        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        return ChannelEvent(topic=topic, signature_valid=valid, payload=payload, raw_body=body)

    def apply_webhook(self, event: ChannelEvent) -> None:
        if not event.signature_valid:
            return
        if event.topic in ("orders/create", "orders/paid"):
            self._record_order(event.payload)

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _serialize_product(self, product: Product) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": product.title,
            "body_html": product.description or "",
            "status": "active" if product.status == "active" else "draft",
            "images": [{"src": img.url, "alt": img.alt or ""} for img in product.images],
        }
        options: list[dict[str, Any]] = []
        for idx in (1, 2, 3):
            names = {getattr(v, f"option{idx}_name") for v in product.variants}
            names.discard(None)
            if names:
                options.append({"name": next(iter(names))})
        if options:
            body["options"] = options

        body["variants"] = [
            {
                "sku": v.sku,
                "price": str(v.price_override if v.price_override is not None else (product.price or 0)),
                "inventory_quantity": int(v.inventory_quantity or 0),
                "inventory_management": "shopify",
                "option1": v.option1_value,
                "option2": v.option2_value,
                "option3": v.option3_value,
                "barcode": v.barcode,
            }
            for v in product.variants
        ] or [
            {
                "sku": product.sku,
                "price": str(product.price or 0),
                "inventory_quantity": 0,
                "inventory_management": "shopify",
            }
        ]
        return body

    def _record_order(self, payload: dict[str, Any]) -> None:
        remote_order_id = str(payload.get("id") or "")
        if not remote_order_id:
            return
        currency = payload.get("currency") or "SEK"
        ordered_at = _parse_iso(payload.get("created_at")) or datetime.utcnow()

        for line in payload.get("line_items", []):
            sku = (line.get("sku") or "").strip()
            if not sku:
                continue
            qty = int(line.get("quantity") or 1)
            total = _to_decimal(line.get("price"))
            if total is not None:
                total = total * qty

            variant = (
                db.session.query(ProductVariant)
                .filter(ProductVariant.sku == sku)
                .join(Product, Product.id == ProductVariant.product_id)
                .filter(Product.account_id == self.channel_account.account_id)
                .first()
            )

            existing = (
                db.session.query(Order)
                .filter_by(
                    channel_account_id=self.channel_account.id,
                    remote_order_id=remote_order_id,
                    sku=sku,
                )
                .first()
            )
            if existing:
                continue

            db.session.add(
                Order(
                    account_id=self.channel_account.account_id,
                    channel_account_id=self.channel_account.id,
                    remote_order_id=remote_order_id,
                    sku=sku,
                    variant_id=variant.id if variant else None,
                    quantity=qty,
                    total=total,
                    currency=currency,
                    ordered_at=ordered_at,
                    raw_payload=line,
                )
            )
            if variant:
                variant.inventory_quantity = max(0, (variant.inventory_quantity or 0) - qty)

        db.session.commit()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
