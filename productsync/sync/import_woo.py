"""Import existing products from a connected WooCommerce store into ProductSync.

Match key: SKU. If a local product with the same SKU exists, link it (don't duplicate).
Otherwise create a new local Product with variants and images.

Two ways to call:
  - import_woo_store(store_id)              — synchronous, no progress tracking
  - run_import_with_job(store_id, job_id)   — updates SyncJob.payload as it runs
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..channels import get_channel
from ..channels.base import ChannelError
from ..channels.woocommerce import WooCommerceChannel
from ..extensions import db
from ..models import (
    ChannelAccount, ChannelLink, Product, ProductImage, ProductVariant, SyncJob,
)


@dataclass
class ImportResult:
    created: int = 0
    linked_existing: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.created + self.linked_existing + self.skipped + self.failed

    def as_summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"{self.created} new")
        if self.linked_existing:
            parts.append(f"{self.linked_existing} linked to existing")
        if self.skipped:
            parts.append(f"{self.skipped} skipped (no SKU)")
        if self.failed:
            parts.append(f"{self.failed} failed")
        return ", ".join(parts) or "Nothing to import."


def import_woo_store(channel_account_id: int, max_pages: int = 100) -> ImportResult:
    """Synchronous import. Used by tests / CLI; the UI uses run_import_with_job."""
    return _run(channel_account_id, max_pages, job=None)[0]


def run_import_with_job(channel_account_id: int, job_id: int, max_pages: int = 100) -> None:
    """Run the import while updating SyncJob.payload as a progress beacon.
    Called by the background thread spawned from the addons_ui route.
    """
    from flask import current_app
    job = db.session.get(SyncJob, job_id)
    if job is None:
        return
    try:
        result, _processed = _run(channel_account_id, max_pages, job=job)
        job.status = "succeeded" if not result.failed else "failed"
        job.payload = _payload(
            processed=result.total_processed,
            total=result.total_processed,
            message=f"Done. {result.as_summary()}",
            errors=result.errors[:10],
            finished=True,
        )
    except ChannelError as exc:
        current_app.logger.warning("WC import failed: %s", exc)
        job.status = "failed"
        job.last_error = str(exc)
        job.payload = _payload(
            processed=(job.payload or {}).get("processed", 0),
            total=(job.payload or {}).get("total"),
            message=f"Import failed: {exc}",
            errors=[],
            finished=True,
        )
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Unexpected error during WC import")
        job.status = "failed"
        job.last_error = str(exc)
        job.payload = _payload(
            processed=(job.payload or {}).get("processed", 0),
            total=(job.payload or {}).get("total"),
            message=f"Import crashed: {exc}",
            errors=[],
            finished=True,
        )
    finally:
        job.finished_at = datetime.utcnow()
        db.session.commit()


def start_import_in_background(app, channel_account_id: int, job_id: int) -> threading.Thread:
    """Spawn a daemon thread running the import inside an app context."""
    def runner():
        with app.app_context():
            run_import_with_job(channel_account_id, job_id)

    t = threading.Thread(target=runner, name=f"woo-import-store-{channel_account_id}", daemon=True)
    t.start()
    return t


# ── Internal: the actual import loop ────────────────────────────────────────
def _run(channel_account_id: int, max_pages: int, job: SyncJob | None) -> tuple[ImportResult, int]:
    account = db.session.get(ChannelAccount, channel_account_id)
    if account is None:
        raise ChannelError("Connected store not found.")

    adapter = get_channel(account)
    if not isinstance(adapter, WooCommerceChannel):
        raise ChannelError("This importer only supports WooCommerce stores.")

    result = ImportResult()
    page = 1

    # First call: fetch page 1 + the exact total from WC's X-WP-Total header.
    products, total_pages, total_count = adapter.pull_products_page(page=1, per_page=100)
    # Fall back to a page-based estimate if the header was missing for some reason.
    estimated_total = total_count or (total_pages * 100)
    if job is not None:
        job.payload = _payload(
            processed=0,
            total=estimated_total,
            message=f"Found {estimated_total} product(s) across {total_pages} page(s). Importing page 1…",
            errors=[],
            finished=False,
        )
        db.session.commit()

    while products:
        for wc_product in products:
            try:
                _import_one(account, adapter, wc_product, result)
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                result.failed += 1
                result.errors.append(
                    f"#{wc_product.get('id', '?')} {wc_product.get('name', '')[:40]}: {exc}"
                )

            # Update progress every 10 items so the DB doesn't get hammered.
            if job is not None and result.total_processed % 10 == 0:
                _save_progress(job, result, estimated_total, page, total_pages)

        db.session.commit()
        if job is not None:
            _save_progress(job, result, estimated_total, page, total_pages)

        if page >= total_pages or page >= max_pages:
            break
        page += 1
        if job is not None:
            job.payload = _payload(
                processed=result.total_processed,
                total=estimated_total,
                message=f"Importing page {page} of {total_pages}…",
                errors=result.errors[-5:],
                finished=False,
            )
            db.session.commit()
        products, _, _ = adapter.pull_products_page(page=page, per_page=100)

    account.last_sync_at = datetime.utcnow()
    db.session.commit()
    return result, result.total_processed


def _save_progress(job: SyncJob, result: ImportResult, estimated_total: int,
                   page: int, total_pages: int) -> None:
    job.payload = _payload(
        processed=result.total_processed,
        total=estimated_total,
        message=f"Importing page {page} of {total_pages} ({result.created} new, {result.linked_existing} linked)",
        errors=result.errors[-5:],
        finished=False,
    )
    db.session.commit()


def _payload(*, processed: int, total: int | None, message: str,
             errors: list[str], finished: bool) -> dict[str, Any]:
    return {
        "processed": processed,
        "total": total,
        "message": message,
        "errors": errors,
        "finished": finished,
    }


def _import_one(
    account: ChannelAccount,
    adapter: WooCommerceChannel,
    wc_product: dict[str, Any],
    result: ImportResult,
) -> None:
    remote_id = str(wc_product.get("id"))
    title = wc_product.get("name") or "(untitled)"
    product_type = wc_product.get("type") or "simple"

    sku = _resolve_product_sku(wc_product, remote_id, product_type)
    if not sku:
        # Simple product with neither SKU nor GTIN — there's nothing to match orders against.
        result.skipped += 1
        result.errors.append(
            f"#{remote_id} {title[:40]}: skipped (simple product with no SKU or GTIN)"
        )
        return

    # Already linked? Update remote_version timestamp and continue.
    existing_link = (
        db.session.query(ChannelLink)
        .filter_by(
            channel_account_id=account.id,
            remote_product_id=remote_id,
            variant_id=None,
        )
        .first()
    )
    if existing_link is not None:
        existing_link.last_pulled_at = datetime.utcnow()
        existing_link.remote_version = str(wc_product.get("date_modified_gmt") or "")
        result.linked_existing += 1
        return

    # Match against an existing local product by SKU.
    product = (
        db.session.query(Product)
        .filter_by(account_id=account.account_id, sku=sku)
        .first()
    )
    is_new = product is None
    if is_new:
        product = Product(
            account_id=account.account_id,
            sku=sku,
            title=title,
            description=wc_product.get("description") or wc_product.get("short_description") or None,
            status="active" if wc_product.get("status") == "publish" else "draft",
            price=_dec(wc_product.get("regular_price") or wc_product.get("price")),
            compare_at_price=_dec(wc_product.get("regular_price") if wc_product.get("sale_price") else None),
            currency="SEK",
            requires_shipping=not wc_product.get("virtual", False),
        )
        db.session.add(product)
        db.session.flush()

        for pos, img in enumerate(wc_product.get("images") or []):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            db.session.add(
                ProductImage(
                    product_id=product.id,
                    url=src,
                    alt=img.get("alt") or None,
                    position=pos,
                )
            )

        if wc_product.get("type") == "variable":
            variations = adapter.pull_variations(remote_id)
            _create_variants_from_variations(product, wc_product, variations)
        else:
            db.session.add(
                ProductVariant(
                    product_id=product.id,
                    sku=sku,
                    inventory_quantity=int(wc_product.get("stock_quantity") or 0),
                    position=0,
                )
            )
        result.created += 1
    else:
        result.linked_existing += 1

    db.session.add(
        ChannelLink(
            product_id=product.id,
            channel_account_id=account.id,
            remote_product_id=remote_id,
            remote_sku=sku,
            last_pulled_at=datetime.utcnow(),
            remote_version=str(wc_product.get("date_modified_gmt") or ""),
        )
    )


def _create_variants_from_variations(
    product: Product, wc_product: dict[str, Any], variations: list[dict[str, Any]]
) -> None:
    for pos, var in enumerate(variations):
        # Variation matching key: SKU first, then WC 9.0 native GTIN/EAN field
        # (`global_unique_id`), then a synthetic fallback so the row isn't lost.
        v_sku = (
            (var.get("sku") or "").strip()
            or (var.get("global_unique_id") or "").strip()
            or f"{product.sku}-V{var.get('id', pos)}"
        )
        opts: list[tuple[str, str]] = []
        for a in var.get("attributes", []):
            opts.append((a.get("name") or "", a.get("option") or ""))
        while len(opts) < 3:
            opts.append(("", ""))

        db.session.add(
            ProductVariant(
                product_id=product.id,
                sku=v_sku,
                option1_name=opts[0][0] or None, option1_value=opts[0][1] or None,
                option2_name=opts[1][0] or None, option2_value=opts[1][1] or None,
                option3_name=opts[2][0] or None, option3_value=opts[2][1] or None,
                price_override=_dec(var.get("regular_price") or var.get("price")),
                inventory_quantity=int(var.get("stock_quantity") or 0),
                position=pos,
            )
        )


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _resolve_product_sku(wc_product: dict[str, Any], remote_id: str, product_type: str) -> str | None:
    """Pick the canonical local SKU for a Woo product.

    Order of preference:
      1. The product's own SKU field
      2. WC 9.0+ native GTIN field (`global_unique_id`)
      3. For VARIABLE products only — the WC slug or `wc-{id}` synthetic.
         The parent SKU is internal; orders always reference the variation SKUs,
         so a synthetic parent SKU is harmless.
      4. Otherwise None — the simple product can't be tracked across channels.
    """
    sku = (wc_product.get("sku") or "").strip()
    if sku:
        return sku
    gtin = (wc_product.get("global_unique_id") or "").strip()
    if gtin:
        return gtin
    if product_type == "variable":
        slug = (wc_product.get("slug") or "").strip()
        return slug or f"wc-{remote_id}"
    return None
