from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    PRODUCT_STATUSES, ChannelAccount, ChannelLink, Product, ProductImage, ProductVariant,
)
from ..sync.push import push_product

bp = Blueprint("catalogue", __name__, url_prefix="/products")


def _decimal(value: str | None):
    if value in (None, "", "None"):
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _owned_product_or_404(product_id: int) -> Product:
    product = db.session.get(Product, product_id)
    if product is None or product.account_id != current_user.account_id:
        abort(404)
    return product


@bp.route("/")
@login_required
def index():
    aid = current_user.account_id
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = 10

    query = Product.query.filter_by(account_id=aid)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.title.ilike(like), Product.sku.ilike(like)))
    if status in PRODUCT_STATUSES:
        query = query.filter(Product.status == status)
    query = query.order_by(Product.updated_at.desc())

    pagination = db.paginate(query, page=page, per_page=per_page, error_out=False)

    # Toolbar KPIs (unfiltered totals)
    total_products = Product.query.filter_by(account_id=aid).count()
    total_inventory = (
        db.session.query(db.func.coalesce(db.func.sum(ProductVariant.inventory_quantity), 0))
        .join(Product, Product.id == ProductVariant.product_id)
        .filter(Product.account_id == aid)
        .scalar()
    ) or 0

    channels = ChannelAccount.query.filter_by(account_id=aid).all()
    return render_template(
        "catalogue/index.html",
        pagination=pagination,
        products=pagination.items,
        q=q,
        status=status,
        channels=channels,
        total_products=total_products,
        total_inventory=int(total_inventory),
        PRODUCT_STATUSES=PRODUCT_STATUSES,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        product = Product(
            account_id=current_user.account_id,
            sku=request.form["sku"].strip(),
            title=request.form["title"].strip(),
            description=request.form.get("description") or None,
            status=request.form.get("status", "draft"),
            price=_decimal(request.form.get("price")),
            currency=request.form.get("currency", "SEK"),
        )
        # one default variant for simple-product start
        product.variants.append(
            ProductVariant(
                sku=product.sku,
                inventory_quantity=_int(request.form.get("inventory_quantity"), 0),
                position=0,
            )
        )
        db.session.add(product)
        db.session.commit()
        flash("Product created.")
        return redirect(url_for("catalogue.detail", product_id=product.id))
    return render_template("catalogue/new.html", PRODUCT_STATUSES=PRODUCT_STATUSES)


@bp.route("/<int:product_id>")
@login_required
def detail(product_id):
    product = _owned_product_or_404(product_id)
    channels = ChannelAccount.query.filter_by(account_id=current_user.account_id).all()
    links_by_account = {l.channel_account_id: l for l in product.channel_links if l.variant_id is None}
    return render_template(
        "catalogue/detail.html",
        product=product,
        channels=channels,
        links_by_account=links_by_account,
        PRODUCT_STATUSES=PRODUCT_STATUSES,
    )


@bp.route("/<int:product_id>/edit", methods=["POST"])
@login_required
def edit(product_id):
    product = _owned_product_or_404(product_id)
    product.title = request.form["title"].strip()
    product.sku = request.form["sku"].strip()
    product.description = request.form.get("description") or None
    product.status = request.form.get("status", product.status)
    product.price = _decimal(request.form.get("price"))
    product.compare_at_price = _decimal(request.form.get("compare_at_price"))
    product.currency = request.form.get("currency", product.currency)
    db.session.commit()
    flash("Saved.")
    return redirect(url_for("catalogue.detail", product_id=product.id))


@bp.route("/<int:product_id>/delete", methods=["POST"])
@login_required
def delete(product_id):
    product = _owned_product_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    flash("Product deleted.")
    return redirect(url_for("catalogue.index"))


# ── Variants ────────────────────────────────────────────────────────────────
@bp.route("/<int:product_id>/variants", methods=["POST"])
@login_required
def add_variant(product_id):
    product = _owned_product_or_404(product_id)
    variant = ProductVariant(
        product_id=product.id,
        sku=request.form["sku"].strip(),
        option1_name=request.form.get("option1_name") or None,
        option1_value=request.form.get("option1_value") or None,
        option2_name=request.form.get("option2_name") or None,
        option2_value=request.form.get("option2_value") or None,
        option3_name=request.form.get("option3_name") or None,
        option3_value=request.form.get("option3_value") or None,
        price_override=_decimal(request.form.get("price_override")),
        inventory_quantity=_int(request.form.get("inventory_quantity"), 0),
        position=len(product.variants),
    )
    db.session.add(variant)
    db.session.commit()
    return redirect(url_for("catalogue.detail", product_id=product.id))


@bp.route("/variants/<int:variant_id>/delete", methods=["POST"])
@login_required
def delete_variant(variant_id):
    variant = db.session.get(ProductVariant, variant_id)
    if variant is None:
        abort(404)
    product = _owned_product_or_404(variant.product_id)
    if len(product.variants) <= 1:
        flash("A product must have at least one variant.")
        return redirect(url_for("catalogue.detail", product_id=product.id))
    db.session.delete(variant)
    db.session.commit()
    return redirect(url_for("catalogue.detail", product_id=product.id))


# ── Images (URL-based for MVP; a real uploader comes later) ────────────────
@bp.route("/<int:product_id>/images", methods=["POST"])
@login_required
def add_image(product_id):
    product = _owned_product_or_404(product_id)
    db.session.add(
        ProductImage(
            product_id=product.id,
            url=request.form["url"].strip(),
            alt=request.form.get("alt") or None,
            position=len(product.images),
        )
    )
    db.session.commit()
    return redirect(url_for("catalogue.detail", product_id=product.id))


@bp.route("/images/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_image(image_id):
    image = db.session.get(ProductImage, image_id)
    if image is None:
        abort(404)
    product = _owned_product_or_404(image.product_id)
    db.session.delete(image)
    db.session.commit()
    return redirect(url_for("catalogue.detail", product_id=product.id))


# ── Push to a channel ──────────────────────────────────────────────────────
@bp.route("/<int:product_id>/push/<int:channel_account_id>", methods=["POST"])
@login_required
def push(product_id, channel_account_id):
    product = _owned_product_or_404(product_id)
    account = db.session.get(ChannelAccount, channel_account_id)
    if account is None or account.account_id != current_user.account_id:
        abort(404)
    push_product(product.id, account.id)
    flash(f"Pushed to {account.display_name}.")
    return redirect(url_for("catalogue.detail", product_id=product.id))
