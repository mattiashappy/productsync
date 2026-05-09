"""Channel adapter contract.

A Channel translates between our canonical Product/Variant model and a remote
storefront's API. Adding a new platform = one new file implementing this ABC.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import ChannelAccount, ChannelLink, Product


@dataclass
class ChannelEvent:
    """Normalized inbound event from a channel webhook."""
    topic: str                   # e.g. "order.created", "product.updated"
    signature_valid: bool
    payload: dict[str, Any] = field(default_factory=dict)
    raw_body: bytes = b""


class ChannelError(Exception):
    """Adapter raises this when the remote API rejects an operation."""


class Channel(ABC):
    name: str = "base"

    def __init__(self, channel_account: ChannelAccount):
        self.channel_account = channel_account

    # ── Outbound: dashboard → channel ────────────────────────────────────────
    @abstractmethod
    def push_product(self, product: Product, link: ChannelLink | None) -> ChannelLink:
        """Create or update the remote product. Return updated/created ChannelLink."""

    @abstractmethod
    def push_inventory(self, link: ChannelLink, quantity: int) -> None:
        """Set inventory for a single variant on the remote side."""

    # ── Inbound: channel → dashboard ─────────────────────────────────────────
    @abstractmethod
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> ChannelEvent:
        """Verify signature and return a normalized event. Always returns
        a ChannelEvent (with signature_valid=False if verification failed).
        """

    @abstractmethod
    def apply_webhook(self, event: ChannelEvent) -> None:
        """Mutate dashboard state for a verified inbound event (e.g. record an
        Order, decrement stock). Called by the worker, not the HTTP handler.
        """

    # ── Optional: pull-based reconciliation ──────────────────────────────────
    def pull_product(self, link: ChannelLink) -> dict[str, Any] | None:
        """Fetch remote state for drift detection. Optional for MVP."""
        return None


_REGISTRY: dict[str, type[Channel]] = {}


def register(channel_cls: type[Channel]) -> type[Channel]:
    _REGISTRY[channel_cls.name] = channel_cls
    return channel_cls


def get_channel(channel_account: ChannelAccount) -> Channel:
    cls = _REGISTRY.get(channel_account.channel)
    if cls is None:
        raise ChannelError(f"No adapter registered for channel '{channel_account.channel}'")
    return cls(channel_account)
