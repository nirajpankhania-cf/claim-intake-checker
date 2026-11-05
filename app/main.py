"""FastAPI entry point for Claim Intake Check.

Flag-and-report only: no route here (or anywhere else) may approve, deny,
pay, cancel, or contact a policyholder.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from app import config, db, history, intake, review
from app.models import LOSS_TYPES, SUBMITTED_VIA

app = FastAPI(title="Claim Intake Check")
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))


async def get_db() -> AsyncIterator[sqlite3.Connection]:
    """Async so the connection is opened on the same thread as the async routes
    that use it (sqlite3 connections are bound to their creating thread)."""
    conn = db.get_connection()
    try:
        db.init_db(conn)
        yield conn
    finally:
        conn.close()


def get_run_checks() -> intake.RunChecks:
    """The rule runner (CIC-28), overridable in tests.

    The import happens on first call, not here, so an unusable file still gets
    a 400 even before the runner exists.
    """

    def run_checks(conn: sqlite3.Connection, run_id: str, claim_ids: list[str]) -> None:
        from app.runner import run_checks as runner  # type: ignore[attr-defined]

        runner(conn, run_id, claim_ids)

    return run_checks


@app.get("/", response_class=PlainTextResponse)
def root() -> str:
    """Health check / landing route."""
    return "Claim Intake Check — OK"


@app.get("/intake", response_class=HTMLResponse)
def intake_form(request: Request) -> Response:
    return _form(request)


@app.post("/intake/csv")
async def intake_csv(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    run_checks: intake.RunChecks = Depends(get_run_checks),
) -> Response:
    # Starlette's per-field limit sits well above ours, so an oversized paste
    # reaches our check and gets a readable message instead of a bare 400.
    form = await request.form(max_part_size=2 * config.MAX_INTAKE_BYTES)
    upload = form.get("file")
    pasted = form.get("csv_text") or ""
    try:
        data = await upload.read(config.MAX_INTAKE_BYTES + 1) if isinstance(upload, UploadFile) else b""
        if data and str(pasted).strip():
            raise intake.IntakeFileError("Upload a file or paste CSV text, not both.")
        if data:
            text = intake.decode_upload(data)
        elif str(pasted).strip():
            text = str(pasted)
        else:
            raise intake.IntakeFileError("Choose a CSV file or paste CSV text.")
        summary = intake.ingest_csv(conn, text, run_checks)
    except intake.IntakeFileError as exc:
        return _form(request, error=str(exc), status_code=400)
    query = urlencode(
        {
            "run_id": summary.run_id,
            "accepted": summary.accepted,
            "skipped": summary.skipped,
            "malformed": summary.malformed,
        }
    )
    return RedirectResponse(f"/review?{query}", status_code=303)


@app.get("/review", response_class=HTMLResponse)
async def review_queue(
    request: Request,
    run_id: str | None = None,
    rule: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Flagged claims only, with rule and date filters. Read-only."""
    error, items = None, []
    try:
        filters = review.parse_filters(run_id, rule, date_from, date_to)
        items = review.review_queue(conn, filters)
    except review.FilterError as exc:
        error, filters = str(exc), review.Filters()
    context = {
        "items": items,
        "filters": filters,
        "raw": {"run_id": run_id or "", "rule": rule or "", "date_from": date_from or "", "date_to": date_to or ""},
        "rule_ids": review.rule_ids(conn),
        "summary": _intake_summary(request),
        "form_results": _form_results(conn, filters.run_id),
        "error": error,
    }
    return templates.TemplateResponse(request, "review.html", context, status_code=400 if error else 200)


def history_query(
    run_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    outcome: str | None = None,
) -> dict[str, str]:
    """History filter values, trimmed. Shared by the page and the CSV export."""
    values = {"run_id": run_id, "date_from": date_from, "date_to": date_to, "outcome": outcome}
    return {name: (value or "").strip() for name, value in values.items()}


def _history_filters(raw: dict[str, str]) -> review.Filters:
    return review.parse_filters(raw["run_id"], None, raw["date_from"], raw["date_to"], raw["outcome"])


@app.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    raw: dict[str, str] = Depends(history_query),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Every claim checked, passed or flagged, with filters. Read-only."""
    error, items = None, []
    try:
        items = review.history(conn, _history_filters(raw))
    except review.FilterError as exc:
        error = str(exc)
    totals = {name: sum(item.outcome == name for item in items) for name in review.OUTCOMES}
    context = {
        "items": items,
        "raw": raw,
        "export_query": urlencode({k: v for k, v in raw.items() if v}),
        "runs": review.list_runs(conn),
        "outcomes": review.OUTCOMES,
        "totals": totals,
        "error": error,
    }
    return templates.TemplateResponse(request, "history.html", context, status_code=400 if error else 200)


@app.get("/history.csv")
async def history_csv(
    raw: dict[str, str] = Depends(history_query),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """The history page's rows, with the same filters, as a CSV download."""
    try:
        filters = _history_filters(raw)
    except review.FilterError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return Response(
        history.to_csv(review.history(conn, filters)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="claim-history.csv"'},
    )


@app.get("/claims/{claim_id:path}", response_class=HTMLResponse)
async def claim_detail(
    request: Request, claim_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Response:
    """One claim's fields and every result and flag recorded for it. Read-only."""
    # Intake stores claim ids trimmed and uppercased; ROW- keys contain a
    # lowercase run id, so try the id exactly as given first.
    for candidate in dict.fromkeys((claim_id, claim_id.strip().upper())):
        claim = review.get_claim(conn, candidate)
        history = review.claim_history(conn, candidate)
        if claim is not None or history:
            claim_id = candidate
            break
    else:
        raise HTTPException(status_code=404, detail="No claim or intake row with that ID.")
    return templates.TemplateResponse(
        request, "claim.html", {"claim_id": claim_id, "claim": claim, "history": history}
    )


def _form_results(conn: sqlite3.Connection, run_id: str | None) -> list[sqlite3.Row]:
    """For a run from the single-claim form, each claim's outcome (for the banner)."""
    if not run_id:
        return []
    rows = review.run_results(conn, run_id)
    return rows if rows and rows[0]["source"] == "form" else []


def _intake_summary(request: Request) -> dict[str, int] | None:
    """Counts passed on the intake redirect, if all are present and whole numbers."""
    names = ("accepted", "skipped", "malformed")
    values = [request.query_params.get(n, "") for n in names]
    if not all(v.isascii() and v.isdecimal() for v in values):
        return None
    return {n: int(v) for n, v in zip(names, values)}


FIELD_LABELS: dict[str, str] = {
    "claim_id": "Claim ID",
    "policy_number": "Policy number",
    "policyholder_name": "Policyholder name",
    "claimant_name": "Claimant name (if not the policyholder)",
    "vehicle_reg": "Vehicle registration",
    "loss_date": "Loss date",
    "report_date": "Report date",
    "loss_type": "Loss type",
    "loss_location": "Loss location",
    "description": "Description",
    "claimed_amount": "Claimed amount",
    "third_party_involved": "Third party involved",
    "police_reference": "Police reference",
    "attachments": "Attachment file names (separate with ;)",
    "submitted_via": "Submitted via",
}


@app.get("/intake/claim", response_class=HTMLResponse)
def claim_form(request: Request) -> Response:
    return _claim_form(request, {"submitted_via": "form"})


@app.post("/intake/claim")
async def submit_claim(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    run_checks: intake.RunChecks = Depends(get_run_checks),
) -> Response:
    """Primary intake path: one claim. Errors re-show the form; nothing is stored."""
    form = await request.form()
    raw = {name: str(form.get(name) or "") for name in intake.COLUMNS}
    raw["third_party_involved"] = "true" if form.get("third_party_involved") else "false"
    claim, errors = intake.parse_claim_fields(raw)
    if claim is None:
        return _claim_form(request, raw, errors)
    try:
        run_id = intake.submit_claim(conn, claim, run_checks)
    except intake.ClaimAlreadySubmitted as exc:
        return _claim_form(request, raw, {"claim_id": str(exc)}, existing_claim_id=exc.claim_id)
    return RedirectResponse(f"/review?{urlencode({'run_id': run_id})}", status_code=303)


def _claim_form(
    request: Request,
    values: dict[str, str],
    errors: dict[str, str] | None = None,
    existing_claim_id: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "intake_claim.html",
        {
            "values": values,
            "errors": errors or {},
            "labels": FIELD_LABELS,
            "required": intake.REQUIRED,
            "loss_types": LOSS_TYPES,
            "submitted_via": SUBMITTED_VIA,
            "existing_claim_id": existing_claim_id,
        },
        status_code=400 if errors else 200,
    )


def _form(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request,
        "intake.html",
        {"error": error, "columns": intake.COLUMNS, "max_bytes": config.MAX_INTAKE_BYTES},
        status_code=status_code,
    )
