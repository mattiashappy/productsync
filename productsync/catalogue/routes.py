from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    LOW_STOCK_THRESHOLD, PRODUCT_STATUSES, PRODUCT_TYPES, STOCK_STATUSES,
    Brand, Category, ChannelAccount, ChannelLink, Product, ProductImage,
    ProductVariant, product_brand, product_category,
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

    # ── Filter inputs ──────────────────────────────────────────────────────
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    category_id = _int(request.args.get("category", ""), default=0) or None
    brand_ids = _csv_ints(request.args.get("brand", ""))
    ptype = request.args.get("type", "").strip()
    stock = request.args.get("stock", "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = 10

    query = Product.query.filter_by(account_id=aid)

    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.title.ilike(like), Product.sku.ilike(like)))
    if status in PRODUCT_STATUSES:
        query = query.filter(Product.status == status)

    # Category filter — hierarchical: include selected category AND all descendants
    if category_id:
        descendant_ids = _walk_descendants(aid, category_id)
        if descendant_ids:
            query = (query.join(product_category, Product.id == product_category.c.product_id)
                          .filter(product_category.c.category_id.in_(descendant_ids)))

    # Brand filter — OR semantics across the selected brand ids
    if brand_ids:
        query = (query.join(product_brand, Product.id == product_brand.c.product_id)
                      .filter(product_brand.c.brand_id.in_(brand_ids)))

    # Product type — derived from variant count, expressed as HAVING clause
    if ptype in PRODUCT_TYPES:
        # subquery: variant count per product
        vc = (db.session.query(
                ProductVariant.product_id.label("pid"),
                db.func.count(ProductVariant.id).label("cnt"))
              .group_by(ProductVariant.product_id).subquery())
        if ptype == "simple":
            query = (query.outerjoin(vc, vc.c.pid == Product.id)
                          .filter(db.or_(vc.c.cnt == None, vc.c.cnt <= 1)))  # noqa: E711
        else:  # variable
            query = query.join(vc, vc.c.pid == Product.id).filter(vc.c.cnt > 1)

    # Stock status — derived from SUM(inventory_quantity) per product
    if stock in STOCK_STATUSES:
        inv = (db.session.query(
                ProductVariant.product_id.label("pid"),
                db.func.coalesce(db.func.sum(ProductVariant.inventory_quantity), 0).label("total"))
               .group_by(ProductVariant.product_id).subquery())
        query = query.outerjoin(inv, inv.c.pid == Product.id)
        if stock == "out_of_stock":
            query = query.filter(db.or_(inv.c.total == None, inv.c.total <= 0))  # noqa: E711
        elif stock == "low_stock":
            query = query.filter(inv.c.total > 0, inv.c.total <= LOW_STOCK_THRESHOLD)
        elif stock == "in_stock":
            query = query.filter(inv.c.total > LOW_STOCK_THRESHOLD)

    # If we joined m2m tables, multiple matches per product can duplicate rows
    query = query.distinct().order_by(Product.updated_at.desc())

    pagination = db.paginate(query, page=page, per_page=per_page, error_out=False)

    # Toolbar KPIs (always unfiltered)
    total_products = Product.query.filter_by(account_id=aid).count()
    total_inventory = (
        db.session.query(db.func.coalesce(db.func.sum(ProductVariant.inventory_quantity), 0))
        .join(Product, Product.id == ProductVariant.product_id)
        .filter(Product.account_id == aid)
        .scalar()
    ) or 0

    channels = ChannelAccount.query.filter_by(account_id=aid).all()

    # Filter UI: dropdown options + currently-selected display values
    categories_tree = _build_category_tree(aid)
    all_brands = Brand.query.filter_by(account_id=aid).order_by(Brand.name).all()
    selected_category = (
        Category.query.filter_by(id=category_id, account_id=aid).first()
        if category_id else None
    )
    selected_brands = (
        Brand.query.filter(Brand.account_id == aid, Brand.id.in_(brand_ids)).all()
        if brand_ids else []
    )

    return render_template(
        "catalogue/index.html",
        pagination=pagination,
        products=pagination.items,
        q=q, status=status,
        category_id=category_id, brand_ids=brand_ids, ptype=ptype, stock=stock,
        categories_tree=categories_tree, all_brands=all_brands,
        selected_category=selected_category, selected_brands=selected_brands,
        channels=channels,
        total_products=total_products,
        total_inventory=int(total_inventory),
        PRODUCT_STATUSES=PRODUCT_STATUSES,
        PRODUCT_TYPES=PRODUCT_TYPES,
        STOCK_STATUSES=STOCK_STATUSES,
    )


def _csv_ints(value: str) -> list[int]:
    out: list[int] = []
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            pass
    return out


def _walk_descendants(account_id: int, root_id: int) -> list[int]:
    """BFS over Category.parent_id to collect root + all descendant ids.
    Cheap on typical 3–4-level hierarchies; revisit with a recursive CTE if
    anyone has a 1000-deep tree.
    """
    rows = (db.session.query(Category.id, Category.parent_id)
            .filter_by(account_id=account_id).all())
    children_by_parent: dict[int, list[int]] = {}
    for cid, pid in rows:
        if pid is not None:
            children_by_parent.setdefault(pid, []).append(cid)
    out: list[int] = []
    queue = [root_id]
    seen: set[int] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        queue.extend(children_by_parent.get(cur, []))
    return out


def _build_category_tree(account_id: int) -> list[dict]:
    """Flatten the category tree into [(category, depth)] for indented
    rendering in a <select>. Depth-first, alphabetical within each level."""
    rows = (Category.query.filter_by(account_id=account_id)
            .order_by(Category.name).all())
    by_parent: dict[int | None, list[Category]] = {}
    for c in rows:
        by_parent.setdefault(c.parent_id, []).append(c)

    flat: list[dict] = []
    def walk(parent_id, depth):
        for c in by_parent.get(parent_id, []):
            flat.append({"category": c, "depth": depth})
            walk(c.id, depth + 1)
    walk(None, 0)
    return flat


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
    # Basics
    product.title = request.form["title"].strip()
    product.sku = request.form["sku"].strip()
    product.description = request.form.get("description") or None
    product.status = request.form.get("status", product.status)
    product.price = _decimal(request.form.get("price"))
    product.compare_at_price = _decimal(request.form.get("compare_at_price"))
    product.currency = request.form.get("currency", product.currency)
    # Marketing
    product.vendor = (request.form.get("vendor") or "").strip() or None
    product.slug = (request.form.get("slug") or "").strip() or None
    product.short_description = request.form.get("short_description") or None
    product.featured = bool(request.form.get("featured"))
    # Sale
    product.sale_price = _decimal(request.form.get("sale_price"))
    product.sale_starts_at = _datetime(request.form.get("sale_starts_at"))
    product.sale_ends_at = _datetime(request.form.get("sale_ends_at"))
    # Shipping (dimensions stored as mm; weight as g)
    product.weight_grams = _int(request.form.get("weight_grams"), default=None)
    product.length_mm = _int(request.form.get("length_mm"), default=None)
    product.width_mm = _int(request.form.get("width_mm"), default=None)
    product.height_mm = _int(request.form.get("height_mm"), default=None)
    db.session.commit()
    flash("Saved.")
    return redirect(url_for("catalogue.detail", product_id=product.id))


def _datetime(value: str | None):
    """Parse an HTML <input type='datetime-local'> value into a datetime."""
    from datetime import datetime as _dt
    if not value:
        return None
    try:
        return _dt.fromisoformat(value)
    except ValueError:
        return None


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
