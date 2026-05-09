"""Outbound sync: push a product to all (or one) connected channel(s)."""
from __future__ import annotations

from datetime import datetime

from ..channels import get_channel
from ..channels.base import ChannelError
from ..extensions import db
from ..models import ChannelAccount, ChannelLink, Product, SyncJob


def push_product(product_id: int, channel_account_id: int | None = None) -> list[SyncJob]:
    """Push a product to one specific channel, or to all channels of its account."""
    product = db.session.get(Product, product_id)
    if product is None:
        return []

    if channel_account_id is not None:
        accounts = [db.session.get(ChannelAccount, channel_account_id)]
        accounts = [a for a in accounts if a is not None]
    else:
        accounts = (
            db.session.query(ChannelAccount)
            .filter_by(account_id=product.account_id, status="active")
            .all()
        )

    jobs: list[SyncJob] = []
    for account in accounts:
        job = SyncJob(
            account_id=product.account_id,
            kind="push_product",
            product_id=product.id,
            channel_account_id=account.id,
            status="running",
            attempts=1,
        )
        db.session.add(job)
        db.session.flush()
        try:
            link = (
                db.session.query(ChannelLink)
                .filter_by(product_id=product.id, channel_account_id=account.id, variant_id=None)
                .first()
            )
            adapter = get_channel(account)
            adapter.push_product(product, link)
            product.last_pushed_at = datetime.utcnow()
            account.last_sync_at = datetime.utcnow()
            account.last_error = None
            job.status = "succeeded"
            job.finished_at = datetime.utcnow()
            db.session.commit()
        except ChannelError as exc:
            db.session.rollback()
            job = db.session.get(SyncJob, job.id)
            if job is not None:
                job.status = "failed"
                job.last_error = str(exc)
                job.finished_at = datetime.utcnow()
            account.last_error = str(exc)
            account.status = "error"
            db.session.commit()
        jobs.append(job)
    return jobs


def mirror_inventory(source_link: ChannelLink, sku: str, quantity: int) -> None:
    """After receiving an order on one channel, mirror the new stock count to
    the other channels carrying the same SKU.
    """
    other_links = (
        db.session.query(ChannelLink)
        .filter(
            ChannelLink.product_id == source_link.product_id,
            ChannelLink.id != source_link.id,
            ChannelLink.remote_sku == sku,
        )
        .all()
    )
    for link in other_links:
        account = link.channel_account
        if account is None or account.status != "active":
            continue
        job = SyncJob(
            account_id=account.account_id,
            kind="push_inventory",
            product_id=link.product_id,
            channel_account_id=account.id,
            status="running",
            attempts=1,
            payload={"sku": sku, "quantity": int(quantity)},
        )
        db.session.add(job)
        db.session.flush()
        try:
            get_channel(account).push_inventory(link, int(quantity))
            job.status = "succeeded"
            job.finished_at = datetime.utcnow()
        except ChannelError as exc:
            job.status = "failed"
            job.last_error = str(exc)
            job.finished_at = datetime.utcnow()
        db.session.commit()
