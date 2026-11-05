"""CSV claim intake: parse, validate, store, check, record.

Flow for one upload (all in one transaction, owned by ``ingest_csv``):

1. ``parse_csv`` checks the file as a whole and each row's *format*. It never
   judges dates against each other or against a policy; that is rule logic.
2. Rows whose ``claim_id`` is already on file, or earlier in the same file,
   are resubmissions (S8): skipped, not re-checked.
3. ``create_run`` records the run; accepted claims are inserted.
4. ``run_checks`` (the runner, CIC-28) checks the accepted claims.
5. ``record_intake_flags`` records every skipped (S8) and malformed (S9) row
   as a ``NEEDS_REVIEW`` result plus an ``INTAKE`` flag.

Free text (names, ``description``, ``loss_location``) is copied verbatim. It
is never inspected, and never changes what intake does.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app import config, db
from app.models import LOSS_TYPES, SUBMITTED_VIA, Claim, Flag

COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(Claim))
REQUIRED: frozenset[str] = frozenset(
    {
        "claim_id",
        "policy_number",
        "policyholder_name",
        "vehicle_reg",
        "loss_date",
        "report_date",
        "loss_type",
        "claimed_amount",
        "third_party_involved",
        "submitted_via",
    }
)
INTAKE_RULE_ID = "INTAKE"

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_AMOUNT = re.compile(r"-?\d+(\.\d+)?")

RunChecks = Callable[[sqlite3.Connection, str, list[str]], Any]


class IntakeFileError(ValueError):
    """The upload as a whole can't be used. Nothing is stored."""


@dataclass(frozen=True)
class IntakeRow:
    """A row that was not checked by the rules: resubmitted or malformed."""

    line_no: int
    kind: Literal["resubmission", "malformed"]
    submitted_claim_id: str
    problems: list[str]
    raw: dict[str, str]
    earlier_run_id: str | None = None
    earlier_line_no: int | None = None


@dataclass
class ParsedBatch:
    accepted: list[tuple[int, Claim, dict[str, str]]] = field(default_factory=list)
    malformed: list[IntakeRow] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.accepted) + len(self.malformed)


@dataclass(frozen=True)
class IntakeSummary:
    run_id: str
    accepted: int
    skipped: int
    malformed: int


# --- parsing ---------------------------------------------------------------


def decode_upload(data: bytes) -> str:
    """Apply the size cap and decode UTF-8 (a leading BOM is allowed)."""
    if len(data) > config.MAX_INTAKE_BYTES:
        raise IntakeFileError(
            f"The CSV is {len(data):,} bytes; the limit is "
            f"{config.MAX_INTAKE_BYTES:,} bytes."
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntakeFileError("The CSV is not UTF-8 text.") from exc


def parse_csv(text: str) -> ParsedBatch:
    """Check the header and the format of every row."""
    if len(text.encode("utf-8")) > config.MAX_INTAKE_BYTES:
        raise IntakeFileError(
            f"The CSV is larger than the {config.MAX_INTAKE_BYTES:,}-byte limit."
        )
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = [name.strip() for name in next(reader)]
    except StopIteration:
        raise IntakeFileError("The CSV is empty.") from None
    except csv.Error as exc:
        raise IntakeFileError(f"The CSV header can't be read: {exc}.") from exc
    _check_header(header)

    batch = ParsedBatch()
    line_no = reader.line_num + 1
    try:
        for cells in reader:
            start, line_no = line_no, reader.line_num + 1
            if not any(cell.strip() for cell in cells):
                continue
            raw = dict(zip(header, cells))
            claim_id = raw.get("claim_id", "").strip().upper()
            if len(cells) != len(header):
                problems = [f"row has {len(cells)} cells, expected {len(header)}"]
                batch.malformed.append(
                    IntakeRow(start, "malformed", claim_id, problems, raw)
                )
                continue
            claim, errors = parse_claim_fields(raw)
            problems = list(errors.values())
            if claim is None:
                batch.malformed.append(
                    IntakeRow(start, "malformed", claim_id, problems, raw)
                )
            else:
                batch.accepted.append((start, claim, raw))
    except csv.Error as exc:
        raise IntakeFileError(f"The CSV can't be read near line {line_no}: {exc}.") from exc
    if batch.row_count == 0:
        raise IntakeFileError("The CSV has a header but no claim rows.")
    return batch


def _check_header(header: list[str]) -> None:
    duplicated = sorted({name for name in header if header.count(name) > 1})
    missing = [name for name in COLUMNS if name not in header]
    unexpected = [name for name in header if name not in COLUMNS]
    problems = []
    if missing:
        problems.append("missing columns: " + ", ".join(missing))
    if unexpected:
        problems.append("unexpected columns: " + ", ".join(unexpected))
    if duplicated:
        problems.append("repeated columns: " + ", ".join(duplicated))
    if problems:
        raise IntakeFileError("The CSV header is wrong (" + "; ".join(problems) + ").")


def parse_claim_fields(raw: dict[str, str]) -> tuple[Claim | None, dict[str, str]]:
    """Build a ``Claim`` from submitted text fields, or say what's wrong.

    Shared by the CSV path (one row) and the single-claim form. Errors are
    keyed by field name, at most one per field. Checks formats only.
    """
    errors = {name: f"{name} is missing" for name in COLUMNS if name in REQUIRED and not raw[name].strip()}

    def problem(name: str, message: str) -> None:
        errors.setdefault(name, message)

    def text(name: str) -> str | None:
        return raw[name] if raw[name].strip() else None  # verbatim, never inspected

    def a_date(name: str) -> date | None:
        value = raw[name].strip()
        if not value:
            return None
        try:
            if _ISO_DATE.fullmatch(value):
                return date.fromisoformat(value)
        except ValueError:
            pass
        problem(name, f"{name} {value!r} is not a real YYYY-MM-DD date")
        return None

    def choice(name: str, allowed: tuple[str, ...]) -> str | None:
        value = raw[name].strip().lower()
        if value and value not in allowed:
            problem(name, f"{name} {value!r} is not one of: {', '.join(allowed)}")
        return value

    amount_text = raw["claimed_amount"].strip()
    amount = None
    if amount_text:
        try:
            if _AMOUNT.fullmatch(amount_text):
                amount = Decimal(amount_text)
        except InvalidOperation:
            pass
        if amount is None:
            problem("claimed_amount", f"claimed_amount {amount_text!r} is not a number")

    flag_text = raw["third_party_involved"].strip().lower()
    if flag_text and flag_text not in ("true", "false"):
        problem("third_party_involved", f"third_party_involved {raw['third_party_involved']!r} is not true or false")

    values = {
        "claim_id": raw["claim_id"].strip().upper(),
        "policy_number": raw["policy_number"].strip().upper(),
        "policyholder_name": text("policyholder_name"),
        "claimant_name": text("claimant_name"),
        "vehicle_reg": text("vehicle_reg"),
        "loss_date": a_date("loss_date"),
        "report_date": a_date("report_date"),
        "loss_type": choice("loss_type", LOSS_TYPES),
        "loss_location": text("loss_location"),
        "description": text("description"),
        "claimed_amount": amount,
        "third_party_involved": flag_text == "true",
        "police_reference": text("police_reference"),
        "attachments": [a.strip() for a in raw["attachments"].split(";") if a.strip()],
        "submitted_via": choice("submitted_via", SUBMITTED_VIA),
    }
    if errors:
        return None, errors
    return Claim(**values), {}


# --- storing and checking --------------------------------------------------


def create_run(conn: sqlite3.Connection, source: db.RunSource, claim_count: int) -> str:
    """Record a new check run. ``claim_count`` is every row in the submission."""
    return db.insert_run(conn, source, claim_count)


def split_resubmissions(
    conn: sqlite3.Connection, batch: ParsedBatch
) -> tuple[list[Claim], list[IntakeRow]]:
    """Separate new claims from claim_ids already on file or earlier in the batch."""
    new: list[Claim] = []
    skipped: list[IntakeRow] = []
    first_line: dict[str, int] = {}
    for line_no, claim, raw in batch.accepted:
        if claim.claim_id in first_line:
            skipped.append(
                IntakeRow(line_no, "resubmission", claim.claim_id,
                          ["claim_id appears earlier in this file"], raw,
                          earlier_line_no=first_line[claim.claim_id])
            )
        elif db.claim_id_in_use(conn, claim.claim_id):
            skipped.append(
                IntakeRow(line_no, "resubmission", claim.claim_id,
                          ["claim_id is already on file"], raw,
                          earlier_run_id=db.get_latest_run_for_claim(conn, claim.claim_id))
            )
        else:
            first_line[claim.claim_id] = line_no
            new.append(claim)
    return new, skipped


def record_intake_flags(
    conn: sqlite3.Connection,
    run_id: str,
    rows: list[IntakeRow],
    taken_ids: set[str],
) -> None:
    """Record each unchecked row as ``NEEDS_REVIEW`` with an ``INTAKE`` flag.

    Resubmissions, rows with no claim_id, and rows whose claim_id is already
    used by a stored claim are keyed ``ROW-<run_id>-<line_no>`` so they never
    share a key with a real claim.
    """
    used = set(taken_ids)
    for row in rows:
        key = row.submitted_claim_id
        if row.kind == "resubmission" or not key or key in used or db.claim_exists(conn, key):
            key = f"ROW-{run_id}-{row.line_no}"
        used.add(key)
        db.insert_result(conn, run_id, key, "NEEDS_REVIEW")
        db.insert_flag(conn, run_id, key, _intake_flag(run_id, row))


def _intake_flag(run_id: str, row: IntakeRow) -> Flag:
    if row.kind == "resubmission":
        if row.earlier_line_no is not None:
            where = f"earlier in this upload (line {row.earlier_line_no}, run {run_id})"
        elif row.earlier_run_id is not None:
            where = f"already on file, last checked in run {row.earlier_run_id}"
        else:
            where = "already on file, with no earlier check run recorded"
        reason = (
            f"Line {row.line_no}: claim {row.submitted_claim_id} was submitted "
            f"again ({where}). Skipped, not re-checked."
        )
    else:
        reason = (
            f"Line {row.line_no}: not checked because the row is malformed: "
            + "; ".join(row.problems) + "."
        )
    return Flag(
        rule_id=INTAKE_RULE_ID,
        reason=reason,
        inputs={
            "kind": row.kind,
            "line_no": row.line_no,
            "submitted_claim_id": row.submitted_claim_id or None,
            "problems": row.problems,
            "earlier_run_id": row.earlier_run_id
            or (run_id if row.earlier_line_no is not None else None),
            "earlier_line_no": row.earlier_line_no,
            "row": row.raw,
        },
    )


def ingest_csv(conn: sqlite3.Connection, text: str, run_checks: RunChecks) -> IntakeSummary:
    """Parse, store and check one CSV submission in a single transaction.

    Raises ``IntakeFileError`` before touching the DB if the file is unusable.
    """
    batch = parse_csv(text)
    with conn:
        # Take the write lock before the resubmission check, so two uploads of
        # the same new claim_id can't both pass it.
        conn.execute("BEGIN IMMEDIATE")
        new_claims, skipped = split_resubmissions(conn, batch)
        run_id = create_run(conn, "csv", batch.row_count)
        for claim in new_claims:
            db.insert_claim(conn, claim)
        new_ids = [claim.claim_id for claim in new_claims]
        run_checks(conn, run_id, new_ids)
        record_intake_flags(conn, run_id, skipped + batch.malformed, set(new_ids))
    return IntakeSummary(run_id, len(new_claims), len(skipped), len(batch.malformed))


# --- single claim (form) -----------------------------------------------------


class ClaimAlreadySubmitted(Exception):
    """The form's claim_id is already on file. Nothing was stored."""

    def __init__(self, claim_id: str) -> None:
        super().__init__(f"{claim_id} has already been submitted.")
        self.claim_id = claim_id


def submit_claim(conn: sqlite3.Connection, claim: Claim, run_checks: RunChecks) -> str:
    """Store and check one claim from the form, in one transaction. Returns the run_id.

    Unlike CSV, a resubmitted claim_id is refused with nothing stored, since
    the person submitting is there to see why.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if db.claim_id_in_use(conn, claim.claim_id):
            raise ClaimAlreadySubmitted(claim.claim_id)
        run_id = create_run(conn, "form", 1)
        db.insert_claim(conn, claim)
        run_checks(conn, run_id, [claim.claim_id])
    return run_id
