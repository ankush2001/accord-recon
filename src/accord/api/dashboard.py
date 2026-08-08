"""The break queue, rendered.

A reconciliation engine whose output is only JSON is a reconciliation engine
nobody uses. The people who work breaks are finance operators, and what they
need is a list, sorted by how much money is unexplained, that says what to look
at first.

Server-rendered with Jinja and no JavaScript build step: the page is a table of
rows the database already produced, and shipping a front-end toolchain to
render one would be a strange amount of machinery for it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accord.db import get_session
from accord.models import BreakRow, ReconRunRow
from accord.schemas import RunSummary

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _money(minor: int, currency: str = "") -> str:
    """Minor units to something readable, sign kept."""
    sign = "-" if minor < 0 else ""
    whole, fraction = divmod(abs(minor), 100)
    return f"{sign}{whole:,}.{fraction:02d} {currency}".strip()


templates.env.filters["money"] = _money


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    latest = session.scalar(select(ReconRunRow).order_by(ReconRunRow.started_at.desc()).limit(1))

    breaks = list(
        session.scalars(
            select(BreakRow)
            .where(BreakRow.is_open.is_(True))
            .order_by(func.abs(BreakRow.amount_minor).desc(), BreakRow.created_at)
            .limit(50)
        )
    )

    by_type: dict[str, int] = {}
    for row in breaks:
        by_type[row.type] = by_type.get(row.type, 0) + 1

    recent = list(
        session.scalars(select(ReconRunRow).order_by(ReconRunRow.started_at.desc()).limit(10))
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "run": RunSummary.of(latest) if latest else None,
            "breaks": breaks,
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "recent": [RunSummary.of(r) for r in recent],
            "open_total": sum(abs(b.amount_minor) for b in breaks),
        },
    )
