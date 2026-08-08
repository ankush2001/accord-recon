"""Configuration, read once from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACCORD_", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://obol:obol@localhost:55432/accord",
        description=(
            "55432, not 5432: the dev stack avoids a system PostgreSQL "
            "already holding the default port."
        ),
    )

    #: The one account being reconciled. A statement covers a single bank
    #: account, so a run that mixed several would be comparing the ledger's
    #: view of everything against the bank's view of one thing.
    reconciled_account: str = "asset:bank"

    #: Matching thresholds. Stored with every run, so a result can be
    #: reproduced later even after these change.
    date_window_days: int = 3
    amount_tolerance_minor: int = 0
    amount_tolerance_bps: int = 0
    timing_break_after_days: int = 2
    max_batch_size: int = 12

    #: Shared secret the ledger's outbox relay presents. Empty disables the
    #: check, which is convenient locally and wrong in production -- the
    #: readiness endpoint reports when it is unset.
    webhook_secret: str = ""

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _normalise_driver(cls, url: str) -> str:
        """Accept the connection string hosted Postgres actually hands out.

        Neon, Render, Supabase and Heroku all give you ``postgresql://…`` (or
        the long-deprecated ``postgres://``). SQLAlchemy needs the driver named
        explicitly, and without it silently reaches for psycopg2 -- which is
        not installed -- so the service dies at import with a ModuleNotFound
        that says nothing about the real cause.

        Rewriting it here means the URL can be pasted from the provider's
        dashboard unedited, which is the one step in a deploy most likely to be
        got wrong at 1am.
        """
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url


@lru_cache
def settings() -> Settings:
    return Settings()
