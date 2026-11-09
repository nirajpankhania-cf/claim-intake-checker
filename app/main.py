"""FastAPI entry point for Claim Intake Check.

Flag-and-report only: no route here (or anywhere else) may approve, deny,
pay, cancel, or contact a policyholder.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from app import config, db, desk, history, intake, review
from app.models import LOSS_TYPES, SUBMITTED_VIA

app = FastAPI(title="Claim Intake Check")
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
_STYLESHEET = Path(__file__).with_name("static") / "desk.css"
# Changes whenever the stylesheet does, so a browser never pairs new pages with an old cached copy.
templates.env.globals["stylesheet_version"] = lambda: _STYLESHEET.stat().st_mtime_ns


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
    to_desk = form.get("next") == "desk"
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
        if to_desk:
            return _desk_home(request, conn, desk.plain_file_error(exc))
        return _form(request, error=str(exc), status_code=400)
    if to_desk:
        return _desk_result(summary.run_id)
    query = urlencode(
        {
            "run_id": summary.run_id,
            "new": summary.new,
            "updated": summary.updated,
            "unchanged": summary.unchanged,
            "malformed": summary.malformed,
            "repeats": summary.repeats,
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
    latest: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """The history page's rows, with the same filters, as a CSV download."""
    try:
        filters = _history_filters(raw)
    except review.FilterError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return Response(
        history.to_csv(desk.one_per_claim(conn, filters) if latest == "1" else review.history(conn, filters)),
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
    versions = db.get_claim_versions(conn, claim_id)
    changes = desk.version_changes(versions, claim, FIELD_LABELS) if claim is not None else {}
    context = {
        "claim_id": claim_id,
        "claim": claim,
        "history": history,
        "groups": review.group_repeats(history, set(changes)),
        "changes": changes,
        "versions": versions,
    }
    return templates.TemplateResponse(request, "claim.html", context)


def _form_results(conn: sqlite3.Connection, run_id: str | None) -> list[sqlite3.Row]:
    """For a run from the single-claim form, each claim's outcome (for the banner)."""
    if not run_id or review.run_source(conn, run_id) != "form":
        return []
    return review.run_results(conn, run_id)


def _intake_summary(request: Request) -> dict[str, int] | None:
    """Counts passed on the intake redirect, if all are present and whole numbers."""
    names = ("new", "updated", "unchanged", "malformed", "repeats")
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
    """Primary intake path: one claim. Errors re-show the form; nothing is stored.

    A claim_id already on file re-shows the form as a confirmation: which
    fields differ, and a "Replace and re-check" button that sends the same
    claim_id back in ``replace``. Only then is the stored claim overwritten.
    """
    form = await request.form()
    # A file part sent under a field name is treated as a missing value.
    raw = {name: value if isinstance(value := form.get(name), str) else "" for name in intake.COLUMNS}
    to_desk = form.get("next") == "desk"
    show_form = _desk_claim_form if to_desk else _claim_form
    claim, errors = intake.parse_claim_fields(raw)
    if claim is None:
        return show_form(request, raw, errors)
    # The token must name the claim being replaced, so editing the id on the
    # confirmation page can't silently overwrite a different claim.
    replace = form.get("replace") == claim.claim_id
    try:
        run_id = intake.submit_claim(conn, claim, run_checks, replace=replace)
    except intake.ClaimAlreadySubmitted as exc:
        changed = [FIELD_LABELS[name] for name in intake.changed_fields(exc.existing, claim)]
        return show_form(request, raw, existing_claim_id=exc.claim_id, changed=changed)
    if to_desk:
        return _desk_result(run_id)
    return RedirectResponse(f"/review?{urlencode({'run_id': run_id})}", status_code=303)


def _claim_form(
    request: Request,
    values: dict[str, str],
    errors: dict[str, str] | None = None,
    existing_claim_id: str | None = None,
    template: str = "intake_claim.html",
    changed: list[str] | None = None,
) -> Response:
    """The form, with field errors (400) or as a replace confirmation (200).

    ``changed`` is the labels of the fields that differ from the stored claim;
    labels only, so submitted text is never repeated outside its own field.
    """
    return templates.TemplateResponse(
        request,
        template,
        {
            "values": values,
            "errors": errors or {},
            "labels": FIELD_LABELS,
            "required": intake.REQUIRED,
            "loss_types": LOSS_TYPES,
            "submitted_via": SUBMITTED_VIA,
            "existing_claim_id": existing_claim_id,
            "changed": changed or [],
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


# --- review desk: plain-language pages for a manager, over the same code -----


def _desk_result(run_id: str) -> Response:
    return RedirectResponse(f"/desk/runs/{quote(run_id, safe='')}", status_code=303)


def _desk_home(request: Request, conn: sqlite3.Connection, error: str | None = None) -> Response:
    context = {"error": error, "runs": desk.recent_runs(conn)}
    return templates.TemplateResponse(request, "desk/home.html", context, status_code=400 if error else 200)


def _desk_not_found(request: Request, message: str) -> Response:
    return templates.TemplateResponse(request, "desk/not_found.html", {"message": message}, status_code=404)


def _desk_claim_form(
    request: Request,
    values: dict[str, str],
    errors: dict[str, str] | None = None,
    existing_claim_id: str | None = None,
    changed: list[str] | None = None,
) -> Response:
    plain = {name: desk.plain_problem(text, FIELD_LABELS) for name, text in (errors or {}).items()}
    return _claim_form(request, values, plain, existing_claim_id, template="desk/claim_form.html", changed=changed)


@app.get("/desk", response_class=HTMLResponse)
async def desk_home(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    return _desk_home(request, conn)


@app.get("/desk/latest")
async def desk_latest(conn: sqlite3.Connection = Depends(get_db)) -> Response:
    """The most recent check's work queue, or the start page if there are none."""
    latest = desk.recent_runs(conn, limit=1)
    return _desk_result(str(latest[0]["run_id"])) if latest else RedirectResponse("/desk", status_code=303)


@app.post("/desk/sample")
async def desk_sample(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    run_checks: intake.RunChecks = Depends(get_run_checks),
) -> Response:
    """Send the generated sample file through the normal CSV intake."""
    try:
        text = intake.decode_upload(desk.SAMPLE_CSV.read_bytes())
        summary = intake.ingest_csv(conn, text, run_checks)
    except FileNotFoundError:
        return _desk_home(request, conn, desk.SAMPLE_MISSING)
    except intake.IntakeFileError as exc:
        return _desk_home(request, conn, desk.plain_file_error(exc))
    return _desk_result(summary.run_id)


@app.get("/desk/claim", response_class=HTMLResponse)
def desk_claim_form(request: Request) -> Response:
    return _desk_claim_form(request, {"submitted_via": "form"})


@app.get("/desk/runs/{run_id}", response_class=HTMLResponse)
async def desk_run(
    request: Request,
    run_id: str,
    queue: str | None = None,
    page: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """One check's work queue, a tab at a time. Read-only."""
    if review.run_source(conn, run_id) is None:
        return _desk_not_found(request, "We couldn't find that check.")
    items = review.history(conn, review.Filters(run_id=run_id))
    rows = desk.to_rows(conn, items, FIELD_LABELS, desk.change_markers(conn, run_id))
    queues = desk.split_queues(rows)
    current = queue if queue in queues or queue == "summary" else desk.first_queue(queues)
    context = {
        "rows": rows,
        "run_id": run_id,
        "counts": desk.counts(rows),
        "change_labels": desk.CHANGE_LABELS,
        "repeats": desk.in_file_repeats(conn, run_id),
        "nothing_changed": desk.nothing_changed(rows),
        "checked_on": review.utc_date(items[0].run_started_at) if items else None,
        "source": desk.SOURCES.get(review.run_source(conn, run_id) or "", ""),
        "tabs": [(name, label, len(queues[name])) for name, label in desk.QUEUES.items()],
        "queue": current,
        "queue_label": desk.QUEUES.get(current, ""),
        "listing": desk.paginate(queues.get(current, []), page),
        "columns": desk.CHECKS,
        "checks": desk.check_summary(rows),
        "overlaps": sum(row.flag_count > 1 for row in rows),
    }
    return templates.TemplateResponse(request, "desk/result.html", context)


@app.get("/desk/claims", response_class=HTMLResponse)
async def desk_claims(
    request: Request,
    outcome: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: str | None = None,
    latest: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Every claim checked, filtered by outcome and date. Read-only."""
    error, rows = None, []
    once = latest == "1"
    raw = {"outcome": outcome or "", "date_from": date_from or "", "date_to": date_to or "", "latest": "1" if once else ""}
    chosen = {name: value for name, value in raw.items() if value and name != "latest"}
    try:
        filters = review.parse_filters(None, None, date_from, date_to, outcome)
        items = desk.one_per_claim(conn, filters) if once else review.history(conn, filters)
        rows = desk.to_rows(conn, items, FIELD_LABELS)
    except review.FilterError as exc:
        error = str(exc)
    context = {
        "listing": desk.paginate(rows, page),
        "outcomes": review.OUTCOMES,
        "raw": raw,
        "query": {name: value for name, value in raw.items() if value},
        "every_link": urlencode(chosen),
        "once_link": urlencode({**chosen, "latest": "1"}),
        "has_filters": bool(chosen),
        "export_query": urlencode({name: value for name, value in raw.items() if value}),
        "error": error,
    }
    return templates.TemplateResponse(request, "desk/claims.html", context, status_code=400 if error else 200)


def _queue_position(
    conn: sqlite3.Connection, run_id: str | None, queue: str | None, claim_id: str
) -> dict[str, object] | None:
    """Previous and Next within the queue this claim was opened from, if any."""
    if not run_id or queue not in desk.QUEUES or review.run_source(conn, run_id) is None:
        return None
    items = review.history(conn, review.Filters(run_id=run_id))
    return desk.neighbours(desk.split_queues(desk.to_rows(conn, items, FIELD_LABELS))[queue], claim_id)


@app.get("/desk/claims/{claim_id:path}", response_class=HTMLResponse)
async def desk_claim(
    request: Request,
    claim_id: str,
    run: str | None = None,
    queue: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """One stored claim's fields and the reasons it was flagged. Read-only."""
    for candidate in dict.fromkeys((claim_id, claim_id.strip().upper())):
        claim = review.get_claim(conn, candidate)
        if claim is not None:
            history_items = review.claim_history(conn, candidate)
            versions = db.get_claim_versions(conn, candidate)
            context = {
                "claim_id": candidate,
                "claim": claim,
                "fields": desk.claim_fields(claim, FIELD_LABELS),
                "result": desk.claim_result(history_items, candidate, versions),
                "audit": desk.audit_trail(history_items, versions, claim, FIELD_LABELS),
                "state_labels": desk.STATE_LABELS,
                "nav": _queue_position(conn, run, queue, candidate),
                "run_id": run,
                "queue": queue,
                "queue_label": desk.QUEUES.get(queue or "", ""),
            }
            return templates.TemplateResponse(request, "desk/claim.html", context)
    return _desk_not_found(request, "We couldn't find a claim with that number.")
