"""What the matcher must and must not conclude.

The engine is pure, so these tests need no database, no fixtures and no
network -- which is exactly why the engine was written that way.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from accord.matching.engine import normalise_reference, reconcile
from accord.matching.types import (
    BreakType,
    LedgerEntry,
    MatchingConfig,
    MatchRule,
    StatementLine,
)

BASE = date(2026, 8, 3)


def entry(
    id_: str,
    amount: int,
    *,
    ref: str | None = None,
    day: int = 0,
    currency: str = "USD",
) -> LedgerEntry:
    return LedgerEntry(
        id=id_,
        transfer_id=f"transfer-{id_}",
        external_id=ref,
        account_code="asset:bank",
        currency=currency,
        amount_minor=amount,
        occurred_at=datetime.combine(BASE + timedelta(days=day), datetime.min.time(), tzinfo=UTC),
    )


def line(
    id_: str,
    amount: int,
    *,
    ref: str | None = None,
    day: int = 0,
    currency: str = "USD",
    description: str = "payment",
) -> StatementLine:
    return StatementLine(
        id=id_,
        statement_id="stmt-1",
        line_no=int(id_[1:]) if id_[1:].isdigit() else 0,
        reference=ref,
        description=description,
        currency=currency,
        amount_minor=amount,
        value_date=BASE + timedelta(days=day),
    )


# ------------------------------------------------------------- references


class TestReferenceNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("psp_ch_9f2a41", "PSPCH9F2A41"),
            ("PSP-CH-9F2A41", "PSPCH9F2A41"),
            ("  psp ch 9f2a41  ", "PSPCH9F2A41"),
            ("", None),
            (None, None),
            ("///", None),
        ],
    )
    def test_formatting_noise_is_stripped(self, raw: str | None, expected: str | None) -> None:
        # Banks reformat references in transit. Comparing them raw is the
        # single biggest cause of a low match rate on real data.
        assert normalise_reference(raw) == expected


# ------------------------------------------------------------- the passes


class TestExactReference:
    def test_matches_on_a_shared_reference(self) -> None:
        result = reconcile([entry("L1", 5000, ref="psp_ch_1")], [line("S1", 5000, ref="PSP-CH-1")])

        assert len(result.matches) == 1
        assert result.matches[0].rule is MatchRule.EXACT_REFERENCE
        assert result.matches[0].delta_minor == 0
        assert result.breaks == []

    def test_a_shared_reference_with_different_amounts_is_a_mismatch_not_a_miss(self) -> None:
        # Both systems say this is the same payment and disagree about the
        # money. Reporting two unrelated "missing" rows instead would hide the
        # single fact that matters.
        result = reconcile([entry("L1", 5000, ref="abc")], [line("S1", 4500, ref="abc")])

        assert result.matches == []
        assert len(result.breaks) == 1
        assert result.breaks[0].type is BreakType.AMOUNT_MISMATCH
        assert result.breaks[0].amount_minor == -500

    def test_a_reference_match_beats_a_closer_amount_only_candidate(self) -> None:
        # S2 is the perfect amount-and-date candidate, but S1 carries the
        # reference. Certainty must win over coincidence.
        result = reconcile(
            [entry("L1", 5000, ref="abc")],
            [line("S1", 5000, ref="abc", day=2), line("S2", 5000, day=0)],
        )

        match = next(m for m in result.matches if m.rule is MatchRule.EXACT_REFERENCE)
        assert match.statement_ids == ("S1",)


class TestAmountAndDate:
    def test_matches_without_a_reference_inside_the_window(self) -> None:
        result = reconcile([entry("L1", 2500)], [line("S1", 2500, day=2)])

        assert len(result.matches) == 1
        assert result.matches[0].rule is MatchRule.AMOUNT_AND_DATE
        assert result.matches[0].date_gap_days == 2

    def test_will_not_match_outside_the_window(self) -> None:
        result = reconcile(
            [entry("L1", 2500)],
            [line("S1", 2500, day=30)],
            MatchingConfig(date_window_days=3),
        )

        assert result.matches == []
        assert {b.type for b in result.breaks} == {
            BreakType.MISSING_IN_STATEMENT,
            BreakType.MISSING_IN_LEDGER,
        }

    def test_will_not_match_across_currencies_or_directions(self) -> None:
        # Same magnitude, opposite direction: money in is not money out,
        # however neatly the numbers line up.
        result = reconcile([entry("L1", 2500)], [line("S1", -2500)])
        assert result.matches == []

        result = reconcile([entry("L1", 2500, currency="USD")], [line("S1", 2500, currency="EUR")])
        assert result.matches == []

    def test_ambiguity_is_resolved_by_total_cost_not_by_input_order(self) -> None:
        # Two ledger entries, two statement lines, all £10 and all inside the
        # window. Pairing by iteration order would give L1->S1 and L2->S2;
        # the assignment that minimises total date gap is L1->S2, L2->S1.
        ledger = [entry("L1", 1000, day=0), entry("L2", 1000, day=3)]
        statement = [line("S1", 1000, day=3), line("S2", 1000, day=0)]

        result = reconcile(ledger, statement)
        pairs = {(m.ledger_ids[0], m.statement_ids[0]) for m in result.matches}

        assert pairs == {("L1", "S2"), ("L2", "S1")}
        assert all(m.date_gap_days == 0 for m in result.matches)

    def test_the_result_does_not_depend_on_row_ordering(self) -> None:
        ledger = [entry("L1", 1000, day=0), entry("L2", 1000, day=1), entry("L3", 2000, day=1)]
        statement = [line("S1", 2000, day=1), line("S2", 1000, day=1), line("S3", 1000, day=0)]

        forward = reconcile(ledger, statement)
        reversed_ = reconcile(list(reversed(ledger)), list(reversed(statement)))

        assert _pairs(forward) == _pairs(reversed_)


class TestTolerance:
    def test_absorbs_a_fee_within_tolerance(self) -> None:
        result = reconcile(
            [entry("L1", 10_000)],
            [line("S1", 9_971)],
            MatchingConfig(amount_tolerance_minor=50),
        )

        assert len(result.matches) == 1
        assert result.matches[0].rule is MatchRule.WITHIN_TOLERANCE
        # The fee is reported, not silently swallowed.
        assert result.matches[0].delta_minor == -29

    def test_tolerance_is_off_unless_configured(self) -> None:
        # Defaulting to a non-zero tolerance would quietly reconcile genuine
        # discrepancies on every deployment that never touched the config.
        result = reconcile([entry("L1", 10_000)], [line("S1", 9_971)])
        assert result.matches == []

    def test_proportional_tolerance_scales_with_the_amount(self) -> None:
        config = MatchingConfig(amount_tolerance_bps=30)  # 0.30%

        big = reconcile([entry("L1", 1_000_000)], [line("S1", 997_500)], config)
        assert len(big.matches) == 1

        small = reconcile([entry("L1", 1_000)], [line("S1", 900)], config)
        assert small.matches == []


class TestBatchSettlement:
    def test_one_payout_line_settles_several_entries(self) -> None:
        # The normal shape of a processor payout. Without this pass the day
        # reconciles as one unexplained credit plus three missing payments.
        ledger = [entry("L1", 1000), entry("L2", 2500), entry("L3", 4000)]
        result = reconcile(ledger, [line("S1", 7500, day=1)])

        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.rule is MatchRule.BATCH_SETTLEMENT
        assert match.ledger_ids == ("L1", "L2", "L3")
        assert match.delta_minor == 0
        assert result.breaks == []

    def test_prefers_the_smaller_explanation(self) -> None:
        # 3000 can be made from L1+L2 or from L1+L3+L4. With enough small
        # numbers almost any total is reachable; the simpler answer is far
        # likelier to be the real one.
        ledger = [entry("L1", 1000), entry("L2", 2000), entry("L3", 500), entry("L4", 1500)]
        result = reconcile(ledger, [line("S1", 3000, day=1)])

        batch = next(m for m in result.matches if m.rule is MatchRule.BATCH_SETTLEMENT)
        assert len(batch.ledger_ids) == 2

    def test_leaves_the_line_alone_when_no_subset_adds_up(self) -> None:
        result = reconcile([entry("L1", 1000), entry("L2", 2500)], [line("S1", 9999, day=1)])

        assert result.matches == []
        assert any(b.type is BreakType.MISSING_IN_LEDGER for b in result.breaks)


# ------------------------------------------------------------- exceptions


class TestBreaks:
    def test_a_payment_the_bank_never_sent_is_reported(self) -> None:
        result = reconcile([entry("L1", 5000, ref="abc")], [])

        assert len(result.breaks) == 1
        assert result.breaks[0].type is BreakType.MISSING_IN_STATEMENT
        assert result.breaks[0].ledger_ids == ("L1",)

    def test_money_the_ledger_never_recorded_is_reported(self) -> None:
        result = reconcile([], [line("S1", 5000, description="unexpected credit")])

        assert len(result.breaks) == 1
        assert result.breaks[0].type is BreakType.MISSING_IN_LEDGER

    def test_a_duplicated_statement_line_cannot_absorb_a_second_payment(self) -> None:
        # The dangerous case: the bank credits us twice for one payment. If the
        # copy were left free to match, a second unrelated payment of the same
        # amount would pair with it and the double-credit would vanish.
        ledger = [entry("L1", 5000, ref="abc"), entry("L2", 5000, ref="xyz")]
        statement = [
            line("S1", 5000, ref="abc"),
            line("S2", 5000, ref="abc"),
        ]

        result = reconcile(ledger, statement)
        types = {b.type for b in result.breaks}

        assert BreakType.DUPLICATE in types
        assert BreakType.MISSING_IN_STATEMENT in types
        assert len(result.matches) == 1

    def test_a_late_settlement_is_matched_and_flagged(self) -> None:
        result = reconcile(
            [entry("L1", 5000, ref="abc")],
            [line("S1", 5000, ref="abc", day=5)],
            MatchingConfig(date_window_days=7, timing_break_after_days=2),
        )

        assert len(result.matches) == 1
        assert [b.type for b in result.breaks] == [BreakType.TIMING_DIFFERENCE]


# ------------------------------------------------------------- invariants


class TestInvariants:
    """Properties that must hold for any input at all.

    Example-based tests check the cases someone thought of. These check the
    ones nobody did -- which, for a matching engine, is where the double-count
    bugs live.
    """

    @staticmethod
    def _entries(draw_data: list[tuple[int, int]]) -> list[LedgerEntry]:
        return [entry(f"L{i}", amount, day=day) for i, (amount, day) in enumerate(draw_data)]

    @staticmethod
    def _lines(draw_data: list[tuple[int, int]]) -> list[StatementLine]:
        return [line(f"S{i}", amount, day=day) for i, (amount, day) in enumerate(draw_data)]

    rows = st.lists(
        st.tuples(
            st.integers(min_value=-500_000, max_value=500_000).filter(lambda n: n != 0),
            st.integers(min_value=0, max_value=6),
        ),
        max_size=8,
    )

    @settings(max_examples=250, deadline=None)
    @given(ledger_rows=rows, statement_rows=rows)
    def test_no_row_is_ever_used_twice(
        self, ledger_rows: list[tuple[int, int]], statement_rows: list[tuple[int, int]]
    ) -> None:
        # The cardinal sin of a matching engine: explaining one payment with
        # two others, so the books appear to balance while money is missing.
        result = reconcile(self._entries(ledger_rows), self._lines(statement_rows))

        used_ledger = [i for m in result.matches for i in m.ledger_ids]
        used_statement = [i for m in result.matches for i in m.statement_ids]

        assert len(used_ledger) == len(set(used_ledger))
        assert len(used_statement) == len(set(used_statement))

    @settings(max_examples=250, deadline=None)
    @given(ledger_rows=rows, statement_rows=rows)
    def test_every_row_is_either_matched_or_explained(
        self, ledger_rows: list[tuple[int, int]], statement_rows: list[tuple[int, int]]
    ) -> None:
        # Nothing may be silently dropped. A row the engine neither matched nor
        # raised is a row an operator will never learn about.
        ledger = self._entries(ledger_rows)
        statement = self._lines(statement_rows)
        result = reconcile(ledger, statement)

        accounted_ledger = result.matched_ledger_ids | {
            i for b in result.breaks for i in b.ledger_ids
        }
        accounted_statement = result.matched_statement_ids | {
            i for b in result.breaks for i in b.statement_ids
        }

        assert {e.id for e in ledger} <= accounted_ledger
        assert {s.id for s in statement} <= accounted_statement

    @settings(max_examples=250, deadline=None)
    @given(ledger_rows=rows, statement_rows=rows)
    def test_matched_pairs_actually_agree(
        self, ledger_rows: list[tuple[int, int]], statement_rows: list[tuple[int, int]]
    ) -> None:
        # Every match must survive re-derivation from the raw amounts. This is
        # what catches a sign error, which otherwise produces a beautifully
        # reconciled report of exactly the wrong pairs.
        ledger = {e.id: e for e in self._entries(ledger_rows)}
        statement = {s.id: s for s in self._lines(statement_rows)}
        result = reconcile(list(ledger.values()), list(statement.values()))

        for match in result.matches:
            ledger_total = sum(ledger[i].amount_minor for i in match.ledger_ids)
            statement_total = sum(statement[i].amount_minor for i in match.statement_ids)
            assert statement_total - ledger_total == match.delta_minor

    @settings(max_examples=150, deadline=None)
    @given(ledger_rows=rows, statement_rows=rows)
    def test_shuffling_the_input_does_not_change_the_answer(
        self, ledger_rows: list[tuple[int, int]], statement_rows: list[tuple[int, int]]
    ) -> None:
        ledger = self._entries(ledger_rows)
        statement = self._lines(statement_rows)

        assert _pairs(reconcile(ledger, statement)) == _pairs(
            reconcile(list(reversed(ledger)), list(reversed(statement)))
        )


def _pairs(result: object) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    return {(m.ledger_ids, m.statement_ids) for m in result.matches}  # type: ignore[attr-defined]
