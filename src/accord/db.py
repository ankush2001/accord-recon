"""Engine and session wiring."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from accord.config import settings

_engine = create_engine(
    settings().database_url,
    # Free-tier Postgres allows very few connections and drops idle ones.
    # pool_pre_ping turns a stale-connection crash into a transparent reconnect.
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    future=True,
)

SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def engine() -> Engine:
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction that commits on success and rolls back on anything else."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
