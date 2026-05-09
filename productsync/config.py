"""Flask config. Reads everything from environment via python-dotenv."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _normalize_db_url(url: str) -> str:
    # Heroku still hands out postgres:// but SQLAlchemy 2.x needs postgresql://
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.environ.get("DATABASE_URL", "sqlite:///productsync.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    RQ_QUEUE = "productsync-default"

    ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")

    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB image upload cap
