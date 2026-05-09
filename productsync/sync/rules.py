"""Field policy: which side wins for which field.

This is the authoritative answer to "two-way sync, but per field":
- Real paid orders ALWAYS flow inward and are append-only.
- Inventory flows inward (because it's derived from orders), and outward only
  when we want to mirror a known-good baseline (e.g. a manual adjustment or
  syncing a decrement made via one channel to the other).
- Everything content-shaped (title, description, images, price, status) flows
  outward from the dashboard to all channels.
"""
from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    PUSH = "push"          # dashboard → channel
    PULL = "pull"          # channel → dashboard
    APPEND = "append"      # channel → dashboard, append-only (e.g. orders)
    MIRROR = "mirror"      # dashboard → channel after a pull, to mirror state across channels


# Fields the dashboard owns and pushes to all channels.
PUSH_FIELDS = {
    "title", "description", "images", "tags", "categories",
    "price", "compare_at_price", "status", "weight_grams",
}

# Variant-level fields the dashboard owns.
PUSH_VARIANT_FIELDS = {
    "sku", "title", "option1_value", "option2_value", "option3_value",
    "price_override", "barcode",
}

# Fields the channels own; dashboard mirrors what comes in.
PULL_FIELDS = {"inventory_quantity"}

# Append-only event types from the channel side.
APPEND_TOPICS = {
    "order.created", "order.updated", "orders/create", "orders/paid",
}
