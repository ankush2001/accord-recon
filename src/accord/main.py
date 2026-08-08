"""Application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from accord.api import dashboard, routes
from accord.config import settings
from accord.db import engine

log = logging.getLogger("accord")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=settings().log_level,
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
    )

    if not settings().webhook_secret:
        # Loud, because the failure mode is silent: without a secret anyone who
        # can reach this service can inject ledger entries, and a reconciliation
        # against fabricated inputs will happily report all clear.
        log.warning(
            "ACCORD_WEBHOOK_SECRET is not set — the ledger event endpoint is unauthenticated"
        )

    log.info("accord ready, reconciling %s", settings().reconciled_account)
    yield


app = FastAPI(
    title="accord-recon",
    version="1.0.0",
    lifespan=lifespan,
    description="""
Reconciliation and break detection for payment ledgers.

Consumes settled postings from
[obol-ledger](https://github.com/ankush2001/obol-ledger) and compares them
against external bank statements. What it produces is not a match rate but a
**classified queue of exceptions** — payments the bank never sent, credits the
ledger never recorded, amounts that disagree, duplicates, and settlements that
arrived late — ordered by how much money is unexplained.

Matching runs in passes, most certain first: shared reference, then identical
amount inside a date window, then amounts differing by no more than a fee, then
batch settlements where one payout line covers many payments. Ambiguity is
resolved as a minimum-cost assignment rather than greedily, so the result does
not depend on the order rows arrived in.
""",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.include_router(routes.router)
app.include_router(dashboard.router)


@app.get("/health", tags=["Ops"])
def health() -> dict[str, object]:
    """Liveness plus a real database check.

    A health endpoint that only proves the process is running will report a
    service healthy while every request it serves fails on the database. One
    round trip is worth the cost.
    """
    try:
        with engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        database = "up"
    except Exception as exc:
        log.error("health check could not reach the database: %s", exc)
        database = "down"

    return {
        "status": "ok" if database == "up" else "degraded",
        "database": database,
        "account": settings().reconciled_account,
        "authenticated_ingest": bool(settings().webhook_secret),
    }
