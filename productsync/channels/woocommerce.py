"""WooCommerce REST API adapter.

Auth: HTTP Basic with consumer_key/consumer_secret.
Webhooks: WooCommerce signs the body with HMAC-SHA256 using the secret you set
when creating the webhook in WP-admin → WooCommerce → Settings → Advanced → Webhooks.
The signature comes in the `X-WC-Webhook-Signature` header, base64-encoded.
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
class WooCommerceChannel(Channel):
    name = "woocommerce"
    api_path = "/wp-json/wc/v3"

    def _client(self) -> httpx.Client:
        key = decrypt(self.channel_account.api_key_enc)
        secret = decrypt(self.channel_account.api_secret_enc)
        if not key or not secret:
            raise ChannelError("WooCommerce credentials missing or unreadable.")
        base = self.channel_account.base_url.rstrip("/") + self.api_path
        return httpx.Client(base_url=base, auth=(key, secret), timeout=30.0)

    # ── Push ────────────────────────────────────────────────────────────────
    def push_product(self, product: Product, link: ChannelLink | None) -> ChannelLink:
        body = self._serialize_product(product)
        with self._client() as client:
            if link and link.remote_product_id:
                resp = client.put(f"/products/{link.remote_product_id}", json=body)
            else:
                resp = client.post("/products", json=body)
            if resp.status_code >= 400:
                raise ChannelError(f"WC push failed [{resp.status_code}]: {resp.text[:300]}")
            data = resp.json()

        if link is None:
            link = ChannelLink(
                product_id=product.id,
                channel_account_id=self.channel_account.id,
            )
            db.session.add(link)
        link.remote_product_id = str(data.get("id"))
        link.remote_sku = data.get("sku") or product.sku
        link.last_pushed_at = datetime.utcnow()
        link.remote_version = str(data.get("date_modified_gmt") or data.get("date_modified") or "")
        return link

    def push_inventory(self, link: ChannelLink, quantity: int) -> None:
        if not link.remote_product_id:
            raise ChannelError("Cannot push inventory without remote_product_id.")
        with self._client() as client:
            resp = client.put(
                f"/products/{link.remote_product_id}",
                json={"manage_stock": True, "stock_quantity": int(quantity)},
            )
            if resp.status_code >= 400:
                raise ChannelError(f"WC inventory push failed [{resp.status_code}]: {resp.text[:300]}")

    # ── Connection test + setup helpers (used during onboarding) ────────────
    def verify_credentials(self) -> dict[str, Any]:
        """Ping WC to confirm credentials work. Returns dict with ok/error/total_products.
        Always logs the underlying exception so server-side debugging is possible.
        """
        from flask import current_app

        base_url = self.channel_account.base_url
        try:
            with self._client() as client:
                resp = client.get("/products", params={"per_page": 1})
        except httpx.HTTPError as exc:
            current_app.logger.warning(
                "WC verify_credentials() failed for %s: %s: %s",
                base_url, exc.__class__.__name__, exc,
            )
            return {"ok": False, "error": _friendly_connect_error(base_url, exc)}
        except ChannelError as exc:
            current_app.logger.warning("WC verify_credentials() ChannelError: %s", exc)
            return {"ok": False, "error": str(exc)}

        if resp.status_code == 401:
            return {"ok": False, "error": "Authentication failed. Double-check the consumer key and secret."}
        if resp.status_code == 404:
            return {"ok": False, "error": "Couldn't find the WooCommerce REST API at this URL. Make sure WooCommerce is installed and the base URL points at the WordPress root (no trailing /wp-admin or /wp-json)."}
        if resp.status_code >= 400:
            return {"ok": False, "error": f"WooCommerce returned {resp.status_code}: {resp.text[:200]}"}

        try:
            total = int(resp.headers.get("X-WP-Total", "0"))
        except ValueError:
            total = 0
        return {"ok": True, "total_products": total}

    def register_order_webhook(self, callback_url: str, secret: str) -> int | None:
        """Create the order.created webhook in WP-admin via the REST API.
        Returns the remote webhook id, or None if WC rejected the call.
        Idempotent: if a webhook with the same delivery URL exists, returns its id.
        """
        with self._client() as client:
            existing = client.get("/webhooks", params={"per_page": 100})
            if existing.status_code == 200:
                for hook in existing.json():
                    if hook.get("delivery_url") == callback_url and hook.get("topic") == "order.created":
                        return int(hook["id"])

            resp = client.post(
                "/webhooks",
                json={
                    "name": "ProductSync — orders",
                    "topic": "order.created",
                    "delivery_url": callback_url,
                    "secret": secret,
                    "status": "active",
                },
            )
            if resp.status_code >= 400:
                raise ChannelError(f"Couldn't register the order webhook automatically ({resp.status_code}): {resp.text[:200]}")
            return int(resp.json().get("id"))

    def pull_products_page(self, page: int = 1, per_page: int = 100) -> tuple[list[dict[str, Any]], int, int]:
        """Returns (products, total_pages, total_count). Use ChannelError on HTTP failure."""
        with self._client() as client:
            resp = client.get("/products", params={"per_page": per_page, "page": page})
            if resp.status_code >= 400:
                raise ChannelError(f"WC list-products failed [{resp.status_code}]: {resp.text[:200]}")
            try:
                total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
            except ValueError:
                total_pages = 1
            try:
                total_count = int(resp.headers.get("X-WP-Total", "0"))
            except ValueError:
                total_count = 0
            return resp.json(), total_pages, total_count

    def pull_variations(self, remote_product_id: str | int) -> list[dict[str, Any]]:
        """For variable products. Returns the list of variation dicts."""
        with self._client() as client:
            resp = client.get(f"/products/{remote_product_id}/variations", params={"per_page": 100})
            if resp.status_code >= 400:
                raise ChannelError(f"WC variations fetch failed [{resp.status_code}]: {resp.text[:200]}")
            return resp.json()

    # ── Webhook ─────────────────────────────────────────────────────────────
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> ChannelEvent:
        secret = decrypt(self.channel_account.webhook_secret_enc) or ""
        sig_header = headers.get("X-WC-Webhook-Signature", "")
        topic = headers.get("X-WC-Webhook-Topic", "unknown")

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
            return  # never mutate state on unverified events

        if event.topic == "order.created":
            self._record_order(event.payload)

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _serialize_product(self, product: Product) -> dict[str, Any]:
        variants = product.variants
        first = variants[0] if variants else None
        body: dict[str, Any] = {
            "name": product.title,
            "sku": product.sku,
            "description": product.description or "",
            "status": "publish" if product.status == "active" else "draft",
            "regular_price": str(product.price) if product.price is not None else "",
            "manage_stock": True,
            "images": [{"src": img.url, "alt": img.alt or ""} for img in product.images],
        }
        if product.compare_at_price is not None:
            body["sale_price"] = str(product.price)
            body["regular_price"] = str(product.compare_at_price)

        if len(variants) > 1:
            body["type"] = "variable"
            body["attributes"] = self._attributes(variants)
        else:
            body["type"] = "simple"
            if first is not None:
                body["stock_quantity"] = int(first.inventory_quantity or 0)
        return body

    @staticmethod
    def _attributes(variants: list[ProductVariant]) -> list[dict[str, Any]]:
        attrs: dict[str, set[str]] = {}
        for v in variants:
            for name_attr, val_attr in (
                (v.option1_name, v.option1_value),
                (v.option2_name, v.option2_value),
                (v.option3_name, v.option3_value),
            ):
                if name_attr and val_attr:
                    attrs.setdefault(name_attr, set()).add(val_attr)
        return [
            {"name": name, "options": sorted(opts), "visible": True, "variation": True}
            for name, opts in attrs.items()
        ]

    def _record_order(self, payload: dict[str, Any]) -> None:
        remote_order_id = str(payload.get("id") or "")
        if not remote_order_id:
            return
        currency = payload.get("currency") or "SEK"
        ordered_at_raw = payload.get("date_created_gmt") or payload.get("date_created")
        ordered_at = _parse_iso(ordered_at_raw) or datetime.utcnow()

        for line in payload.get("line_items", []):
            sku = (line.get("sku") or "").strip()
            if not sku:
                continue
            qty = int(line.get("quantity") or 1)
            total = _to_decimal(line.get("total"))
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


def _friendly_connect_error(base_url: str, exc: httpx.HTTPError) -> str:
    """Translate an httpx exception into actionable advice."""
    msg = str(exc) or exc.__class__.__name__
    cls = exc.__class__.__name__
    lower = msg.lower()

    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return (
            f"Your URL is missing a protocol — it should start with `https://` "
            f"(e.g. `https://your-shop.com`). You entered: `{base_url}`."
        )

    if isinstance(exc, httpx.ConnectError):
        if any(s in lower for s in ("name or service not known", "nodename nor servname",
                                     "getaddrinfo", "no address associated", "name resolution",
                                     "host is unreachable")):
            return (
                f"DNS lookup failed — we couldn't resolve `{base_url}`. "
                f"Check the spelling. Underlying error: {msg}"
            )
        if "connection refused" in lower:
            return (
                f"The server at `{base_url}` refused the connection. "
                f"Is the site reachable from the public internet on this URL?"
            )
        if "timed out" in lower or "timeout" in lower:
            return (
                f"The connection to `{base_url}` timed out. "
                f"The site may be slow, behind a firewall, or only reachable from inside a private network."
            )
        return (
            f"Couldn't open a TCP connection to `{base_url}`. "
            f"If the store is on your local network only, ProductSync (running here) needs to be on the same network or the URL needs to be publicly reachable. "
            f"Underlying error: {msg}"
        )

    if "ssl" in cls.lower() or "certificate" in lower:
        return (
            f"SSL/TLS error talking to `{base_url}`. The site may have an invalid or self-signed certificate. "
            f"Underlying error: {msg}"
        )

    return (
        f"Couldn't reach `{base_url}` ({cls}). "
        f"Underlying error: {msg}"
    )


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
