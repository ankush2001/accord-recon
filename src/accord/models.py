"""Storage model.

Three things are kept apart on purpose:

* **What each side said** -- ``ledger_entry`` and ``statement_line``, ingested
  as received and never rewritten by a reconciliation.
* **What a run concluded** -- ``recon_match`` and ``recon_break``, always tied
  to the run that produced them.
* **What a human then did** -- ``break_event``, an append-only trail.

Keeping conclusions separate from inputs is what makes a run repeatable. If
matching wrote its answers back onto the rows, re-running last Tuesday with a
corrected tolerance would be impossible: the inputs would already carry the old
run's opinions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class LedgerEntryRow(Base):
    """A settled posting, as obol-ledger reported it.

    Delivery from the ledger's outbox is at-least-once, so the same event will
    arrive more than once. The unique constraint on
    ``(transfer_id, account_code)`` is what makes that harmless: a repeat
    insert conflicts and is ignored rather than producing a phantom second
    payment that would then fail to reconcile.
    """

    __tablename__ = "ledger_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    transfer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256))
    account_code: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: Signed minor units, same convention as the ledger: money into this
    #: account is positive.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        UniqueConstraint("transfer_id", "account_code", name="ledger_entry_transfer_account_uq"),
        Index("ledger_entry_occurred_idx", "account_code", "occurred_at"),
        Index("ledger_entry_external_idx", "external_id"),
    )


class StatementRow(Base):
    """One imported statement file."""

    __tablename__ = "statement"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    account_code: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: SHA-256 of the uploaded bytes. Re-uploading the same file is detected
    #: rather than silently doubling every line in it -- which would otherwise
    #: show up as a page of duplicate breaks and look like a bank problem.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    lines: Mapped[list[StatementLineRow]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )


class StatementLineRow(Base):
    """One line of one statement, exactly as the bank sent it."""

    __tablename__ = "statement_line"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    statement_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("statement.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    value_date: Mapped[date] = mapped_column(Date, nullable=False)

    statement: Mapped[StatementRow] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("statement_id", "line_no", name="statement_line_no_uq"),
        Index("statement_line_value_date_idx", "value_date"),
    )


class ReconRunRow(Base):
    """One reconciliation, and enough context to reproduce it.

    ``config_snapshot`` is the point. Thresholds change; a run whose thresholds
    were not recorded cannot be re-derived, and "why did this reconcile in
    March but break in April?" becomes unanswerable.
    """

    __tablename__ = "recon_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_code: Mapped[str] = mapped_column(String(128), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    statement_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("statement.id"))

    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Fingerprint of the exact inputs. Two runs with the same fingerprint and
    #: the same config must produce identical output -- which the tests assert.
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    ledger_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    statement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    break_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Net unexplained amount in minor units. The single number a finance team
    #: actually asks for.
    unexplained_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    matches: Mapped[list[MatchRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    breaks: Mapped[list[BreakRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("recon_run_started_idx", "started_at"),)


class MatchRow(Base):
    """A conclusion, with the rule that produced it."""

    __tablename__ = "recon_match"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recon_run.id", ondelete="CASCADE"), nullable=False
    )
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Both sides are arrays because a batch settlement is many-to-one. JSONB
    #: rather than link tables: these are always read as a whole, never joined
    #: through, and Postgres can still index into them with a GIN index if that
    #: ever changes.
    ledger_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    statement_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    delta_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    date_gap_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)

    run: Mapped[ReconRunRow] = relationship(back_populates="matches")

    __table_args__ = (Index("recon_match_run_rule_idx", "run_id", "rule"),)


class BreakRow(Base):
    """An exception, and where a human has got to with it."""

    __tablename__ = "recon_break"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recon_run.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    ledger_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    statement_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: OPEN -> INVESTIGATING -> RESOLVED | WRITTEN_OFF
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    assignee: Mapped[str | None] = mapped_column(String(128))
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set when a later run raised the same exception again. A break that has
    #: persisted for eleven days is a different problem from one raised this
    #: morning, and without this the distinction is invisible.
    first_seen_run_id: Mapped[str | None] = mapped_column(String(36))
    recurrence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    run: Mapped[ReconRunRow] = relationship(back_populates="breaks")
    events: Mapped[list[BreakEventRow]] = relationship(
        back_populates="break_row", cascade="all, delete-orphan", order_by="BreakEventRow.at"
    )

    __table_args__ = (
        Index("recon_break_open_idx", "is_open", "type"),
        Index("recon_break_run_idx", "run_id"),
    )


class BreakEventRow(Base):
    """Append-only trail of what was done about a break.

    Never updated, only inserted -- the same rule the ledger applies to its
    postings. "Who marked this resolved, when, and on what grounds" is the
    first question asked when a written-off break turns out to have been real.
    """

    __tablename__ = "break_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    break_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recon_break.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    note: Mapped[str | None] = mapped_column(Text)

    break_row: Mapped[BreakRow] = relationship(back_populates="events")
