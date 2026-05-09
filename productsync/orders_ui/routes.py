from __future__ import annotations

from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from ..extensions import db
from ..models import ChannelAccount, Order

bp = Blueprint("orders_ui", __name__, url_prefix="/orders")


@bp.route("/")
@login_required
def index():
    aid = current_user.account_id
    sku = request.args.get("sku", "").strip()
    channel_filter = request.args.get("channel", "").strip()

    query = db.session.query(Order).filter_by(account_id=aid)
    if sku:
        query = query.filter(Order.sku.ilike(f"%{sku}%"))
    if channel_filter:
        query = query.filter(Order.channel_account_id == int(channel_filter))

    orders = query.order_by(Order.received_at.desc()).limit(200).all()
    channels = ChannelAccount.query.filter_by(account_id=aid).all()
    return render_template(
        "orders_ui/index.html",
        orders=orders,
        channels=channels,
        sku=sku,
        channel_filter=channel_filter,
    )
