"""Request and response shapes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from accord.models import ReconRunRow

BreakStatus = Literal["OPEN", "INVESTIGATING", "RESOLVED", "WRITTEN_OFF"]


class RunRequest(BaseModel):
    account_code: str | None = Field(
        default=None, description="Defaults to the configured reconciled account."
    )
    period_start: date
    period_end: date
    statement_id: str | None = Field(
        default=None,
        description=(
            "Restrict to one imported statement. Omitted, every line in the period is used."
        ),
    )


class RunSummary(BaseModel):
    id: str
    account_code: str
    period_start: date
    period_end: date
    ledger_count: int
    statement_count: int
    matched_count: int
    break_count: int
    unexplained_minor: int
    #: Share of rows on both sides that were explained. The headline number,
    #: and the one to watch over time -- a falling match rate is usually the
    #: first sign that an upstream format changed.
    #:
    #: Defaulted because it is derived rather than stored: `of` computes it
    #: from the run's matches after validating the row.
    match_rate: float = 0.0
    duration_ms: int | None
    started_at: datetime
    input_hash: str
    config_snapshot: dict[str, Any]

    model_config = {"from_attributes": True}

    @staticmethod
    def of(row: ReconRunRow) -> RunSummary:
        """Build the response, deriving the match rate from the stored matches.

        The rate counts *rows explained*, not matches made. One batch
        settlement that accounts for twelve payments and one statement line is
        thirteen rows explained by a single match — counting matches would
        report it as one and make a good reconciliation look like a poor one.
        """
        total = row.ledger_count + row.statement_count
        explained = sum(len(m.ledger_ids) + len(m.statement_ids) for m in row.matches)

        summary = RunSummary.model_validate(row)
        summary.match_rate = round(explained / total, 4) if total else 1.0
        return summary


class MatchOut(BaseModel):
    id: str
    rule: str
    ledger_ids: list[str]
    statement_ids: list[str]
    delta_minor: int
    date_gap_days: int
    confidence: float

    model_config = {"from_attributes": True}


class BreakOut(BaseModel):
    id: str
    run_id: str
    type: str
    side: str
    ledger_ids: list[str]
    statement_ids: list[str]
    amount_minor: int
    currency: str
    detail: str
    status: BreakStatus
    assignee: str | None
    resolution: str | None
    #: How many consecutive runs have raised this same exception. A break on
    #: its eleventh run is a different problem from one raised this morning.
    recurrence: int
    is_open: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class BreakUpdate(BaseModel):
    status: BreakStatus
    assignee: str | None = None
    note: str | None = Field(
        default=None,
        description="Recorded on the audit trail. Required in spirit when writing a break off.",
    )
    actor: str = "operator"


class StatementOut(BaseModel):
    id: str
    source: str
    account_code: str
    currency: str
    period_start: date
    period_end: date
    line_count: int
    imported_at: datetime

    model_config = {"from_attributes": True}


class IngestResult(BaseModel):
    received: int
    stored: int
    #: Duplicates are expected, not an error: outbox delivery is at-least-once
    #: and the consumer is responsible for absorbing repeats.
    duplicates_ignored: int
