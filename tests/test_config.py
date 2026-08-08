"""Configuration handling — mostly the connection-string footgun."""

from __future__ import annotations

import pytest

from accord.config import Settings


class TestDatabaseUrlNormalisation:
    @pytest.mark.parametrize(
        "given",
        [
            "postgresql://user:pw@ep-cool-1.neon.tech/accord?sslmode=require",
            "postgres://user:pw@ep-cool-1.neon.tech/accord?sslmode=require",
        ],
    )
    def test_hosted_provider_urls_get_the_driver_added(self, given: str) -> None:
        # Every hosted Postgres hands out one of these two forms. Without the
        # driver, SQLAlchemy reaches for psycopg2 -- which is not installed --
        # and the service dies at import with a ModuleNotFoundError that says
        # nothing at all about the connection string.
        settings = Settings(database_url=given)

        assert settings.database_url.startswith("postgresql+psycopg://")
        assert "ep-cool-1.neon.tech/accord?sslmode=require" in settings.database_url

    def test_an_explicit_driver_is_left_alone(self) -> None:
        given = "postgresql+psycopg://obol:obol@localhost:55432/accord"
        assert Settings(database_url=given).database_url == given

    def test_credentials_survive_the_rewrite(self) -> None:
        # Rewriting a URL by string surgery is exactly where a password
        # containing "postgres://" or a stray slash would get mangled.
        given = "postgresql://u:p@ss/w0rd@host:5432/db"
        assert Settings(database_url=given).database_url == "postgresql+psycopg://u:p@ss/w0rd@host:5432/db"
