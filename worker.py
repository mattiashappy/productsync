"""RQ worker entrypoint.

Heroku worker dyno: `worker: rq worker -u $REDIS_URL productsync-default`
Locally: `python worker.py` works too, but `rq worker` is the standard path.
"""
from __future__ import annotations

import os

from redis import Redis
from rq import Connection, Queue, Worker

from productsync.config import Config

if __name__ == "__main__":
    redis_url = os.environ.get("REDIS_URL", Config.REDIS_URL)
    queue_name = os.environ.get("RQ_QUEUE", Config.RQ_QUEUE)
    with Connection(Redis.from_url(redis_url)):
        worker = Worker([Queue(queue_name)])
        worker.work()
