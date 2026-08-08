"""HTTP endpoints."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accord.config import settings
from accord.db import get_session
from accord.ingest.statements import StatementParseError, parse_statement_csv
from accord.models import BreakRow, LedgerEntryRow, MatchRow, ReconRunRow, StatementRow
from accord.schemas import (
    BreakOut,
    BreakUpdate,
    IngestResult,
    MatchOut,
    RunRequest,
    RunSummary,
    StatementOut,
)
from accord.service import import_statement, ingest_ledger_events, run_reconciliation
from accord.service import update_break_status as apply_break_update

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")


# ------------------------------------------------------------- ingestion


@router.post("/ledger/events", response_model=IngestResult, tags=["Ingestion"])
def receive_ledger_events(
    events: list[dict[str, Any]],
    session: Session = Depends(get_session),
    x_obol_signature: str | None = Header(default=None),
) -> IngestResult:
    """Receive posting events from obol-ledger's outbox relay.

    The relay delivers **at-least-once**, so this endpoint is written to be
    called with the same batch repeatedly. Anything already stored is ignored
    at the database level and reported back as a duplicate rather than treated
    as a failure -- if repeats were errors the relay would retry them forever.
    """
    _verify_signature(x_obol_signature, events)

    config = settings()
    stored = ingest_ledger_events(session, events, config.reconciled_account)

    return IngestResult(
        received=len(events),
        stored=stored,
        duplicates_ignored=len(events) - stored,
    )


@router.post("/statements", response_model=StatementOut, tags=["Ingestion"])
def upload_statement(
    file: UploadFile = File(...),
    source: str = Query(default="bank-export"),
    account_code: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> StatementOut:
    """Import a bank statement CSV.

    Columns: ``value_date,reference,description,amount,currency``.

    Re-uploading a file already imported returns the original rather than
    creating a second copy — the file's hash is unique. Without that, one
    accidental double-upload reads as a page of duplicate breaks and looks
    exactly like a bank error.
    """
    content = file.file.read()
    try:
        lines = parse_statement_csv(content)
    except StatementParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if not lines:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "the file has no rows")

    row = import_statement(
        session,
        source=source,
        account_code=account_code or settings().reconciled_account,
        currency=lines[0].currency,
        lines=lines,
        content=content,
    )
    return StatementOut.model_validate(row)


@router.get("/ledger/entries", tags=["Ingestion"])
def list_ledger_entries(
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """The ledger side of the comparison, as ingested.

    Exists so the two records can be inspected independently. Being able to see
    what this service *believes* the ledger said, separately from what the
    ledger says now, is the first step in explaining any break.
    """
    rows = session.scalars(
        select(LedgerEntryRow).order_by(LedgerEntryRow.occurred_at.desc()).limit(limit)
    )
    return [
        {
            "id": row.id,
            "transferId": row.transfer_id,
            "externalId": row.external_id,
            "accountCode": row.account_code,
            "currency": row.currency,
            "amountMinor": row.amount_minor,
            "occurredAt": row.occurred_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/statements", response_model=list[StatementOut], tags=["Ingestion"])
def list_statements(session: Session = Depends(get_session)) -> list[StatementOut]:
    rows = session.scalars(select(StatementRow).order_by(StatementRow.imported_at.desc()).limit(50))
    return [StatementOut.model_validate(row) for row in rows]


# --------------------------------------------------------------- running


@router.post("/runs", response_model=RunSummary, tags=["Reconciliation"])
def start_run(request: RunRequest, session: Session = Depends(get_session)) -> RunSummary:
    """Reconcile a period and record the conclusions.

    Runs synchronously. Reconciliation over a day or a month finishes in
    milliseconds at this scale, and a caller that gets its answer in the
    response needs no polling, no job table and no way to be told a run
    succeeded that in fact failed.
    """
    config = settings()
    if request.period_start > request.period_end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "period_start is after period_end"
        )

    run = run_reconciliation(
        session,
        account_code=request.account_code or config.reconciled_account,
        period_start=request.period_start,
        period_end=request.period_end,
        statement_id=request.statement_id,
        config=config,
    )
    session.flush()
    log.info(
        "run %s: %d matched, %d breaks, %d unexplained",
        run.id,
        run.matched_count,
        run.break_count,
        run.unexplained_minor,
    )
    return RunSummary.of(run)


@router.get("/runs", response_model=list[RunSummary], tags=["Reconciliation"])
def list_runs(
    limit: int = Query(default=20, ge=1, le=100), session: Session = Depends(get_session)
) -> list[RunSummary]:
    rows = session.scalars(
        select(ReconRunRow).order_by(ReconRunRow.started_at.desc()).limit(limit)
    )
    return [RunSummary.of(row) for row in rows]


@router.get("/runs/{run_id}", response_model=RunSummary, tags=["Reconciliation"])
def get_run(run_id: str, session: Session = Depends(get_session)) -> RunSummary:
    run = session.get(ReconRunRow, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return RunSummary.of(run)


@router.get("/runs/{run_id}/matches", response_model=list[MatchOut], tags=["Reconciliation"])
def list_matches(
    run_id: str,
    rule: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[MatchOut]:
    query = select(MatchRow).where(MatchRow.run_id == run_id)
    if rule:
        query = query.where(MatchRow.rule == rule)
    return [MatchOut.model_validate(row) for row in session.scalars(query)]


# ---------------------------------------------------------------- breaks


@router.get("/breaks", response_model=list[BreakOut], tags=["Breaks"])
def list_breaks(
    open_only: bool = Query(default=True, description="Only exceptions still outstanding."),
    break_type: str | None = Query(default=None, alias="type"),
    run_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[BreakOut]:
    """The work queue.

    Ordered by how much money is unexplained, largest first. An operator with
    an hour should spend it on the £4,000 exception, not on whichever break
    happens to sort first by id.
    """
    query = select(BreakRow)
    if open_only:
        query = query.where(BreakRow.is_open.is_(True))
    if break_type:
        query = query.where(BreakRow.type == break_type)
    if run_id:
        query = query.where(BreakRow.run_id == run_id)

    query = query.order_by(func.abs(BreakRow.amount_minor).desc(), BreakRow.created_at).limit(limit)
    return [BreakOut.model_validate(row) for row in session.scalars(query)]


@router.get("/breaks/{break_id}", response_model=BreakOut, tags=["Breaks"])
def get_break(break_id: str, session: Session = Depends(get_session)) -> BreakOut:
    row = session.get(BreakRow, break_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such break")
    return BreakOut.model_validate(row)


@router.patch("/breaks/{break_id}", response_model=BreakOut, tags=["Breaks"])
def update_break(
    break_id: str, update: BreakUpdate, session: Session = Depends(get_session)
) -> BreakOut:
    """Move a break through its workflow.

    Every transition is appended to ``break_event`` and never overwritten.
    "Who wrote this off, when, and on what grounds" is the first question asked
    when a written-off break turns out to have been a real loss.
    """
    try:
        row = apply_break_update(
            session,
            break_id,
            status=update.status,
            actor=update.actor,
            note=update.note,
            assignee=update.assignee,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such break") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return BreakOut.model_validate(row)


# --------------------------------------------------------------- helpers


def _verify_signature(provided: str | None, events: list[dict[str, Any]]) -> None:
    """Check the shared secret, if one is configured.

    ``compare_digest`` rather than ``==``: string comparison short-circuits on
    the first differing byte, which leaks the secret one character at a time to
    anyone patient enough to time the responses.
    """
    secret = settings().webhook_secret
    if not secret:
        return
    if not provided or not hmac.compare_digest(provided, secret):
        log.warning(
            "rejected ledger events with a bad or missing signature (%d events)", len(events)
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

