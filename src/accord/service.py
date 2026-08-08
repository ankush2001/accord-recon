"""Everything between the HTTP layer and the matching engine."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from accord.config import Settings, settings
from accord.matching.engine import reconcile
from accord.matching.types import (
    Break,
    LedgerEntry,
    MatchingConfig,
    ReconResult,
    StatementLine,
)
from accord.models import (
    BreakEventRow,
    BreakRow,
    LedgerEntryRow,
    MatchRow,
    ReconRunRow,
    StatementLineRow,
    StatementRow,
)

OPEN_STATUSES = ("OPEN", "INVESTIGATING")
TERMINAL_STATUSES = ("RESOLVED", "WRITTEN_OFF")


# ------------------------------------------------------------- ingestion


def ingest_ledger_events(
    session: Session, events: list[dict[str, Any]], account_code: str
) -> int:
    """Absorb posting events from the ledger's outbox.

    Delivery is at-least-once by design, so this must be safe to call with the
    same events repeatedly. ``ON CONFLICT DO NOTHING`` against the
    ``(transfer_id, account_code)`` unique index does that at the database
    level rather than with a read-then-write that concurrent deliveries would
    both pass.

    Only settled transfers are taken. A pending authorisation has not moved
    money, so it cannot appear on a bank statement, and ingesting it would
    manufacture a break that resolves itself on capture.
    """
    rows = []
    for event in events:
        if event.get("eventType") != "transfer.posted":
            continue

        leg = next(
            (
                candidate
                for candidate in event.get("legs", [])
                if candidate.get("accountCode") == account_code
            ),
            None,
        )
        if leg is None:
            continue

        # The ledger reports direction and magnitude; storage wants one signed
        # number, in the same convention the statement uses.
        sign = 1 if leg["direction"] == "DEBIT" else -1

        rows.append(
            {
                "transfer_id": event["transferId"],
                "external_id": event.get("externalId"),
                "account_code": account_code,
                "currency": event["currency"],
                "amount_minor": sign * int(leg["amountMinor"]),
                "description": event.get("description"),
                "occurred_at": _parse_instant(event["occurredAt"]),
            }
        )

    if not rows:
        return 0

    # RETURNING and count the rows, rather than trusting result.rowcount.
    # For a multi-row INSERT ... ON CONFLICT DO NOTHING, psycopg reports -1
    # ("unknown") often enough that the caller cannot tell "nothing was new"
    # from "the driver did not say" -- and this number is reported back to the
    # relay as how much of its batch landed.
    statement = (
        pg_insert(LedgerEntryRow)
        .values(rows)
        .on_conflict_do_nothing(constraint="ledger_entry_transfer_account_uq")
        .returning(LedgerEntryRow.id)
    )
    return len(session.execute(statement).scalars().all())


def import_statement(
    session: Session,
    *,
    source: str,
    account_code: str,
    currency: str,
    lines: list[StatementLine],
    content: bytes,
) -> StatementRow:
    """Store a parsed statement file.

    The file's SHA-256 is unique, so re-uploading the same export is rejected
    rather than doubling every line in it. Without that, a second upload
    produces a page of duplicate breaks that reads exactly like a bank error.
    """
    content_hash = hashlib.sha256(content).hexdigest()

    existing = session.scalar(select(StatementRow).where(StatementRow.content_hash == content_hash))
    if existing is not None:
        return existing

    statement = StatementRow(
        source=source,
        account_code=account_code,
        currency=currency,
        period_start=min(line.value_date for line in lines) if lines else date.today(),
        period_end=max(line.value_date for line in lines) if lines else date.today(),
        content_hash=content_hash,
        line_count=len(lines),
    )
    session.add(statement)
    session.flush()

    for line in lines:
        session.add(
            StatementLineRow(
                statement_id=statement.id,
                line_no=line.line_no,
                reference=line.reference,
                description=line.description,
                currency=line.currency,
                amount_minor=line.amount_minor,
                value_date=line.value_date,
            )
        )

    session.flush()
    return statement


# --------------------------------------------------------- reconciliation


def run_reconciliation(
    session: Session,
    *,
    account_code: str,
    period_start: date,
    period_end: date,
    statement_id: str | None = None,
    config: Settings | None = None,
) -> ReconRunRow:
    """Reconcile one account over one period and persist the conclusions."""
    config = config or settings()
    started = time.perf_counter()

    entries = _load_ledger_entries(session, account_code, period_start, period_end)
    lines = _load_statement_lines(session, statement_id, period_start, period_end)

    matching_config = MatchingConfig(
        date_window_days=config.date_window_days,
        amount_tolerance_minor=config.amount_tolerance_minor,
        amount_tolerance_bps=config.amount_tolerance_bps,
        timing_break_after_days=config.timing_break_after_days,
        max_batch_size=config.max_batch_size,
    )

    result = reconcile(entries, lines, matching_config)

    run = ReconRunRow(
        account_code=account_code,
        period_start=period_start,
        period_end=period_end,
        statement_id=statement_id,
        config_snapshot=asdict(matching_config),
        input_hash=_fingerprint(entries, lines, matching_config),
        ledger_count=result.ledger_count,
        statement_count=result.statement_count,
        matched_count=len(result.matches),
        break_count=len(result.breaks),
        unexplained_minor=_unexplained(result),
        finished_at=datetime.now(UTC),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    session.add(run)
    session.flush()

    for match in result.matches:
        session.add(
            MatchRow(
                run_id=run.id,
                rule=match.rule.value,
                ledger_ids=list(match.ledger_ids),
                statement_ids=list(match.statement_ids),
                delta_minor=match.delta_minor,
                date_gap_days=match.date_gap_days,
                confidence=match.confidence,
            )
        )

    _persist_breaks(session, run, result.breaks)
    session.flush()
    return run


def _persist_breaks(session: Session, run: ReconRunRow, breaks: list[Break]) -> None:
    """Write this run's breaks, carrying forward what was known about them.

    A break is not a fresh discovery just because the reconciliation ran again.
    If the same exception was already open, its investigation status, assignee
    and original sighting move across, and a counter records how many runs it
    has survived. Losing that on every run would reset an analyst's work each
    morning and make a two-week-old problem indistinguishable from a new one.
    """
    previously_open = {
        _break_fingerprint(row.type, row.ledger_ids, row.statement_ids): row
        for row in session.scalars(
            select(BreakRow).where(BreakRow.is_open.is_(True))
        )
    }

    seen: set[str] = set()

    for detected in breaks:
        fingerprint = _break_fingerprint(
            detected.type.value, list(detected.ledger_ids), list(detected.statement_ids)
        )
        seen.add(fingerprint)
        earlier = previously_open.get(fingerprint)

        row = BreakRow(
            run_id=run.id,
            type=detected.type.value,
            side=detected.side.value,
            ledger_ids=list(detected.ledger_ids),
            statement_ids=list(detected.statement_ids),
            amount_minor=detected.amount_minor,
            currency=detected.currency,
            detail=detected.detail,
            status=earlier.status if earlier else "OPEN",
            assignee=earlier.assignee if earlier else None,
            first_seen_run_id=(earlier.first_seen_run_id or earlier.run_id) if earlier else run.id,
            recurrence=(earlier.recurrence + 1) if earlier else 1,
            is_open=True,
        )
        session.add(row)
        session.flush()

        if earlier is None:
            session.add(
                BreakEventRow(break_id=row.id, to_status="OPEN", actor="system", note="detected")
            )
        else:
            # Supersede rather than mutate: each run's conclusions stay exactly
            # as that run recorded them.
            earlier.is_open = False

    # An exception the latest run no longer raises has gone away -- the missing
    # payment arrived, the duplicate was reversed. Close it, with a reason.
    for fingerprint, stale in previously_open.items():
        if fingerprint not in seen and stale.is_open:
            stale.is_open = False
            stale.status = "RESOLVED"
            stale.resolved_at = datetime.now(UTC)
            stale.resolution = "no longer detected"
            session.add(
                BreakEventRow(
                    break_id=stale.id,
                    from_status=stale.status,
                    to_status="RESOLVED",
                    actor="system",
                    note=f"not raised by run {run.id}",
                )
            )


def update_break_status(
    session: Session,
    break_id: str,
    *,
    status: str,
    actor: str = "operator",
    note: str | None = None,
    assignee: str | None = None,
) -> BreakRow:
    """Move a break through its workflow, recording who did it."""
    row = session.get(BreakRow, break_id)
    if row is None:
        raise KeyError(break_id)

    allowed = (*OPEN_STATUSES, *TERMINAL_STATUSES)
    if status not in allowed:
        raise ValueError(f"status must be one of {allowed}")

    previous = row.status
    row.status = status
    if assignee is not None:
        row.assignee = assignee
    if status in TERMINAL_STATUSES:
        row.resolved_at = datetime.now(UTC)
        row.resolution = note
        row.is_open = False

    session.add(
        BreakEventRow(
            break_id=row.id, from_status=previous, to_status=status, actor=actor, note=note
        )
    )
    session.flush()
    return row


# ------------------------------------------------------------- internals


def _load_ledger_entries(
    session: Session, account_code: str, start: date, end: date
) -> list[LedgerEntry]:
    rows = session.scalars(
        select(LedgerEntryRow)
        .where(
            LedgerEntryRow.account_code == account_code,
            LedgerEntryRow.occurred_at >= datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            LedgerEntryRow.occurred_at < datetime.combine(end, datetime.max.time(), tzinfo=UTC),
        )
        .order_by(LedgerEntryRow.occurred_at, LedgerEntryRow.id)
    )
    return [
        LedgerEntry(
            id=row.id,
            transfer_id=row.transfer_id,
            external_id=row.external_id,
            account_code=row.account_code,
            currency=row.currency,
            amount_minor=row.amount_minor,
            occurred_at=row.occurred_at,
        )
        for row in rows
    ]


def _load_statement_lines(
    session: Session, statement_id: str | None, start: date, end: date
) -> list[StatementLine]:
    query = select(StatementLineRow).where(
        StatementLineRow.value_date >= start, StatementLineRow.value_date <= end
    )
    if statement_id:
        query = query.where(StatementLineRow.statement_id == statement_id)

    rows = session.scalars(query.order_by(StatementLineRow.value_date, StatementLineRow.line_no))
    return [
        StatementLine(
            id=row.id,
            statement_id=row.statement_id,
            line_no=row.line_no,
            reference=row.reference,
            description=row.description,
            currency=row.currency,
            amount_minor=row.amount_minor,
            value_date=row.value_date,
        )
        for row in rows
    ]


def _fingerprint(
    entries: list[LedgerEntry], lines: list[StatementLine], config: MatchingConfig
) -> str:
    """Hash the exact inputs and thresholds of a run.

    Two runs sharing a fingerprint must produce identical output. That is
    asserted in the tests, and it is what makes "re-run March with the
    corrected tolerance" a meaningful request rather than a hopeful one.
    """
    payload = {
        "ledger": sorted((e.id, e.amount_minor, e.occurred_at.isoformat()) for e in entries),
        "statement": sorted((s.id, s.amount_minor, s.value_date.isoformat()) for s in lines),
        "config": asdict(config),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _unexplained(result: ReconResult) -> int:
    """Net value of everything the run could not account for.

    Timing differences are excluded: that money was matched, it simply arrived
    late, and counting it as unexplained would overstate the exposure every
    time a bank settled on a Monday.
    """
    return sum(
        b.amount_minor for b in result.breaks if b.type.value != "TIMING_DIFFERENCE"
    )


def _break_fingerprint(break_type: str, ledger_ids: list[str], statement_ids: list[str]) -> str:
    return "|".join([break_type, ",".join(sorted(ledger_ids)), ",".join(sorted(statement_ids))])


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
