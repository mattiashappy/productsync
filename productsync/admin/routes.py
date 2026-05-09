"""Platform-admin area — for users with User.is_admin=True.

Read-only for v1: shows all accounts, all users, and platform-wide KPIs.
Destructive ops (delete account, edit user) come later when we can design
them safely with confirm flows + soft-delete.

Non-admins hitting /admin/* get a 404 — we'd rather not advertise the area's
existence to a logged-in non-admin probing for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import (
    Account, ChannelAccount, Order, Product, SyncJob, User, WebhookEvent,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    """Login-required AND is_admin. Non-admins get a 404, not a 403, so we
    don't leak the area's existence."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(404)
        return view(*args, **kwargs)
    return wrapper


@bp.route("/")
@bp.route("/dashboard")
@admin_required
def dashboard():
    # Core counts
    total_accounts = Account.query.count()
    accounts_by_type = dict(
        db.session.query(Account.account_type, func.count(Account.id))
        .group_by(Account.account_type).all()
    )
    total_users = User.query.count()
    total_admins = User.query.filter_by(is_admin=True).count()

    # Activity last 7 days
    since = datetime.utcnow() - timedelta(days=7)
    new_accounts_7d = Account.query.filter(Account.created_at >= since).count()
    new_users_7d = User.query.filter(User.created_at >= since).count()

    # Cross-account totals
    total_channels = ChannelAccount.query.count()
    total_products = Product.query.count()
    total_orders = Order.query.count()

    # Recent platform activity (most recent across ALL accounts — admin-eyes-only)
    recent_accounts = (
        Account.query.order_by(Account.created_at.desc()).limit(8).all()
    )
    recent_users = (
        User.query.order_by(User.created_at.desc()).limit(8).all()
    )
    recent_jobs = (
        SyncJob.query.order_by(SyncJob.created_at.desc()).limit(10).all()
    )

    return render_template(
        "admin/dashboard.html",
        total_accounts=total_accounts,
        accounts_by_type=accounts_by_type,
        total_users=total_users,
        total_admins=total_admins,
        new_accounts_7d=new_accounts_7d,
        new_users_7d=new_users_7d,
        total_channels=total_channels,
        total_products=total_products,
        total_orders=total_orders,
        recent_accounts=recent_accounts,
        recent_users=recent_users,
        recent_jobs=recent_jobs,
    )


@bp.route("/accounts")
@admin_required
def accounts():
    q = request.args.get("q", "").strip()
    account_type = request.args.get("type", "").strip()

    query = Account.query
    if q:
        query = query.filter(Account.name.ilike(f"%{q}%"))
    if account_type in ("store", "distributor"):
        query = query.filter(Account.account_type == account_type)

    rows = []
    for a in query.order_by(Account.created_at.desc()).limit(500).all():
        rows.append({
            "account": a,
            "user_count": User.query.filter_by(account_id=a.id).count(),
            "channel_count": ChannelAccount.query.filter_by(account_id=a.id).count(),
            "product_count": Product.query.filter_by(account_id=a.id).count(),
        })
    return render_template("admin/accounts.html", rows=rows, q=q, account_type=account_type)


@bp.route("/users")
@admin_required
def users():
    q = request.args.get("q", "").strip()
    only_admins = request.args.get("admins") == "1"

    query = User.query.join(Account, User.account_id == Account.id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.email.ilike(like), User.name.ilike(like), Account.name.ilike(like)))
    if only_admins:
        query = query.filter(User.is_admin == True)  # noqa: E712

    users_list = query.order_by(User.created_at.desc()).limit(500).all()
    return render_template("admin/users.html", users=users_list, q=q, only_admins=only_admins)
