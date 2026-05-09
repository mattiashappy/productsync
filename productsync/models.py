"""SQLAlchemy models. JSONB on Postgres, falls back to JSON on SQLite."""
from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

from .extensions import db

# Use JSONB on Postgres, JSON on SQLite.
JsonType = JSON().with_variant(JSONB(), "postgresql")


PRODUCT_STATUSES = ["draft", "active", "archived"]
PRODUCT_TYPES = ["simple", "variable"]              # derived from variant count, not stored
STOCK_STATUSES = ["in_stock", "low_stock", "out_of_stock"]
LOW_STOCK_THRESHOLD = 10                            # ≤10 = "low stock"; configurable per-account next iteration
CHANNEL_KINDS = ["woocommerce", "shopify"]
SYNC_JOB_KINDS = ["push_product", "push_inventory", "pull_inventory", "webhook", "import_woo"]
SYNC_JOB_STATUSES = ["queued", "running", "succeeded", "failed"]
ACCOUNT_TYPES = ["store", "distributor"]


# ── Many-to-many association tables ─────────────────────────────────────────
product_category = db.Table(
    "product_category",
    db.Column("product_id", db.Integer, db.ForeignKey("product.id"), primary_key=True),
    db.Column("category_id", db.Integer, db.ForeignKey("category.id"), primary_key=True),
)

product_brand = db.Table(
    "product_brand",
    db.Column("product_id", db.Integer, db.ForeignKey("product.id"), primary_key=True),
    db.Column("brand_id", db.Integer, db.ForeignKey("brand.id"), primary_key=True),
)


class Account(db.Model):
    __tablename__ = "account"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    # 'store'       — connects to WC/Shopify, manages products, syncs to channels (default)
    # 'distributor' — publishes catalogues that subscribed stores pull from
    account_type = db.Column(db.String(20), nullable=False, default="store")

    # Profile fields collected during signup. All optional — kept on Account
    # rather than User because they describe the business, not the person.
    industry = db.Column(db.String(80))            # what they sell or distribute
    website = db.Column(db.String(255))            # store URL (store) or brand site (distributor)
    country = db.Column(db.String(80))             # free-text country / region
    # Store-only — comma-separated list of platform slugs the user already runs
    # ("woocommerce", "shopify", "both", "none"). Used later to tailor onboarding.
    current_platforms = db.Column(db.String(120))
    # Distributor-only — rough range string ("1-10", "11-50", ...). Helps decide
    # whether to push catalogue imports to a background worker from day one.
    product_count_estimate = db.Column(db.String(20))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def is_distributor(self) -> bool:
        return self.account_type == "distributor"


class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # Platform-operator role. Orthogonal to account_type — an admin is a User
    # who can access the /admin/* area; what kind of account they belong to
    # (store/distributor) is independent. Granted via DB only, never via UI.
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    account = db.relationship("Account", lazy="joined")


class Product(db.Model):
    __tablename__ = "product"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    sku = db.Column(db.String(120), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="draft")
    price = db.Column(db.Numeric(10, 2))
    compare_at_price = db.Column(db.Numeric(10, 2))
    currency = db.Column(db.String(3), nullable=False, default="SEK")
    weight_grams = db.Column(db.Integer)
    requires_shipping = db.Column(db.Boolean, nullable=False, default=True)
    tax_class = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_pushed_at = db.Column(db.DateTime)

    variants = db.relationship(
        "ProductVariant", backref="product", cascade="all, delete-orphan",
        order_by="ProductVariant.position", lazy="selectin",
    )
    images = db.relationship(
        "ProductImage", backref="product", cascade="all, delete-orphan",
        order_by="ProductImage.position", lazy="selectin",
    )
    channel_links = db.relationship(
        "ChannelLink", backref="product", cascade="all, delete-orphan", lazy="selectin",
    )
    # Taxonomy links — many-to-many, populated by the importer + (later) manual editor
    categories = db.relationship(
        "Category", secondary=product_category,
        backref=db.backref("products", lazy="dynamic"), lazy="selectin",
    )
    brands = db.relationship(
        "Brand", secondary=product_brand,
        backref=db.backref("products", lazy="dynamic"), lazy="selectin",
    )

    __table_args__ = (
        db.UniqueConstraint("account_id", "sku", name="uq_product_account_sku"),
    )

    @property
    def default_variant(self) -> "ProductVariant | None":
        return self.variants[0] if self.variants else None

    @property
    def total_inventory(self) -> int:
        return sum((v.inventory_quantity or 0) for v in self.variants)

    @property
    def product_type(self) -> str:
        """Derived: simple = exactly one variant, variable = two or more."""
        return "variable" if len(self.variants) > 1 else "simple"

    @property
    def stock_status(self) -> str:
        """Derived from total_inventory and the LOW_STOCK_THRESHOLD constant."""
        total = self.total_inventory
        if total <= 0:
            return "out_of_stock"
        if total <= LOW_STOCK_THRESHOLD:
            return "low_stock"
        return "in_stock"


class ProductVariant(db.Model):
    __tablename__ = "product_variant"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    sku = db.Column(db.String(120), nullable=False)
    title = db.Column(db.String(120))  # e.g. "Red / M"; null for the default single variant
    option1_name = db.Column(db.String(60))
    option1_value = db.Column(db.String(60))
    option2_name = db.Column(db.String(60))
    option2_value = db.Column(db.String(60))
    option3_name = db.Column(db.String(60))
    option3_value = db.Column(db.String(60))
    price_override = db.Column(db.Numeric(10, 2))
    inventory_quantity = db.Column(db.Integer, nullable=False, default=0)
    inventory_policy = db.Column(db.String(20), nullable=False, default="deny")  # deny|continue
    barcode = db.Column(db.String(120))
    position = db.Column(db.Integer, nullable=False, default=0)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        parts = [self.option1_value, self.option2_value, self.option3_value]
        joined = " / ".join(p for p in parts if p)
        return joined or "Default"


class ProductImage(db.Model):
    __tablename__ = "product_image"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    url = db.Column(db.Text, nullable=False)
    alt = db.Column(db.String(255))
    position = db.Column(db.Integer, nullable=False, default=0)


class ChannelAccount(db.Model):
    """Credentials for one connected store on one platform."""
    __tablename__ = "channel_account"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    channel = db.Column(db.String(40), nullable=False)  # woocommerce | shopify
    display_name = db.Column(db.String(120), nullable=False)
    base_url = db.Column(db.String(255), nullable=False)
    api_key_enc = db.Column(db.Text)
    api_secret_enc = db.Column(db.Text)
    webhook_secret_enc = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="active")  # active | disabled | error
    last_sync_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ChannelLink(db.Model):
    """Maps a Product (and optionally a Variant) to its remote IDs on one channel."""
    __tablename__ = "channel_link"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variant.id"))  # nullable: product-level link
    channel_account_id = db.Column(db.Integer, db.ForeignKey("channel_account.id"), nullable=False, index=True)
    remote_product_id = db.Column(db.String(120))
    remote_variant_id = db.Column(db.String(120))
    remote_sku = db.Column(db.String(120))
    last_pushed_at = db.Column(db.DateTime)
    last_pulled_at = db.Column(db.DateTime)
    remote_version = db.Column(db.String(120))  # etag, updated_at, whatever the platform gives us

    channel_account = db.relationship("ChannelAccount", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint(
            "channel_account_id", "remote_product_id", "remote_variant_id",
            name="uq_link_remote",
        ),
    )


class Order(db.Model):
    """Read-only mirror of paid orders that arrived via webhook."""
    __tablename__ = "order"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    channel_account_id = db.Column(db.Integer, db.ForeignKey("channel_account.id"), nullable=False, index=True)
    remote_order_id = db.Column(db.String(120), nullable=False)
    sku = db.Column(db.String(120), nullable=False, index=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variant.id"))
    quantity = db.Column(db.Integer, nullable=False, default=1)
    total = db.Column(db.Numeric(10, 2))
    currency = db.Column(db.String(3))
    ordered_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    raw_payload = db.Column(JsonType)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    channel_account = db.relationship("ChannelAccount", lazy="joined")
    variant = db.relationship("ProductVariant", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint(
            "channel_account_id", "remote_order_id", "sku",
            name="uq_order_per_line",
        ),
    )


class SyncJob(db.Model):
    """Audit + retry tracking for every sync action."""
    __tablename__ = "sync_job"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    channel_account_id = db.Column(db.Integer, db.ForeignKey("channel_account.id"))
    status = db.Column(db.String(20), nullable=False, default="queued")
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text)
    payload = db.Column(JsonType)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finished_at = db.Column(db.DateTime)

    product = db.relationship("Product", lazy="joined")
    channel_account = db.relationship("ChannelAccount", lazy="joined")


class WebhookEvent(db.Model):
    """Raw inbound webhook payload, kept for debugging and replay."""
    __tablename__ = "webhook_event"
    id = db.Column(db.Integer, primary_key=True)
    channel_account_id = db.Column(db.Integer, db.ForeignKey("channel_account.id"), nullable=False, index=True)
    topic = db.Column(db.String(120), nullable=False)
    signature_valid = db.Column(db.Boolean, nullable=False, default=False)
    payload = db.Column(JsonType)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    processed_at = db.Column(db.DateTime)
    process_error = db.Column(db.Text)

    channel_account = db.relationship("ChannelAccount", lazy="joined")


# ── Taxonomies (Category, Brand) ────────────────────────────────────────────
class Category(db.Model):
    """Hierarchical product category. Mirrors WooCommerce's category taxonomy.
    Each account has its own tree (account-scoped). For top-level categories
    parent_id is NULL. Imported from WC via remote_id; manual editing UI is
    a future iteration."""
    __tablename__ = "category"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200))
    parent_id = db.Column(db.Integer, db.ForeignKey("category.id"))    # nullable = top-level
    remote_id = db.Column(db.String(120))                               # WC category id
    remote_source = db.Column(db.String(40))                            # 'woocommerce'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    parent = db.relationship("Category", remote_side=[id], backref="children")

    __table_args__ = (
        db.UniqueConstraint("account_id", "remote_source", "remote_id",
                            name="uq_category_account_remote"),
        db.Index("ix_category_account_parent", "account_id", "parent_id"),
    )


class Brand(db.Model):
    """Flat brand. Each product can have multiple brands (WC 9.0 brand
    taxonomy semantics). Account-scoped, imported via remote_id."""
    __tablename__ = "brand"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200))
    remote_id = db.Column(db.String(120))                               # WC brand id
    remote_source = db.Column(db.String(40))                            # 'woocommerce'
    logo_url = db.Column(db.Text)                                       # populated later
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("account_id", "remote_source", "remote_id",
                            name="uq_brand_account_remote"),
    )
