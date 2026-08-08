"""The vocabulary of a reconciliation, independent of storage.

Everything the matcher works with is defined here as a frozen dataclass with no
database, no ORM and no I/O. That is what lets the engine be tested with
property-based tests over generated inputs -- and it is why the matching rules
can be reasoned about at all, since a rule that also loads rows is a rule you
cannot read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Side(StrEnum):
    """Which record a row came from."""

    LEDGER = "LEDGER"
    STATEMENT = "STATEMENT"


class MatchRule(StrEnum):
    """How a match was arrived at, ordered from most to least certain.

    The rule is stored on every match, not just used to make it. An auditor
    asking "why does the system believe these two rows are the same payment?"
    gets an answer, and a rule that turns out to be too loose can be found and
    reviewed after the fact.
    """

    #: Same reference, same amount, same currency. Not really an inference.
    EXACT_REFERENCE = "EXACT_REFERENCE"
    #: No shared reference, but an identical amount inside the date window.
    AMOUNT_AND_DATE = "AMOUNT_AND_DATE"
    #: Amount differs by no more than the configured tolerance -- typically a
    #: fee the intermediary deducted before the money arrived.
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    #: One statement line settles several ledger entries at once. The normal
    #: shape of a payout: a processor pays one lump sum for the day's takings.
    BATCH_SETTLEMENT = "BATCH_SETTLEMENT"


class BreakType(StrEnum):
    """Why a row could not be matched.

    The classification is the actual product of a reconciliation. "1,412 rows
    did not match" tells an operator nothing; "3 payments the bank never sent
    us, 1 duplicate, 12 arrived late" tells them what to do this morning.
    """

    #: The ledger recorded money moving; the bank statement has no such line.
    #: The most serious of these -- it can mean a payment that never landed.
    MISSING_IN_STATEMENT = "MISSING_IN_STATEMENT"
    #: The bank shows money the ledger knows nothing about. Unexpected credits,
    #: fees taken directly, or a posting the ledger failed to record.
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
    #: Matched confidently on reference, but the amounts disagree beyond
    #: tolerance.
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    #: The same payment appears twice on one side.
    DUPLICATE = "DUPLICATE"
    #: Matched, but outside the expected settlement window -- money that
    #: arrived, late. Usually benign, occasionally the first sign of a
    #: struggling counterparty.
    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A settled posting, as the ledger reported it.

    Amounts are signed integer minor units, matching obol-ledger exactly: a
    debit is positive, a credit negative. Converting to decimals at the
    boundary would reintroduce precisely the rounding the ledger avoids.
    """

    id: str
    transfer_id: str
    external_id: str | None
    account_code: str
    currency: str
    amount_minor: int
    occurred_at: datetime

    @property
    def value_date(self) -> date:
        return self.occurred_at.date()

    @property
    def magnitude(self) -> int:
        return abs(self.amount_minor)


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One line of an external statement -- the counterparty's version of events."""

    id: str
    statement_id: str
    line_no: int
    reference: str | None
    description: str
    currency: str
    amount_minor: int
    value_date: date

    @property
    def magnitude(self) -> int:
        return abs(self.amount_minor)


@dataclass(frozen=True, slots=True)
class Match:
    """A conclusion: these ledger entries and these statement lines are the same money.

    Both sides are lists because matching is not always one-to-one -- a batch
    settlement is many ledger entries against a single statement line.
    """

    rule: MatchRule
    ledger_ids: tuple[str, ...]
    statement_ids: tuple[str, ...]
    #: Statement total minus ledger total, in minor units. Zero for an exact
    #: match; a fee shows up here rather than being quietly absorbed.
    delta_minor: int
    #: Days between the two sides. Non-zero is normal; large is a warning.
    date_gap_days: int
    #: 0.0-1.0. Not a probability -- a ranking, used to resolve competing
    #: candidates deterministically.
    confidence: float

    @property
    def is_one_to_one(self) -> bool:
        return len(self.ledger_ids) == 1 and len(self.statement_ids) == 1


@dataclass(frozen=True, slots=True)
class Break:
    """An exception a human has to look at."""

    type: BreakType
    side: Side
    ledger_ids: tuple[str, ...] = ()
    statement_ids: tuple[str, ...] = ()
    amount_minor: int = 0
    currency: str = ""
    #: Written for the person who has to act on it, not for a log file.
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MatchingConfig:
    """The knobs, all in one place and all recorded with each run.

    A reconciliation whose thresholds are not stored alongside its results
    cannot be re-run or defended later: the same inputs would produce different
    output as soon as someone nudged a tolerance.
    """

    #: How far apart two dates may be and still be the same payment. Card
    #: settlement typically lands one or two business days after the sale.
    date_window_days: int = 3
    #: Absolute amount difference tolerated when nothing else distinguishes a
    #: pair -- a processor fee deducted in transit.
    amount_tolerance_minor: int = 0
    #: Same, as a fraction of the amount. The larger of the two applies, so a
    #: percentage tolerance does not accidentally shrink to nothing on small
    #: payments.
    amount_tolerance_bps: int = 0
    #: Beyond this gap a match is still made but flagged as a timing break.
    timing_break_after_days: int = 2
    #: Cap on how many ledger entries may be combined into one batch match.
    #: Subset-sum is exponential in the worst case; this is what stops a
    #: pathological input from hanging the run.
    max_batch_size: int = 12

    def tolerance_for(self, amount_minor: int) -> int:
        """The tolerance that applies to a specific amount."""
        proportional = abs(amount_minor) * self.amount_tolerance_bps // 10_000
        return max(self.amount_tolerance_minor, proportional)


@dataclass(frozen=True, slots=True)
class ReconResult:
    """Everything one run concluded."""

    matches: list[Match] = field(default_factory=list)
    breaks: list[Break] = field(default_factory=list)
    ledger_count: int = 0
    statement_count: int = 0

    @property
    def matched_ledger_ids(self) -> set[str]:
        return {i for m in self.matches for i in m.ledger_ids}

    @property
    def matched_statement_ids(self) -> set[str]:
        return {i for m in self.matches for i in m.statement_ids}

    @property
    def match_rate(self) -> float:
        total = self.ledger_count + self.statement_count
        if total == 0:
            return 1.0
        matched = len(self.matched_ledger_ids) + len(self.matched_statement_ids)
        return matched / total
