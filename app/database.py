"""Database engine/session configuration."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/catalog.db")
    if url.startswith("sqlite:///") and ":memory:" not in url:
        # sqlite:///./data/catalog.db -> ensure ./data exists.
        prefix = "sqlite:///"
        raw_path = url[len(prefix) :]
        if raw_path.startswith("./"):
            raw_path = raw_path[2:]
        path = Path(raw_path)
        if path.parent and str(path.parent) not in (".", ""):
            path.parent.mkdir(parents=True, exist_ok=True)
    return url


DATABASE_URL = _database_url()

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
_engine_kwargs = {"connect_args": _connect_args}
if DATABASE_URL == "sqlite://" or ":memory:" in DATABASE_URL:
    from sqlalchemy.pool import StaticPool

    _engine_kwargs["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    # PRAGMAs are specific to SQLite.
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if DATABASE_URL not in ("sqlite://", "sqlite:///:memory:"):
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
    except Exception:
        # Non-SQLite engines ignore this listener's practical effect.
        pass


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register models)

    Base.metadata.create_all(bind=engine)
