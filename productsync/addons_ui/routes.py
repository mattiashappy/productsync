"""Addons (integrations) UI.

Internally the architecture keeps the 'channel' naming (ChannelAccount model,
Channel ABC). Externally, users see 'Addons' — a marketplace of integrations
they can install. Each addon can have multiple stores connected.
"""
from __future__ import annotations

import secrets

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..channels import get_channel
from ..channels.base import ChannelError
from ..channels.woocommerce import WooCommerceChannel
from ..crypto import encrypt
from ..extensions import db
from ..models import ChannelAccount, SyncJob

bp = Blueprint("addons_ui", __name__, url_prefix="/addons")


# Addon catalogue. Each entry describes one available integration.
# `status='available'` = connectable now; `status='coming_soon'` = visible but disabled.
ADDONS: dict[str, dict] = {
    "woocommerce": {
        "slug": "woocommerce",
        "channel": "woocommerce",  # matches ChannelAccount.channel
        "name": "WooCommerce",
        "category": "E-commerce",
        "tagline": "Sync products with WordPress + WooCommerce.",
        "description": (
            "Push titles, prices, variants, and images to WooCommerce via the REST API. "
            "Pull paid orders and inventory back via webhooks."
        ),
        "docs_url": "https://woocommerce.com/document/woocommerce-rest-api/",
        "credential_help": (
            "Paste your store's URL and the WooCommerce REST API consumer key/secret. "
            "ProductSync verifies them on save and registers the order webhook for you."
        ),
        "fields": ["base_url", "api_key", "api_secret"],
        "status": "available",
        # Logo path under static/. If None, the templates fall back to the
        # icon_letter chip (used by addons that ship without a brand asset).
        "logo": "img/woo-logo.svg",
        "icon_letter": "W",
        "color_bg": "bg-purple-100",
        "color_text": "text-purple-700",
    },
    "shopify": {
        "slug": "shopify",
        "channel": "shopify",
        "name": "Shopify",
        "category": "E-commerce",
        "tagline": "Sync products with your Shopify store.",
        "description": (
            "Push titles, prices, variants, and images to Shopify via the Admin REST API. "
            "Pull paid orders and inventory decrements back via webhooks."
        ),
        "docs_url": "https://shopify.dev/docs/api/admin-rest",
        "credential_help": (
            "In Shopify admin → Settings → Apps and sales channels → Develop apps. "
            "Create a custom app, paste its Admin API access token here."
        ),
        "fields": ["base_url", "api_key", "webhook_secret"],
        "status": "available",
        # Compact glyph (shopping-bag) for catalogue tiles; templates upgrade
        # to the full wordmark on the detail page header.
        "logo": "img/shopify-glyph.svg",
        "logo_wordmark": "img/shopify-logo.svg",
        "icon_letter": "S",
        "color_bg": "bg-emerald-100",
        "color_text": "text-emerald-700",
    },
    "webflow": {
        "slug": "webflow",
        "channel": "webflow",
        "name": "Webflow",
        "category": "E-commerce",
        "tagline": "Sync products with your Webflow Ecommerce store.",
        "description": (
            "Coming soon. Push titles, prices, variants, and images to Webflow "
            "Ecommerce via the Designer API; pull paid orders back."
        ),
        "docs_url": "https://developers.webflow.com/",
        "credential_help": "",
        "fields": [],
        "status": "coming_soon",
        "logo": None,  # falls back to icon_letter chip
        "icon_letter": "W",
        "color_bg": "bg-indigo-100",
        "color_text": "text-indigo-700",
    },
}


def _addon_or_404(slug: str) -> dict:
    addon = ADDONS.get(slug)
    if addon is None:
        abort(404)
    return addon


def _connection_counts(account_id: int) -> dict[str, int]:
    rows = (
        db.session.query(ChannelAccount.channel, func.count(ChannelAccount.id))
        .filter_by(account_id=account_id)
        .group_by(ChannelAccount.channel)
        .all()
    )
    return {channel: count for channel, count in rows}


# ── Catalogue ───────────────────────────────────────────────────────────────
@bp.route("/")
@login_required
def index():
    counts = _connection_counts(current_user.account_id)
    addons = []
    for addon in ADDONS.values():
        addons.append({**addon, "connected_count": counts.get(addon["channel"], 0)})
    return render_template("addons_ui/index.html", addons=addons)


# ── Per-addon detail page ───────────────────────────────────────────────────
@bp.route("/<slug>")
@login_required
def detail(slug: str):
    addon = _addon_or_404(slug)
    stores = (
        ChannelAccount.query
        .filter_by(account_id=current_user.account_id, channel=addon["channel"])
        .order_by(ChannelAccount.created_at.desc())
        .all()
    )
    return render_template(
        "addons_ui/detail.html",
        addon=addon,
        stores=stores,
        public_base_url=current_app.config.get("PUBLIC_BASE_URL", ""),
    )


@bp.route("/<slug>/stores", methods=["POST"])
@login_required
def connect_store(slug: str):
    addon = _addon_or_404(slug)
    if addon["status"] != "available":
        flash("This addon is not yet available.", "error")
        return redirect(url_for("addons_ui.detail", slug=slug))

    # Auto-generate a webhook secret if the user didn't supply one — needed for
    # signature verification, and we'll register the webhook for them too.
    webhook_secret = request.form.get("webhook_secret") or secrets.token_urlsafe(32)

    store = ChannelAccount(
        account_id=current_user.account_id,
        channel=addon["channel"],
        display_name=request.form["display_name"].strip(),
        base_url=request.form["base_url"].strip().rstrip("/"),
        api_key_enc=encrypt(request.form.get("api_key") or None),
        api_secret_enc=encrypt(request.form.get("api_secret") or None),
        webhook_secret_enc=encrypt(webhook_secret),
    )

    # Step 1: verify credentials BEFORE saving — clear immediate feedback
    # for typos, wrong URL, etc.
    adapter = get_channel(store)  # works on the in-memory store; no id needed
    if isinstance(adapter, WooCommerceChannel):
        # WooCommerceChannel needs the encrypted creds to roundtrip via decrypt(); flush so encrypt() is invoked properly
        check = adapter.verify_credentials()
        if not check["ok"]:
            flash(f"Couldn't connect: {check['error']}", "error")
            return redirect(url_for("addons_ui.detail", slug=slug))

        db.session.add(store)
        db.session.commit()  # need store.id for the webhook callback URL

        # Step 2: register the webhook automatically.
        callback = (
            current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
            + url_for("webhooks.receive", channel=store.channel, account_id=store.id)
        )
        webhook_msg = ""
        try:
            adapter.register_order_webhook(callback, webhook_secret)
            webhook_msg = " Order webhook registered automatically."
        except ChannelError as exc:
            webhook_msg = (
                f" Couldn't auto-register the order webhook ({exc}). "
                f"You can add it manually in WP-admin → WooCommerce → Settings → Advanced → Webhooks."
            )

        flash(
            f"Connected {store.display_name}. Found {check['total_products']} product(s) in the store.{webhook_msg}",
            "info",
        )
    else:
        # Non-Woo addon (Shopify): just save without verify (verify can be added later)
        db.session.add(store)
        db.session.commit()
        flash(f"Connected {store.display_name}.")

    return redirect(url_for("addons_ui.detail", slug=slug))


@bp.route("/stores/<int:store_id>/import", methods=["POST"])
@login_required
def import_store(store_id: int):
    """Kick off a background WooCommerce import. Returns immediately so the UI
    can poll for progress; doesn't block on the actual import work.
    """
    store = db.session.get(ChannelAccount, store_id)
    if store is None or store.account_id != current_user.account_id:
        abort(404)
    if store.channel != "woocommerce":
        flash("Import is currently only supported for WooCommerce.", "error")
        return redirect(url_for("addons_ui.detail", slug=store.channel))

    # Concurrency guard — refuse if an import is already running for this store.
    running = (
        SyncJob.query
        .filter_by(channel_account_id=store.id, kind="import_woo", status="running")
        .first()
    )
    if running is not None:
        flash("An import is already running for this store. Watch the progress below.", "info")
        return redirect(url_for("addons_ui.detail", slug=store.channel))

    job = SyncJob(
        account_id=current_user.account_id,
        kind="import_woo",
        channel_account_id=store.id,
        status="running",
        attempts=1,
        payload={"processed": 0, "total": None,
                 "message": "Starting…", "errors": [], "finished": False},
    )
    db.session.add(job)
    db.session.commit()

    from ..sync.import_woo import start_import_in_background
    start_import_in_background(current_app._get_current_object(), store.id, job.id)

    flash("Import started — watch progress below.", "info")
    return redirect(url_for("addons_ui.detail", slug=store.channel))


@bp.route("/stores/<int:store_id>/import/progress")
@login_required
def import_progress(store_id: int):
    """HTML fragment that HTMX polls every couple of seconds while the import runs."""
    store = db.session.get(ChannelAccount, store_id)
    if store is None or store.account_id != current_user.account_id:
        abort(404)
    job = (
        SyncJob.query
        .filter_by(channel_account_id=store.id, kind="import_woo")
        .order_by(SyncJob.created_at.desc())
        .first()
    )
    return render_template("addons_ui/_import_progress.html", store=store, job=job)


@bp.route("/stores/<int:store_id>/delete", methods=["POST"])
@login_required
def delete_store(store_id: int):
    store = db.session.get(ChannelAccount, store_id)
    if store is None or store.account_id != current_user.account_id:
        abort(404)
    slug = store.channel
    db.session.delete(store)
    db.session.commit()
    flash("Store removed.")
    return redirect(url_for("addons_ui.detail", slug=slug))


@bp.route("/stores/<int:store_id>/toggle", methods=["POST"])
@login_required
def toggle_store(store_id: int):
    store = db.session.get(ChannelAccount, store_id)
    if store is None or store.account_id != current_user.account_id:
        abort(404)
    store.status = "disabled" if store.status == "active" else "active"
    db.session.commit()
    return redirect(url_for("addons_ui.detail", slug=store.channel))
