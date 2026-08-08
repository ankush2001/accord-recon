"""Reading statement files, and manufacturing realistic ones.

Both halves live here because they are the same format seen from two sides: the
generator writes exactly what the parser must survive, so a change to one that
breaks the other fails immediately rather than at demo time.
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from accord.matching.types import StatementLine

#: Currencies with no minor unit. Rendering 1500 JPY as "15.00" would be wrong
#: by a factor of a hundred, and would reconcile against nothing.
ZERO_DECIMAL = frozenset({"JPY", "KRW", "CLP", "ISK", "VND", "XOF", "XAF"})

HEADERS = ("value_date", "reference", "description", "amount", "currency")


class StatementParseError(ValueError):
    """Raised with the offending line number, because a 1,200-line file needs one."""


def minor_units(amount: str, currency: str) -> int:
    """Parse a decimal amount into signed integer minor units.

    Via ``Decimal``, never ``float``. ``float("0.07") * 100`` is 7.000000000001
    and truncates to 6 -- a penny lost per line, which is precisely the class
    of error a reconciliation exists to catch and must not itself commit.
    """
    try:
        value = Decimal(amount.strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise StatementParseError(f"{amount!r} is not a number") from exc

    scale = 0 if currency.upper() in ZERO_DECIMAL else 2
    scaled = value.scaleb(scale)

    if scaled != scaled.to_integral_value():
        raise StatementParseError(
            f"{amount} has more precision than {currency} allows ({scale} decimal places)"
        )
    return int(scaled)


def format_amount(minor: int, currency: str) -> str:
    scale = 0 if currency.upper() in ZERO_DECIMAL else 2
    return str(Decimal(minor).scaleb(-scale))


def parse_statement_csv(content: bytes, statement_id: str = "pending") -> list[StatementLine]:
    """Parse a statement export.

    Expects ``value_date,reference,description,amount,currency``. Real bank
    exports vary wildly and a production importer would need a per-source
    adapter; the point of interest here is the matching, not the dialect
    zoo, so the format is fixed and strictly validated.
    """
    text = content.decode("utf-8-sig")  # Excel exports lead with a BOM
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        raise StatementParseError("the file is empty")

    missing = set(HEADERS) - {name.strip().lower() for name in reader.fieldnames}
    if missing:
        raise StatementParseError(f"missing column(s): {', '.join(sorted(missing))}")

    lines: list[StatementLine] = []
    for line_no, row in enumerate(reader, start=1):
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        try:
            currency = clean["currency"].upper()
            amount = minor_units(clean["amount"], currency)
            if amount == 0:
                # A zero-value line is not a movement of money; matching one
                # would be meaningless and it would pair with anything.
                raise StatementParseError("amount is zero")

            lines.append(
                StatementLine(
                    id=f"{statement_id}:{line_no}",
                    statement_id=statement_id,
                    line_no=line_no,
                    reference=clean["reference"] or None,
                    description=clean["description"],
                    currency=currency,
                    amount_minor=amount,
                    value_date=date.fromisoformat(clean["value_date"]),
                )
            )
        except StatementParseError as exc:
            raise StatementParseError(f"line {line_no}: {exc}") from exc
        except (KeyError, ValueError) as exc:
            raise StatementParseError(f"line {line_no}: {exc}") from exc

    return lines


# ----------------------------------------------------------- generation


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One deliberate flaw, and what it should be reported as."""

    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class GeneratedStatement:
    csv_bytes: bytes
    injected: list[Discrepancy]


def generate_statement(
    entries: list[tuple[str | None, int, date, str]],
    *,
    seed: int = 20260808,
    settlement_lag_days: int = 1,
    fee_bps: int = 0,
    drop_rate: float = 0.05,
    duplicate_rate: float = 0.03,
    phantom_count: int = 2,
    late_rate: float = 0.05,
) -> GeneratedStatement:
    """Build a statement from ledger entries, with faults deliberately injected.

    A generator that produces a perfectly matching file demonstrates nothing:
    every reconciliation would report zero breaks and the break classifier
    would never run. This one drops payments, duplicates them, shifts value
    dates, deducts fees and invents credits from nowhere -- so the demo
    exercises every path and the injected faults are the expected answer to
    check the engine against.

    ``entries`` are ``(reference, signed_minor, value_date, currency)``.
    """
    rng = random.Random(seed)
    injected: list[Discrepancy] = []
    rows: list[dict[str, str]] = []

    def emit(reference: str | None, amount: int, when: date, currency: str, note: str) -> None:
        rows.append(
            {
                "value_date": when.isoformat(),
                "reference": reference or "",
                "description": note,
                "amount": format_amount(amount, currency),
                "currency": currency,
            }
        )

    for reference, amount, value_date, currency in entries:
        if rng.random() < drop_rate:
            injected.append(
                Discrepancy("MISSING_IN_STATEMENT", f"{reference}: never sent by the bank")
            )
            continue

        settled_on = value_date + timedelta(days=settlement_lag_days)
        if rng.random() < late_rate:
            extra = rng.randint(3, 9)
            settled_on += timedelta(days=extra)
            injected.append(
                Discrepancy("TIMING_DIFFERENCE", f"{reference}: settled {extra} days late")
            )

        settled_amount = amount
        if fee_bps and amount > 0:
            fee = amount * fee_bps // 10_000
            if fee:
                settled_amount = amount - fee
                injected.append(Discrepancy("FEE", f"{reference}: {fee} deducted in transit"))

        emit(reference, settled_amount, settled_on, currency, "settlement")

        if rng.random() < duplicate_rate:
            emit(reference, settled_amount, settled_on, currency, "settlement")
            injected.append(Discrepancy("DUPLICATE", f"{reference}: credited twice"))

    currency = entries[0][3] if entries else "USD"
    base_date = entries[0][2] if entries else date.today()
    for i in range(phantom_count):
        emit(
            f"BANKFEE{i:03d}",
            -rng.randint(150, 2_500),
            base_date + timedelta(days=rng.randint(0, 5)),
            currency,
            "bank charge",
        )
        injected.append(
            Discrepancy("MISSING_IN_LEDGER", f"BANKFEE{i:03d}: charge the ledger never saw")
        )

    # Banks do not sort by our transfer order.
    rng.shuffle(rows)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(HEADERS))
    writer.writeheader()
    writer.writerows(rows)

    return GeneratedStatement(buffer.getvalue().encode(), injected)
