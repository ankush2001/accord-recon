#!/usr/bin/env python3
"""End-to-end demonstration across both services.

Drives obol-ledger to produce real settled payments, manufactures a bank
statement from them with faults deliberately injected, reconciles the two, and
then checks that every injected fault was actually found.

That last step is the point. A demo that prints "17 breaks detected" proves
nothing -- it might have detected the wrong seventeen. This one knows exactly
what it broke and verifies the engine reported it.

    ./scripts/demo.py [--ledger URL] [--accord URL]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import date, datetime, timedelta

sys.path.insert(0, str(__file__.rsplit("/scripts/", 1)[0] + "/src"))

from accord.ingest.statements import generate_statement

LEDGER = "http://localhost:8080"
ACCORD = "http://localhost:8000"
BANK_ACCOUNT = "asset:bank"
PAYMENTS = 40


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def request(method: str, url: str, body: object = None, headers: dict | None = None) -> object:
    data = None
    all_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise SystemExit(f"{method} {url} failed with {exc.code}: {detail}") from exc


# ------------------------------------------------------------- the ledger


def build_ledger_history(ledger: str) -> str:
    """Create accounts and settle a few dozen payments into the bank account."""
    run = int(time.time())
    customers = f"liability:customers:{run}"

    for code, kind, negative in (
        (BANK_ACCOUNT, "ASSET", True),
        (customers, "LIABILITY", False),
    ):
        # The bank account survives between demo runs; only the customer pool
        # is unique. Already existing is the expected case, not an error.
        with contextlib.suppress(SystemExit):
            request("POST", f"{ledger}/v1/accounts", {
                "code": code, "name": code, "currency": "USD",
                "type": kind, "allowNegative": negative,
            })

    print(f"settling {PAYMENTS} payments through the ledger ...")
    for i in range(PAYMENTS):
        reference = f"PSP-{run}-{i:04d}"
        amount = 1_500 + (i * 337) % 48_500
        request(
            "POST",
            f"{ledger}/v1/transfers",
            {
                "currency": "USD",
                "externalId": reference,
                "description": "customer payment",
                "legs": [
                    {"accountCode": BANK_ACCOUNT, "direction": "DEBIT", "amountMinor": amount},
                    {"accountCode": customers, "direction": "CREDIT", "amountMinor": amount},
                ],
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    return customers


def wait_for_ingestion(accord: str, expected: int, timeout: float = 60.0) -> list[dict]:
    """Wait for the ledger's outbox relay to deliver the postings.

    Polls rather than sleeping a fixed interval: the relay's cycle is
    configurable, and a hard-coded sleep is either flaky or slow.
    """
    print("waiting for the outbox relay to deliver them ...", end="", flush=True)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        entries = request("GET", f"{accord}/v1/ledger/entries?limit=1000")
        assert isinstance(entries, list)
        if len(entries) >= expected:
            print(f" {len(entries)} received")
            return entries
        print(".", end="", flush=True)
        time.sleep(2)

    entries = request("GET", f"{accord}/v1/ledger/entries?limit=1000")
    assert isinstance(entries, list)
    print(f" only {len(entries)} of {expected} arrived")
    print(red("  Is OUTBOX_TARGET_URL set on the ledger? See the README."))
    return entries


# ---------------------------------------------------------- the statement


def upload_statement(accord: str, csv_bytes: bytes) -> str:
    """Upload the generated CSV as multipart, without pulling in a HTTP client."""
    boundary = "----accorddemo" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="statement.csv"\r\n',
        b"Content-Type: text/csv\r\n\r\n",
        csv_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ])

    req = urllib.request.Request(
        f"{accord}/v1/statements?source=demo-bank",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=LEDGER)
    parser.add_argument("--accord", default=ACCORD)
    args = parser.parse_args()

    print(bold("\n1. Settle real payments in obol-ledger"))
    build_ledger_history(args.ledger)

    print(bold("\n2. Let accord ingest them from the outbox"))
    entries = wait_for_ingestion(args.accord, PAYMENTS)
    if not entries:
        return 1

    print(bold("\n3. Manufacture a bank statement, with faults injected"))
    rows = [
        (
            e["externalId"],
            e["amountMinor"],
            datetime.fromisoformat(e["occurredAt"]).date(),
            e["currency"],
        )
        for e in entries
    ]
    generated = generate_statement(
        rows,
        settlement_lag_days=1,
        fee_bps=0,
        drop_rate=0.08,
        duplicate_rate=0.05,
        phantom_count=3,
        late_rate=0.08,
    )

    injected = Counter(d.kind for d in generated.injected)
    for kind, count in sorted(injected.items()):
        print(f"   injected {count:>2}  {kind}")

    statement_id = upload_statement(args.accord, generated.csv_bytes)
    line_count = sum(1 for _ in csv.DictReader(io.StringIO(generated.csv_bytes.decode())))
    print(f"   uploaded {line_count} statement lines")

    print(bold("\n4. Reconcile"))
    today = date.today()
    run = request("POST", f"{args.accord}/v1/runs", {
        "period_start": (today - timedelta(days=30)).isoformat(),
        "period_end": (today + timedelta(days=30)).isoformat(),
        "statement_id": statement_id,
    })
    assert isinstance(run, dict)

    print(f"   {run['ledger_count']} ledger vs {run['statement_count']} statement rows")
    print(f"   {run['matched_count']} matched, {run['break_count']} breaks, "
          f"match rate {run['match_rate'] * 100:.1f}%, {run['duration_ms']}ms")

    print(bold("\n5. What it found"))
    breaks = request("GET", f"{args.accord}/v1/breaks?run_id={run['id']}&limit=500")
    assert isinstance(breaks, list)

    found = Counter(b["type"] for b in breaks)
    for kind, count in sorted(found.items()):
        print(f"   detected {count:>2}  {kind}")

    print(bold("\n6. Did it find what was broken?"))
    ok = True
    # Each injected fault must appear at least as often as it was injected.
    # More is acceptable -- one dropped payment inside a batch can surface as
    # several -- but fewer means something went unreported.
    for kind, expected in sorted(injected.items()):
        if kind == "FEE":
            continue  # absorbed by tolerance when configured; not a break
        actual = found.get(kind, 0)
        if actual >= expected:
            print(green(f"   ok    {kind}: injected {expected}, reported {actual}"))
        else:
            ok = False
            print(red(f"   MISS  {kind}: injected {expected}, reported only {actual}"))

    print()
    print(f"   dashboard: {args.accord}/")
    print(f"   run:       {args.accord}/v1/runs/{run['id']}")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
