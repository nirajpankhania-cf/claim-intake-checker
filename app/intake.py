"""CSV claim intake: parse, validate, store, check, record.

Flow for one upload (all in one transaction, owned by ``ingest_csv``):

1. ``parse_csv`` checks the file as a whole and each row's *format*. It never
   judges dates against each other or against a policy; that is rule logic.
2. ``pick_latest`` keeps one row per ``claim_id`` in the file: the most recent
   ``report_date`` wins, and a tie goes to the last row. The others are
   dropped and only counted.
3. ``create_run`` records the run; ``store_claim`` inserts each new claim or,
   for a ``claim_id`` already on file (S8), overwrites the stored claim after
   saving the old version to ``claim_versions`` if anything changed.
4. ``run_checks`` (the runner, CIC-28) checks every kept claim, new or not.
5. ``record_intake_flags`` records every malformed (S9) row as a
   ``NEEDS_REVIEW`` result plus an ``INTAKE`` flag.

Resubmission handling is a team assumption pending the client's answer (see
``docs/decisions.md``, 2026-11-09).

Free text (names, ``description``, ``loss_location``) is copied verbatim. It
is never inspected, and never changes what intake does.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from collections.abc import Callable
from collections import Counter
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
    """The upload as a whole can't be used. Nothing is stored.

    ``kind`` says what went wrong, so a page can word it without parsing the text.
    """

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class IntakeRow:
    """A row that was not checked by the rules because it is malformed."""

    line_no: int
    kind: Literal["malformed"]
    submitted_claim_id: str
    problems: list[str]
    raw: dict[str, str]


@dataclass
class ParsedBatch:
    accepted: list[tuple[int, Claim, dict[str, str]]] = field(default_factory=list)
    malformed: list[IntakeRow] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.accepted) + len(self.malformed)


@dataclass(frozen=True)
class IntakeSummary:
    """What one upload did. ``repeats`` is rows dropped because the same
    claim_id appeared again in the file; they are not stored."""

    run_id: str
    new: int
    updated: int
    unchanged: int
    malformed: int
    repeats: int


# --- parsing ---------------------------------------------------------------


def decode_upload(data: bytes) -> str:
    """Apply the size cap and decode UTF-8 (a leading BOM is allowed)."""
    if len(data) > config.MAX_INTAKE_BYTES:
        raise IntakeFileError(
            f"The CSV is {len(data):,} bytes; the limit is "
            f"{config.MAX_INTAKE_BYTES:,} bytes.",
            kind="too_large",
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntakeFileError("The CSV is not UTF-8 text.", kind="encoding") from exc


def parse_csv(text: str) -> ParsedBatch:
    """Check the header and the format of every row."""
    if len(text.encode("utf-8")) > config.MAX_INTAKE_BYTES:
        raise IntakeFileError(
            f"The CSV is larger than the {config.MAX_INTAKE_BYTES:,}-byte limit.",
            kind="too_large",
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
        raise IntakeFileError("The CSV header is wrong (" + "; ".join(problems) + ").", kind="header")


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

StoreState = Literal["new", "updated", "unchanged"]
AcceptedRow = tuple[int, Claim, dict[str, str]]


def create_run(conn: sqlite3.Connection, source: db.RunSource, claim_count: int) -> str:
    """Record a new check run. ``claim_count`` is every row in the submission."""
    return db.insert_run(conn, source, claim_count)


def pick_latest(accepted: list[AcceptedRow]) -> tuple[list[AcceptedRow], int]:
    """One row per claim_id: the most recent ``report_date`` wins, ties go to
    the last row in the file. Returns the kept rows (in first-seen order, so
    submission order is stable) and how many rows were dropped."""
    winners: dict[str, AcceptedRow] = {}
    for entry in accepted:
        claim = entry[1]
        current = winners.get(claim.claim_id)
        if current is None or claim.report_date >= current[1].report_date:
            winners[claim.claim_id] = entry
    return list(winners.values()), len(accepted) - len(winners)


def changed_fields(old: Claim, new: Claim) -> list[str]:
    """Names of the fields that differ, in ``Claim`` field order."""
    return [f.name for f in fields(Claim) if getattr(old, f.name) != getattr(new, f.name)]


def store_claim(conn: sqlite3.Connection, run_id: str, claim: Claim) -> StoreState:
    """Insert a new claim, or overwrite the stored one with this submission.

    When the stored claim differs, its old version is saved to
    ``claim_versions`` (against ``run_id``) before it is overwritten. An
    identical resubmission stores nothing; it is only checked again.
    """
    stored = db.get_claim(conn, claim.claim_id)
    if stored is None:
        db.insert_claim(conn, claim)
        return "new"
    if stored == claim:
        return "unchanged"
    db.insert_claim_version(conn, run_id, stored)
    db.update_claim(conn, claim)
    return "updated"


def record_intake_flags(
    conn: sqlite3.Connection,
    run_id: str,
    rows: list[IntakeRow],
    taken_ids: set[str],
) -> None:
    """Record each malformed row as ``NEEDS_REVIEW`` with an ``INTAKE`` flag.

    Rows with no claim_id, or whose claim_id is used by a stored claim or by
    a claim checked in this run, are keyed ``ROW-<run_id>-<line_no>`` so they
    never share a key with a real claim.
    """
    used = set(taken_ids)
    for row in rows:
        key = row.submitted_claim_id
        if not key or key in used or db.claim_exists(conn, key):
            key = f"ROW-{run_id}-{row.line_no}"
        used.add(key)
        db.insert_result(conn, run_id, key, "NEEDS_REVIEW")
        db.insert_flag(conn, run_id, key, _intake_flag(row))


def _intake_flag(row: IntakeRow) -> Flag:
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
            "row": row.raw,
        },
    )


def ingest_csv(conn: sqlite3.Connection, text: str, run_checks: RunChecks) -> IntakeSummary:
    """Parse, store and check one CSV submission in a single transaction.

    Raises ``IntakeFileError`` before touching the DB if the file is unusable.
    """
    batch = parse_csv(text)
    kept, repeats = pick_latest(batch.accepted)
    with conn:
        # Take the write lock before looking for stored claims, so two uploads
        # of the same claim_id serialise and the later one overwrites.
        conn.execute("BEGIN IMMEDIATE")
        run_id = create_run(conn, "csv", batch.row_count)  # before store_claim: versions reference the run
        states = Counter(store_claim(conn, run_id, claim) for _, claim, _ in kept)
        check_ids = [claim.claim_id for _, claim, _ in kept]
        run_checks(conn, run_id, check_ids)
        record_intake_flags(conn, run_id, batch.malformed, set(check_ids))
    return IntakeSummary(
        run_id, states["new"], states["updated"], states["unchanged"], len(batch.malformed), repeats
    )


# --- single claim (form) -----------------------------------------------------


class ClaimAlreadySubmitted(Exception):
    """The form's claim_id is already on file and ``replace`` was not given.

    Nothing was stored. ``existing`` is the stored claim, so the form can say
    which fields differ before the person confirms the replacement.
    """

    def __init__(self, claim_id: str, existing: Claim) -> None:
        super().__init__(f"{claim_id} has already been submitted.")
        self.claim_id = claim_id
        self.existing = existing


def submit_claim(
    conn: sqlite3.Connection, claim: Claim, run_checks: RunChecks, *, replace: bool = False
) -> str:
    """Store and check one claim from the form, in one transaction. Returns the run_id.

    A claim_id already on file is refused unless ``replace`` is set, since the
    person submitting is there to confirm. With ``replace`` it is stored the
    same way as a CSV row (``store_claim``) and checked again.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        stored = db.get_claim(conn, claim.claim_id)
        if stored is not None and not replace:
            raise ClaimAlreadySubmitted(claim.claim_id, stored)
        run_id = create_run(conn, "form", 1)
        store_claim(conn, run_id, claim)
        run_checks(conn, run_id, [claim.claim_id])
    return run_id
