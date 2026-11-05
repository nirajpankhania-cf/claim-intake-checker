"""FastAPI entry point for Claim Intake Check.

Flag-and-report only: no route here (or anywhere else) may approve, deny,
pay, cancel, or contact a policyholder.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from app import config, db, intake

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


def _form(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request,
        "intake.html",
        {"error": error, "columns": intake.COLUMNS, "max_bytes": config.MAX_INTAKE_BYTES},
        status_code=status_code,
    )
