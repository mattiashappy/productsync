from flask import Blueprint, render_template
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import ChannelAccount, Order, Product, SyncJob, WebhookEvent

bp = Blueprint("sync_ui", __name__)


@bp.route("/dashboard")
@login_required
def dashboard():
    aid = current_user.account_id
    counts = {
        "total": Product.query.filter_by(account_id=aid).count(),
        "active": Product.query.filter_by(account_id=aid, status="active").count(),
        "draft": Product.query.filter_by(account_id=aid, status="draft").count(),
    }
    recent_orders = (
        db.session.query(Order)
        .filter_by(account_id=aid)
        .order_by(Order.received_at.desc())
        .limit(8).all()
    )
    channels = (
        db.session.query(ChannelAccount).filter_by(account_id=aid).all()
    )
    out_of_stock = (
        db.session.query(Product)
        .filter_by(account_id=aid)
        .all()
    )
    out_of_stock = [p for p in out_of_stock if p.total_inventory == 0][:8]
    return render_template(
        "sync_ui/dashboard.html",
        counts=counts,
        recent_orders=recent_orders,
        channels=channels,
        out_of_stock=out_of_stock,
    )


@bp.route("/sync-log")
@login_required
def log():
    aid = current_user.account_id
    jobs = (
        db.session.query(SyncJob)
        .filter_by(account_id=aid)
        .order_by(SyncJob.created_at.desc())
        .limit(50).all()
    )
    events = (
        db.session.query(WebhookEvent)
        .join(ChannelAccount, WebhookEvent.channel_account_id == ChannelAccount.id)
        .filter(ChannelAccount.account_id == aid)
        .order_by(WebhookEvent.received_at.desc())
        .limit(50).all()
    )
    return render_template("sync_ui/log.html", jobs=jobs, events=events)
