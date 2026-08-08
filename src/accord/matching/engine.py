"""The matching engine.

Pure functions over the dataclasses in :mod:`accord.matching.types`. No
database, no clock, no configuration read from the environment -- the same
inputs always produce the same output, which is the only way a reconciliation
can be re-run months later and defended.

The engine works in passes, most certain first. Each pass may only consume rows
no earlier pass claimed, so a confident reference match is never stolen by a
speculative amount-and-date guess:

    1. duplicates            -- flagged before anything is matched
    2. exact reference       -- same reference, same amount
    3. amount and date       -- identical amount inside the window
    4. within tolerance      -- amount differs by no more than a fee
    5. batch settlement      -- one statement line, several ledger entries
    6. whatever is left      -- classified as breaks

Passes 3 and 4 can produce ambiguity: three £10 payments on Tuesday and three
£10 statement lines on Wednesday are mutually compatible in nine ways. Picking
greedily by iteration order would make the result depend on row ordering, so
those passes resolve each ambiguous cluster as a minimum-cost assignment
instead. See :func:`_resolve_cluster`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import combinations

from accord.matching.types import (
    Break,
    BreakType,
    LedgerEntry,
    Match,
    MatchingConfig,
    MatchRule,
    ReconResult,
    Side,
    StatementLine,
)

# Bank references arrive mangled in ways that carry no meaning: padded, cased
# differently, punctuated by whatever system last touched them. Normalising
# before comparison is the difference between a 60% and a 95% match rate on
# real data, and costs nothing.
_NOISE = re.compile(r"[^A-Z0-9]")


def normalise_reference(reference: str | None) -> str | None:
    """Strip formatting noise so two spellings of one reference compare equal."""
    if not reference:
        return None
    cleaned = _NOISE.sub("", reference.upper())
    return cleaned or None


def reconcile(
    ledger: Sequence[LedgerEntry],
    statement: Sequence[StatementLine],
    config: MatchingConfig | None = None,
) -> ReconResult:
    """Reconcile one account's ledger entries against one statement."""
    config = config or MatchingConfig()

    # Sorted up front so that every downstream tie-break is decided by a stable
    # key rather than by whatever order the rows arrived in.
    ledger = sorted(ledger, key=lambda e: (e.occurred_at, e.id))
    statement = sorted(statement, key=lambda line: (line.value_date, line.line_no, line.id))

    matches: list[Match] = []
    breaks: list[Break] = []

    open_ledger = {e.id: e for e in ledger}
    open_statement = {line.id: line for line in statement}

    _flag_duplicates(ledger, statement, breaks, open_ledger, open_statement)
    _match_exact_reference(open_ledger, open_statement, config, matches, breaks)
    _match_amount_and_date(open_ledger, open_statement, config, matches)
    _match_within_tolerance(open_ledger, open_statement, config, matches)
    _match_batch_settlements(open_ledger, open_statement, config, matches)

    _flag_timing_differences(matches, config, breaks)
    _classify_remaining(open_ledger, open_statement, breaks)

    return ReconResult(
        matches=matches,
        breaks=breaks,
        ledger_count=len(ledger),
        statement_count=len(statement),
    )


# --------------------------------------------------------------- pass 1


def _flag_duplicates(
    ledger: Sequence[LedgerEntry],
    statement: Sequence[StatementLine],
    breaks: list[Break],
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
) -> None:
    """Report rows that appear twice on the same side, and withhold the copies.

    Run before any matching, deliberately. A duplicated statement line would
    otherwise happily match a second, unrelated ledger entry of the same
    amount, and the run would report a clean reconciliation over a genuine
    double-credit. The first occurrence stays available to match; the rest are
    held back and reported.
    """
    seen_ledger: dict[tuple[str | None, int, date], str] = {}
    for entry in ledger:
        reference = normalise_reference(entry.external_id)
        if reference is None:
            # Without a reference, two identical amounts on the same day are
            # ordinary business, not evidence of duplication.
            continue
        key = (reference, entry.amount_minor, entry.value_date)
        if key in seen_ledger:
            open_ledger.pop(entry.id, None)
            breaks.append(
                Break(
                    type=BreakType.DUPLICATE,
                    side=Side.LEDGER,
                    ledger_ids=(seen_ledger[key], entry.id),
                    amount_minor=entry.amount_minor,
                    currency=entry.currency,
                    detail=(
                        f"reference {entry.external_id} posted twice for "
                        f"{entry.amount_minor} on {entry.value_date}"
                    ),
                )
            )
        else:
            seen_ledger[key] = entry.id

    seen_statement: dict[tuple[str | None, int, date], str] = {}
    for line in statement:
        reference = normalise_reference(line.reference)
        if reference is None:
            continue
        key = (reference, line.amount_minor, line.value_date)
        if key in seen_statement:
            open_statement.pop(line.id, None)
            breaks.append(
                Break(
                    type=BreakType.DUPLICATE,
                    side=Side.STATEMENT,
                    statement_ids=(seen_statement[key], line.id),
                    amount_minor=line.amount_minor,
                    currency=line.currency,
                    detail=(
                        f"reference {line.reference} appears twice on the statement for "
                        f"{line.amount_minor} on {line.value_date}"
                    ),
                )
            )
        else:
            seen_statement[key] = line.id


# --------------------------------------------------------------- pass 2


def _match_exact_reference(
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
    config: MatchingConfig,
    matches: list[Match],
    breaks: list[Break],
) -> None:
    """Match on a shared reference.

    A shared reference is an assertion by both systems that these are the same
    payment, so when the amounts then disagree the answer is not "no match" --
    it is an amount mismatch, which is a far more actionable finding. Both rows
    are consumed either way; leaving them open would let a later pass pair them
    with something else and hide the discrepancy.
    """
    by_reference: dict[tuple[str, str], list[StatementLine]] = defaultdict(list)
    for line in open_statement.values():
        reference = normalise_reference(line.reference)
        if reference:
            by_reference[(reference, line.currency)].append(line)

    for entry in sorted(open_ledger.values(), key=lambda e: e.id):
        reference = normalise_reference(entry.external_id)
        if not reference:
            continue

        candidates = [
            line
            for line in by_reference.get((reference, entry.currency), [])
            if line.id in open_statement
        ]
        if not candidates:
            continue

        # Prefer the candidate that also agrees on amount; only then the nearest
        # in time. Without the amount term a reference reused across two
        # payments would resolve by date alone.
        line = min(
            candidates,
            key=lambda c: (abs(c.amount_minor - entry.amount_minor), _gap(entry, c), c.id),
        )
        delta = line.amount_minor - entry.amount_minor

        if abs(delta) <= config.tolerance_for(entry.amount_minor):
            matches.append(
                Match(
                    rule=MatchRule.EXACT_REFERENCE,
                    ledger_ids=(entry.id,),
                    statement_ids=(line.id,),
                    delta_minor=delta,
                    date_gap_days=_gap(entry, line),
                    confidence=1.0 if delta == 0 else 0.95,
                )
            )
        else:
            breaks.append(
                Break(
                    type=BreakType.AMOUNT_MISMATCH,
                    side=Side.LEDGER,
                    ledger_ids=(entry.id,),
                    statement_ids=(line.id,),
                    amount_minor=delta,
                    currency=entry.currency,
                    detail=(
                        f"reference {entry.external_id}: ledger {entry.amount_minor} "
                        f"vs statement {line.amount_minor} (difference {delta})"
                    ),
                )
            )

        del open_ledger[entry.id]
        del open_statement[line.id]


# ----------------------------------------------------------- passes 3 & 4


def _match_amount_and_date(
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
    config: MatchingConfig,
    matches: list[Match],
) -> None:
    """Identical amounts inside the date window, resolved as an assignment."""
    _match_by_candidates(
        open_ledger,
        open_statement,
        config,
        matches,
        rule=MatchRule.AMOUNT_AND_DATE,
        tolerance=lambda _amount: 0,
        confidence=0.85,
    )


def _match_within_tolerance(
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
    config: MatchingConfig,
    matches: list[Match],
) -> None:
    """Amounts differing by no more than a fee, inside the date window."""
    if config.amount_tolerance_minor == 0 and config.amount_tolerance_bps == 0:
        return
    _match_by_candidates(
        open_ledger,
        open_statement,
        config,
        matches,
        rule=MatchRule.WITHIN_TOLERANCE,
        tolerance=config.tolerance_for,
        confidence=0.7,
    )


def _match_by_candidates(
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
    config: MatchingConfig,
    matches: list[Match],
    *,
    rule: MatchRule,
    tolerance: Callable[[int], int],
    confidence: float,
) -> None:
    """Build compatible pairs, then choose between them optimally.

    Compatibility is cheap and generous; the choosing is where the care goes.
    Three identical payments against three identical statement lines are
    compatible nine ways, and picking greedily would make the output depend on
    input ordering -- a reconciliation that changes when you re-sort the CSV is
    not a reconciliation.
    """
    candidates: list[tuple[str, str, int, int]] = []
    for entry in open_ledger.values():
        for line in open_statement.values():
            if entry.currency != line.currency:
                continue
            # Direction must agree. Both sides use "money into this account is
            # positive", so a credit can never be the same event as a debit,
            # however well the magnitudes line up.
            if (entry.amount_minor > 0) != (line.amount_minor > 0):
                continue
            gap = _gap(entry, line)
            if gap > config.date_window_days:
                continue
            delta = line.amount_minor - entry.amount_minor
            if abs(delta) > tolerance(entry.amount_minor):
                continue
            candidates.append((entry.id, line.id, delta, gap))

    if not candidates:
        return

    for ledger_id, statement_id, delta, gap in _resolve_clusters(candidates):
        matches.append(
            Match(
                rule=rule,
                ledger_ids=(ledger_id,),
                statement_ids=(statement_id,),
                delta_minor=delta,
                date_gap_days=gap,
                confidence=confidence,
            )
        )
        del open_ledger[ledger_id]
        del open_statement[statement_id]


def _resolve_clusters(
    candidates: Sequence[tuple[str, str, int, int]],
) -> list[tuple[str, str, int, int]]:
    """Split candidate pairs into independent clusters and solve each.

    Clusters are the connected components of the bipartite candidate graph.
    They are almost always tiny -- one ledger entry against one statement line
    -- so solving each in isolation keeps an exact search affordable even when
    the day's file is large.
    """
    adjacency: dict[str, set[str]] = defaultdict(set)
    for ledger_id, statement_id, _delta, _gap in candidates:
        adjacency[f"L{ledger_id}"].add(f"S{statement_id}")
        adjacency[f"S{statement_id}"].add(f"L{ledger_id}")

    seen: set[str] = set()
    chosen: list[tuple[str, str, int, int]] = []

    for node in sorted(adjacency):
        if node in seen:
            continue
        component = _component(node, adjacency, seen)
        cluster = [
            c for c in candidates if f"L{c[0]}" in component and f"S{c[1]}" in component
        ]
        chosen.extend(_resolve_cluster(cluster))

    return chosen


def _component(start: str, adjacency: dict[str, set[str]], seen: set[str]) -> set[str]:
    """Breadth-first walk of one connected component."""
    component = {start}
    seen.add(start)
    queue = [start]
    while queue:
        node = queue.pop()
        for neighbour in adjacency[node]:
            if neighbour not in seen:
                seen.add(neighbour)
                component.add(neighbour)
                queue.append(neighbour)
    return component


def _resolve_cluster(
    cluster: Sequence[tuple[str, str, int, int]],
) -> list[tuple[str, str, int, int]]:
    """Choose a set of non-overlapping pairs with the lowest total cost.

    Cost prefers, in order: the smallest amount difference, then the smallest
    date gap. An exact search runs when the cluster is small enough -- which it
    nearly always is -- and a deterministic greedy pass takes over beyond that,
    because a reconciliation must finish before the operator's morning does.
    """
    ledger_ids = sorted({c[0] for c in cluster})
    statement_ids = sorted({c[1] for c in cluster})

    # 10 on the smaller side means at most ~3.6M states, which runs in
    # milliseconds. Real clusters are 1-3; anything larger usually means the
    # tolerances are too loose rather than that the data is genuinely ambiguous.
    if min(len(ledger_ids), len(statement_ids)) > 10:
        return _greedy(cluster)

    by_pair = {(c[0], c[1]): c for c in cluster}
    memo: dict[tuple[int, frozenset[str]], _Solution] = {}

    def cost(candidate: tuple[str, str, int, int]) -> int:
        _l, _s, delta, gap = candidate
        # Amount agreement dominates: a penny apart is a worse match than three
        # days apart, and scaling the date term keeps it from ever overtaking.
        return abs(delta) * 1000 + gap

    def search(index: int, used: frozenset[str]) -> _Solution:
        if index == len(ledger_ids):
            return _Solution(0, 0, [])
        key = (index, used)
        if key in memo:
            return memo[key]

        # Leaving a ledger entry unmatched is always allowed; it simply becomes
        # a break, which is an honest outcome rather than a failure.
        best = search(index + 1, used)

        for statement_id in statement_ids:
            if statement_id in used:
                continue
            candidate = by_pair.get((ledger_ids[index], statement_id))
            if candidate is None:
                continue
            rest = search(index + 1, used | {statement_id})
            option = _Solution(
                matched=rest.matched + 1,
                cost=rest.cost + cost(candidate),
                pairs=[candidate, *rest.pairs],
            )
            # Explaining more rows always wins; total cost only breaks ties.
            # A cheaper solution that leaves two payments unreconciled is not
            # the better answer -- it just has fewer numbers in it.
            if (-option.matched, option.cost) < (-best.matched, best.cost):
                best = option

        memo[key] = best
        return best

    return sorted(search(0, frozenset()).pairs)


@dataclass(frozen=True, slots=True)
class _Solution:
    """A partial assignment: how many rows it explains and what that cost."""

    matched: int
    cost: int
    pairs: list[tuple[str, str, int, int]]


def _greedy(
    cluster: Sequence[tuple[str, str, int, int]],
) -> list[tuple[str, str, int, int]]:
    """Deterministic fallback for clusters too large to search exactly."""
    taken_ledger: set[str] = set()
    taken_statement: set[str] = set()
    chosen: list[tuple[str, str, int, int]] = []

    for candidate in sorted(cluster, key=lambda c: (abs(c[2]), c[3], c[0], c[1])):
        ledger_id, statement_id, _delta, _gap = candidate
        if ledger_id in taken_ledger or statement_id in taken_statement:
            continue
        taken_ledger.add(ledger_id)
        taken_statement.add(statement_id)
        chosen.append(candidate)

    return sorted(chosen)


# --------------------------------------------------------------- pass 5


def _match_batch_settlements(
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
    config: MatchingConfig,
    matches: list[Match],
) -> None:
    """One statement line settling several ledger entries at once.

    The ordinary shape of a payout: a processor takes a day of transactions and
    sends one lump sum. Without this pass every such day reconciles as one
    unexplained credit plus a hundred missing payments, which buries the
    genuine exceptions under noise.

    Finding which entries make up the total is subset-sum, so it is bounded
    twice -- by how many entries may combine, and by how many are considered at
    all -- and the search returns the first exact hit rather than enumerating
    every possibility.
    """
    ordered = sorted(open_statement.values(), key=lambda line: (-abs(line.amount_minor), line.id))
    for line in ordered:
        pool = [
            entry
            for entry in open_ledger.values()
            if entry.currency == line.currency
            and (entry.amount_minor > 0) == (line.amount_minor > 0)
            and _gap(entry, line) <= config.date_window_days
            and abs(entry.amount_minor) <= abs(line.amount_minor)
        ]
        if len(pool) < 2:
            continue

        # Largest first: a batch is usually made of a few big entries and some
        # small ones, and starting from the big ones prunes far sooner.
        pool.sort(key=lambda e: (-abs(e.amount_minor), e.id))
        pool = pool[:25]

        subset = _subset_summing_to(pool, line.amount_minor, config.max_batch_size)
        if subset is None:
            continue

        gap = max(_gap(entry, line) for entry in subset)
        matches.append(
            Match(
                rule=MatchRule.BATCH_SETTLEMENT,
                ledger_ids=tuple(sorted(entry.id for entry in subset)),
                statement_ids=(line.id,),
                delta_minor=0,
                date_gap_days=gap,
                confidence=0.75,
            )
        )
        for entry in subset:
            del open_ledger[entry.id]
        del open_statement[line.id]


def _subset_summing_to(
    pool: Sequence[LedgerEntry], target: int, max_size: int
) -> list[LedgerEntry] | None:
    """Find entries summing exactly to ``target``, or None.

    Sizes are tried smallest first, so a two-entry explanation is preferred
    over a seven-entry coincidence -- with enough small numbers almost any
    total can be reached, and the simpler answer is far likelier to be the real
    one.
    """
    limit = min(max_size, len(pool))
    for size in range(2, limit + 1):
        for combination in combinations(pool, size):
            if sum(entry.amount_minor for entry in combination) == target:
                return list(combination)
    return None


# --------------------------------------------------------------- pass 6


def _flag_timing_differences(
    matches: Iterable[Match], config: MatchingConfig, breaks: list[Break]
) -> None:
    """Note matches that landed late. The match stands; the delay is reported."""
    for match in matches:
        if match.date_gap_days > config.timing_break_after_days:
            breaks.append(
                Break(
                    type=BreakType.TIMING_DIFFERENCE,
                    side=Side.STATEMENT,
                    ledger_ids=match.ledger_ids,
                    statement_ids=match.statement_ids,
                    amount_minor=match.delta_minor,
                    detail=(
                        f"matched, but {match.date_gap_days} days apart "
                        f"(expected within {config.timing_break_after_days})"
                    ),
                )
            )


def _classify_remaining(
    open_ledger: dict[str, LedgerEntry],
    open_statement: dict[str, StatementLine],
    breaks: list[Break],
) -> None:
    """Everything nothing could explain."""
    for entry in sorted(open_ledger.values(), key=lambda e: e.id):
        breaks.append(
            Break(
                type=BreakType.MISSING_IN_STATEMENT,
                side=Side.LEDGER,
                ledger_ids=(entry.id,),
                amount_minor=entry.amount_minor,
                currency=entry.currency,
                detail=(
                    f"ledger posted {entry.amount_minor} on {entry.value_date}"
                    f"{f' (ref {entry.external_id})' if entry.external_id else ''} "
                    "with nothing on the statement to match it"
                ),
            )
        )

    for line in sorted(open_statement.values(), key=lambda line: line.id):
        breaks.append(
            Break(
                type=BreakType.MISSING_IN_LEDGER,
                side=Side.STATEMENT,
                statement_ids=(line.id,),
                amount_minor=line.amount_minor,
                currency=line.currency,
                detail=(
                    f"statement shows {line.amount_minor} on {line.value_date} "
                    f"({line.description}) that the ledger never recorded"
                ),
            )
        )


def _gap(entry: LedgerEntry, line: StatementLine) -> int:
    return abs((line.value_date - entry.value_date).days)
