"""Lazy RQ queue accessor."""
from __future__ import annotations

from functools import lru_cache

from flask import current_app
from redis import Redis
from rq import Queue


@lru_cache(maxsize=1)
def _redis() -> Redis:
    return Redis.from_url(current_app.config["REDIS_URL"])


def get_queue() -> Queue:
    return Queue(current_app.config["RQ_QUEUE"], connection=_redis())
