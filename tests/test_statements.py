"""Parsing statement files, and the generator that produces them."""

from __future__ import annotations

from datetime import date

import pytest

from accord.ingest.statements import (
    StatementParseError,
    format_amount,
    generate_statement,
    minor_units,
    parse_statement_csv,
)

HEADER = b"value_date,reference,description,amount,currency\n"


class TestAmountParsing:
    @pytest.mark.parametrize(
        ("amount", "currency", "expected"),
        [
            ("12.34", "USD", 1234),
            ("-12.34", "USD", -1234),
            ("0.07", "USD", 7),
            ("1,250.00", "USD", 125000),
            ("1234", "JPY", 1234),
            ("1500", "CLP", 1500),
        ],
    )
    def test_decimals_become_minor_units(self, amount: str, currency: str, expected: int) -> None:
        assert minor_units(amount, currency) == expected

    def test_parsing_goes_through_decimal_not_float(self) -> None:
        # float("0.07") * 100 is 7.000000000001 and truncates to 6 -- a penny
        # lost per line. A reconciliation engine that introduces the very error
        # it exists to catch would be worse than none.
        assert minor_units("0.07", "USD") == 7
        assert minor_units("0.29", "USD") == 29
        assert minor_units("1.15", "USD") == 115

    def test_refuses_precision_the_currency_does_not_have(self) -> None:
        with pytest.raises(StatementParseError, match="more precision"):
            minor_units("1.005", "USD")
        with pytest.raises(StatementParseError, match="more precision"):
            minor_units("1500.50", "JPY")

    @pytest.mark.parametrize(
        ("minor", "currency", "expected"),
        [(1234, "USD", "12.34"), (-500, "USD", "-5.00"), (1234, "JPY", "1234")],
    )
    def test_formatting_round_trips(self, minor: int, currency: str, expected: str) -> None:
        assert format_amount(minor, currency) == expected
        assert minor_units(expected, currency) == minor


class TestCsvParsing:
    def test_parses_a_well_formed_file(self) -> None:
        content = HEADER + b"2026-08-03,PSP-1,settlement,125.00,USD\n"
        lines = parse_statement_csv(content, statement_id="s1")

        assert len(lines) == 1
        assert lines[0].reference == "PSP-1"
        assert lines[0].amount_minor == 12500
        assert lines[0].value_date == date(2026, 8, 3)

    def test_survives_an_excel_byte_order_mark(self) -> None:
        # Excel prefixes exports with a BOM, which turns the first header into
        # "﻿value_date" and makes the column look absent.
        content = b"\xef\xbb\xbf" + HEADER + b"2026-08-03,A,x,1.00,USD\n"
        assert len(parse_statement_csv(content)) == 1

    def test_reports_the_offending_line_number(self) -> None:
        content = HEADER + b"2026-08-03,A,x,1.00,USD\n2026-08-04,B,x,not-a-number,USD\n"

        with pytest.raises(StatementParseError, match="line 2"):
            parse_statement_csv(content)

    def test_rejects_a_missing_column(self) -> None:
        with pytest.raises(StatementParseError, match="currency"):
            parse_statement_csv(b"value_date,reference,description,amount\n")

    def test_rejects_a_zero_value_line(self) -> None:
        # Zero is not a movement of money, and it would match anything.
        with pytest.raises(StatementParseError, match="zero"):
            parse_statement_csv(HEADER + b"2026-08-03,A,x,0.00,USD\n")


class TestGenerator:
    entries = [
        (f"REF-{i:03d}", 1000 + i * 250, date(2026, 8, 3), "USD") for i in range(40)
    ]

    def test_output_is_parseable_by_the_parser(self) -> None:
        # The two halves must agree. If the generator can produce a file the
        # parser rejects, the demo breaks at exactly the wrong moment.
        generated = generate_statement(self.entries, seed=1)
        lines = parse_statement_csv(generated.csv_bytes)
        assert len(lines) > 0

    def test_is_deterministic_for_a_seed(self) -> None:
        first = generate_statement(self.entries, seed=99)
        second = generate_statement(self.entries, seed=99)
        assert first.csv_bytes == second.csv_bytes

    def test_actually_injects_the_faults_it_reports(self) -> None:
        generated = generate_statement(
            self.entries, seed=7, drop_rate=0.2, duplicate_rate=0.2, phantom_count=3
        )
        kinds = {d.kind for d in generated.injected}

        # A generator that produced a clean file would make the demo prove
        # nothing: every run would report zero breaks and the classifier would
        # never execute.
        assert "MISSING_IN_STATEMENT" in kinds
        assert "DUPLICATE" in kinds
        assert "MISSING_IN_LEDGER" in kinds

    def test_fees_are_deducted_when_configured(self) -> None:
        generated = generate_statement(
            self.entries, seed=3, fee_bps=250, drop_rate=0, duplicate_rate=0,
            phantom_count=0, late_rate=0,
        )
        lines = parse_statement_csv(generated.csv_bytes)
        by_reference = {line.reference: line for line in lines}

        for reference, amount, _day, _currency in self.entries:
            assert by_reference[reference].amount_minor < amount
