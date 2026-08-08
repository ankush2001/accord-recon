"""Storage behaviour: ingestion, reproducibility, and break history."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from accord.config import Settings
from accord.matching.types import StatementLine
from accord.models import BreakEventRow, BreakRow, LedgerEntryRow, ReconRunRow
from accord.service import (
    import_statement,
    ingest_ledger_events,
    run_reconciliation,
    update_break_status,
)

pytestmark = pytest.mark.integration

ACCOUNT = "asset:bank"
BASE = date(2026, 8, 3)
CONFIG = Settings(reconciled_account=ACCOUNT, date_window_days=3, timing_break_after_days=2)


def event(reference: str, amount: int, *, day: int = 0, state: str = "transfer.posted") -> dict:
    return {
        "eventType": state,
        "transferId": f"transfer-{reference}",
        "externalId": reference,
        "state": "POSTED",
        "currency": "USD",
        "amountMinor": abs(amount),
        "description": "customer payment",
        "occurredAt": datetime.combine(
            BASE + timedelta(days=day), datetime.min.time(), tzinfo=UTC
        ).isoformat(),
        "legs": [
            {
                "accountCode": ACCOUNT,
                "direction": "DEBIT" if amount > 0 else "CREDIT",
                "amountMinor": abs(amount),
            },
            {
                "accountCode": "liability:customers",
                "direction": "CREDIT",
                "amountMinor": abs(amount),
            },
        ],
    }


def line(id_no: int, amount: int, *, ref: str | None = None, day: int = 0) -> StatementLine:
    return StatementLine(
        id=f"stmt:{id_no}",
        statement_id="stmt",
        line_no=id_no,
        reference=ref,
        description="settlement",
        currency="USD",
        amount_minor=amount,
        value_date=BASE + timedelta(days=day),
    )


class TestLedgerIngestion:
    def test_events_are_stored_once_however_often_they_arrive(self, session) -> None:
        # Outbox delivery is at-least-once, so the relay *will* send these
        # again. A second copy would show up as an unmatched payment and be
        # reported as a break -- a bug that looks exactly like a bank error.
        events = [event("A", 1000), event("B", 2000)]

        assert ingest_ledger_events(session, events, ACCOUNT) == 2
        assert ingest_ledger_events(session, events, ACCOUNT) == 0
        assert ingest_ledger_events(session, [*events, event("C", 3000)], ACCOUNT) == 1

        assert session.scalar(select(func.count()).select_from(LedgerEntryRow)) == 3

    def test_pending_authorisations_are_not_ingested(self, session) -> None:
        # A hold has not moved money, so it cannot appear on a statement.
        # Ingesting it would manufacture a break that resolves itself on
        # capture -- noise that trains operators to ignore the queue.
        stored = ingest_ledger_events(
            session, [event("A", 1000, state="transfer.pending")], ACCOUNT
        )
        assert stored == 0

    def test_direction_becomes_a_sign(self, session) -> None:
        ingest_ledger_events(session, [event("IN", 5000), event("OUT", -5000)], ACCOUNT)

        amounts = sorted(session.scalars(select(LedgerEntryRow.amount_minor)))
        assert amounts == [-5000, 5000]

    def test_events_for_other_accounts_are_ignored(self, session) -> None:
        foreign = event("X", 1000)
        foreign["legs"] = [
            {"accountCode": "asset:other", "direction": "DEBIT", "amountMinor": 1000}
        ]
        assert ingest_ledger_events(session, [foreign], ACCOUNT) == 0


class TestStatementImport:
    def test_the_same_file_is_not_imported_twice(self, session) -> None:
        lines = [line(1, 1000, ref="A"), line(2, 2000, ref="B")]
        content = b"value_date,reference,description,amount,currency\n..."

        first = import_statement(
            session, source="bank", account_code=ACCOUNT, currency="USD",
            lines=lines, content=content,
        )
        second = import_statement(
            session, source="bank", account_code=ACCOUNT, currency="USD",
            lines=lines, content=content,
        )

        # Returning the original beats both alternatives: a second copy would
        # double every line, and an error would make a harmless retry fail.
        assert first.id == second.id
        assert first.line_count == 2


class TestRunReproducibility:
    def test_the_same_inputs_produce_the_same_fingerprint_and_result(self, session) -> None:
        ingest_ledger_events(session, [event("A", 1000), event("B", 2000)], ACCOUNT)
        import_statement(
            session, source="bank", account_code=ACCOUNT, currency="USD",
            lines=[line(1, 1000, ref="A", day=1), line(2, 2000, ref="B", day=1)],
            content=b"first",
        )

        first = run_reconciliation(
            session, account_code=ACCOUNT, period_start=BASE,
            period_end=BASE + timedelta(days=10), config=CONFIG,
        )
        second = run_reconciliation(
            session, account_code=ACCOUNT, period_start=BASE,
            period_end=BASE + timedelta(days=10), config=CONFIG,
        )

        # A run that cannot be reproduced cannot be defended six months later,
        # when someone asks why March reconciled and April did not.
        assert first.input_hash == second.input_hash
        assert (first.matched_count, first.break_count) == (
            second.matched_count,
            second.break_count,
        )

    def test_the_thresholds_are_stored_with_the_run(self, session) -> None:
        run = run_reconciliation(
            session, account_code=ACCOUNT, period_start=BASE,
            period_end=BASE + timedelta(days=10), config=CONFIG,
        )
        # Without this, changing a tolerance silently rewrites the meaning of
        # every historical run.
        assert run.config_snapshot["date_window_days"] == 3
        assert "amount_tolerance_minor" in run.config_snapshot

    def test_unexplained_excludes_timing_differences(self, session) -> None:
        # Money that merely arrived late is not exposure. Counting it would
        # overstate the number every time a bank settled on a Monday.
        ingest_ledger_events(session, [event("LATE", 5000)], ACCOUNT)
        import_statement(
            session, source="bank", account_code=ACCOUNT, currency="USD",
            lines=[line(1, 5000, ref="LATE", day=3)], content=b"late",
        )

        run = run_reconciliation(
            session, account_code=ACCOUNT, period_start=BASE,
            period_end=BASE + timedelta(days=10), config=CONFIG,
        )

        assert run.matched_count == 1
        assert run.break_count == 1
        assert run.unexplained_minor == 0


class TestBreakLifecycle:
    def _run_with_one_break(self, session) -> ReconRunRow:
        ingest_ledger_events(session, [event("GONE", 7500)], ACCOUNT)
        return run_reconciliation(
            session, account_code=ACCOUNT, period_start=BASE,
            period_end=BASE + timedelta(days=10), config=CONFIG,
        )

    def test_a_persistent_break_keeps_its_history(self, session) -> None:
        self._run_with_one_break(session)
        second = self._run_with_one_break(session)

        current = session.scalars(
            select(BreakRow).where(BreakRow.is_open.is_(True), BreakRow.run_id == second.id)
        ).one()

        # An eleven-day-old break is a different problem from one raised this
        # morning, and an operator has to be able to tell them apart.
        assert current.recurrence == 2
        assert current.first_seen_run_id is not None
        assert current.first_seen_run_id != second.id

    def test_investigation_status_survives_the_next_run(self, session) -> None:
        first = self._run_with_one_break(session)
        opened = session.scalars(
            select(BreakRow).where(BreakRow.run_id == first.id)
        ).one()

        update_break_status(
            session, opened.id, status="INVESTIGATING",
            actor="ankush", note="chasing the bank", assignee="ankush",
        )

        second = self._run_with_one_break(session)
        current = session.scalars(
            select(BreakRow).where(BreakRow.is_open.is_(True), BreakRow.run_id == second.id)
        ).one()

        # Resetting this every run would throw away an analyst's work each
        # morning and make the queue useless.
        assert current.status == "INVESTIGATING"
        assert current.assignee == "ankush"

    def test_a_break_that_stops_recurring_is_closed(self, session) -> None:
        self._run_with_one_break(session)

        # The payment turns up on the statement, so the exception is gone.
        import_statement(
            session, source="bank", account_code=ACCOUNT, currency="USD",
            lines=[line(1, 7500, ref="GONE", day=1)], content=b"arrived",
        )
        final = run_reconciliation(
            session, account_code=ACCOUNT, period_start=BASE,
            period_end=BASE + timedelta(days=10), config=CONFIG,
        )

        assert final.break_count == 0
        assert session.scalar(
            select(func.count()).select_from(BreakRow).where(BreakRow.is_open.is_(True))
        ) == 0

    def test_every_transition_is_recorded_and_never_overwritten(self, session) -> None:
        first = self._run_with_one_break(session)
        row = session.scalars(select(BreakRow).where(BreakRow.run_id == first.id)).one()

        update_break_status(session, row.id, status="INVESTIGATING", actor="ankush")
        update_break_status(
            session, row.id, status="WRITTEN_OFF", actor="ankush", note="bank confirmed reversal"
        )

        events = session.scalars(
            select(BreakEventRow).where(BreakEventRow.break_id == row.id).order_by(BreakEventRow.at)
        ).all()

        # "Who wrote this off, when, and on what grounds" is the first question
        # asked when a written-off break turns out to have been a real loss.
        assert [e.to_status for e in events] == ["OPEN", "INVESTIGATING", "WRITTEN_OFF"]
        assert events[-1].note == "bank confirmed reversal"
        assert row.is_open is False

    def test_an_unknown_status_is_rejected(self, session) -> None:
        first = self._run_with_one_break(session)
        row = session.scalars(select(BreakRow).where(BreakRow.run_id == first.id)).one()

        with pytest.raises(ValueError, match="status must be one of"):
            update_break_status(session, row.id, status="PROBABLY_FINE")
