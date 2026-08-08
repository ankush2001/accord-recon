"""Shared fixtures.

The engine tests need none of this -- they are pure. Everything here exists for
the tests that must prove the *storage* behaves: that at-least-once ingestion
really is idempotent, that a run is really reproducible, that break history
really carries forward.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Colima puts the Docker socket somewhere Testcontainers does not look. Set
# before importing testcontainers, which reads the environment at import time.
# Guarded so an explicit DOCKER_HOST, or a normal Linux CI runner, is untouched.
if "DOCKER_HOST" not in os.environ:
    colima = Path.home() / ".colima" / "default" / "docker.sock"
    if colima.exists():
        os.environ["DOCKER_HOST"] = f"unix://{colima}"
        os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

try:  # testcontainers 4.9+ moved the module
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover - older installs
    from testcontainers.postgres import PostgresContainer

from accord.models import Base


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    """One container for the whole suite; each test cleans up after itself."""
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def engine(postgres: PostgresContainer):
    engine = create_engine(postgres.get_connection_url(), future=True)
    # create_all rather than running Alembic: these tests are about behaviour,
    # and the migration is verified separately by CI applying it to an empty
    # database. Coupling every test to the migration history would mean a
    # rename breaks a hundred tests that do not care.
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    """A session whose work is rolled back, so tests cannot leak into each other."""
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()
