"""SQLite schema and database helpers.

Plain ``sqlite3``, no ORM. Storage conventions:

* Dates are ISO ``YYYY-MM-DD`` text and are converted back to ``date``
  objects on read; timestamps are ISO 8601 UTC text.
* Money is stored as text (``str(Decimal)``) so values round-trip exactly.
* Booleans are ``INTEGER`` 0/1; ``Claim.attachments`` is a JSON array.
* Free text (names, ``description``, ``loss_location``) is stored verbatim.

``check_results``, ``flags`` and ``claim_versions`` are append-only history.
This module has no UPDATE or DELETE helper for them, and triggers in
``app/schema.sql`` reject any attempt to update or delete their rows.

``claims`` holds the current version of each claim. It is changed only through
``update_claim``, and only after ``insert_claim_version`` has saved the version
being replaced (see ``app.intake.store_claim``). Nothing deletes a claim.

``claims.policy_number`` deliberately has no foreign key to ``policies``: a
claim quoting an unknown policy is a valid input that rule R0 flags.

Helpers do not commit; callers own the transaction (``with conn:``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from app.models import Claim, ClaimVersion, Flag, Outcome, Policy

RunSource = Literal["form", "csv"]

DEFAULT_DB_PATH = Path(os.environ.get("CIC_DB_PATH", "claim_intake.db"))

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# --- connection / schema ----------------------------------------------------


def get_connection(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with ``sqlite3.Row`` rows and foreign keys enforced."""
    conn = sqlite3.connect(str(path if path is not None else DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table, index and trigger. Safe to call repeatedly."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


# --- row -> dataclass converters -------------------------------------------


def row_to_policy(row: sqlite3.Row) -> Policy:
    return Policy(
        policy_number=row["policy_number"],
        policyholder_name=row["policyholder_name"],
        vehicle_reg=row["vehicle_reg"],
        cover_type=row["cover_type"],
        effective_date=date.fromisoformat(row["effective_date"]),
        expiration_date=date.fromisoformat(row["expiration_date"]),
        cancellation_date=_date(row["cancellation_date"]),
        policy_limit=Decimal(row["policy_limit"]),
    )


def row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        claim_id=row["claim_id"],
        policy_number=row["policy_number"],
        policyholder_name=row["policyholder_name"],
        claimant_name=row["claimant_name"],
        vehicle_reg=row["vehicle_reg"],
        loss_date=date.fromisoformat(row["loss_date"]),
        report_date=date.fromisoformat(row["report_date"]),
        loss_type=row["loss_type"],
        loss_location=row["loss_location"],
        description=row["description"],
        claimed_amount=Decimal(row["claimed_amount"]),
        third_party_involved=bool(row["third_party_involved"]),
        police_reference=row["police_reference"],
        attachments=list(json.loads(row["attachments"])),
        submitted_via=row["submitted_via"],
    )


def row_to_flag(row: sqlite3.Row) -> Flag:
    return Flag(
        rule_id=row["rule_id"],
        reason=row["reason"],
        inputs=json.loads(row["inputs_json"]),
    )


# --- policies / claims ------------------------------------------------------


def insert_policy(conn: sqlite3.Connection, policy: Policy) -> None:
    conn.execute(
        """
        INSERT INTO policies (
            policy_number, policyholder_name, vehicle_reg, cover_type,
            effective_date, expiration_date, cancellation_date, policy_limit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            policy.policy_number,
            policy.policyholder_name,
            policy.vehicle_reg,
            policy.cover_type,
            _iso(policy.effective_date),
            _iso(policy.expiration_date),
            _iso(policy.cancellation_date),
            str(policy.policy_limit),
        ),
    )


CLAIM_COLUMNS: tuple[str, ...] = (
    "claim_id", "policy_number", "policyholder_name", "claimant_name",
    "vehicle_reg", "loss_date", "report_date", "loss_type", "loss_location",
    "description", "claimed_amount", "third_party_involved",
    "police_reference", "attachments", "submitted_via",
)


def _claim_values(claim: Claim) -> tuple[object, ...]:
    """The claim's column values in ``CLAIM_COLUMNS`` order, in storage form."""
    return (
        claim.claim_id,
        claim.policy_number,
        claim.policyholder_name,
        claim.claimant_name,
        claim.vehicle_reg,
        _iso(claim.loss_date),
        _iso(claim.report_date),
        claim.loss_type,
        claim.loss_location,
        claim.description,
        str(claim.claimed_amount),
        int(claim.third_party_involved),
        claim.police_reference,
        json.dumps(list(claim.attachments)),
        claim.submitted_via,
    )


def insert_claim(conn: sqlite3.Connection, claim: Claim) -> None:
    conn.execute(
        f"INSERT INTO claims ({', '.join(CLAIM_COLUMNS)})"
        f" VALUES ({', '.join('?' * len(CLAIM_COLUMNS))})",
        _claim_values(claim),
    )


def update_claim(conn: sqlite3.Connection, claim: Claim) -> None:
    """Overwrite the stored claim with ``claim`` (matched on ``claim_id``).

    A plain UPDATE, so the row keeps its rowid and therefore its place in
    submission order, which the runner uses for R3. Call
    ``insert_claim_version`` with the stored claim first; nothing else may
    change a claim. Raises ``LookupError`` if no claim has that id.
    """
    assignments = ", ".join(f"{name} = ?" for name in CLAIM_COLUMNS[1:])
    cur = conn.execute(
        f"UPDATE claims SET {assignments} WHERE claim_id = ?",
        (*_claim_values(claim)[1:], claim.claim_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"update_claim: no stored claim with id {claim.claim_id!r}")


def get_claim(conn: sqlite3.Connection, claim_id: str) -> Claim | None:
    row = conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    return row_to_claim(row) if row is not None else None


def get_policy(conn: sqlite3.Connection, policy_number: str) -> Policy | None:
    row = conn.execute(
        "SELECT * FROM policies WHERE policy_number = ?", (policy_number,)
    ).fetchone()
    return row_to_policy(row) if row is not None else None


def get_claims_on_policy(conn: sqlite3.Connection, policy_number: str) -> list[Claim]:
    rows = conn.execute(
        "SELECT * FROM claims WHERE policy_number = ? ORDER BY claim_id",
        (policy_number,),
    ).fetchall()
    return [row_to_claim(row) for row in rows]


def claim_exists(conn: sqlite3.Connection, claim_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    return row is not None


# --- runs, results, flags (append-only) ------------------------------------


def insert_run(
    conn: sqlite3.Connection,
    source: RunSource,
    claim_count: int,
    *,
    run_id: str | None = None,
    started_at: str | None = None,
) -> str:
    """Record a check run and return its ``run_id``."""
    run_id = run_id or uuid.uuid4().hex
    conn.execute(
        "INSERT INTO check_runs (run_id, started_at, source, claim_count)"
        " VALUES (?, ?, ?, ?)",
        (run_id, started_at or _now(), source, claim_count),
    )
    return run_id


def insert_result(
    conn: sqlite3.Connection,
    run_id: str,
    claim_id: str,
    outcome: Outcome,
    *,
    checked_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO check_results (run_id, claim_id, outcome, checked_at)"
        " VALUES (?, ?, ?, ?)",
        (run_id, claim_id, outcome, checked_at or _now()),
    )
    return int(cur.lastrowid)


def insert_flag(
    conn: sqlite3.Connection,
    run_id: str,
    claim_id: str,
    flag: Flag,
    *,
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO flags"
        " (run_id, claim_id, rule_id, reason, inputs_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            claim_id,
            flag.rule_id,
            flag.reason,
            json.dumps(flag.inputs, sort_keys=True),
            created_at or _now(),
        ),
    )
    return int(cur.lastrowid)


def get_latest_run_for_claim(conn: sqlite3.Connection, claim_id: str) -> str | None:
    """The ``run_id`` that most recently recorded a result for ``claim_id``."""
    row = conn.execute(
        "SELECT run_id FROM check_results WHERE claim_id = ?"
        " ORDER BY checked_at DESC, id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    return row["run_id"] if row is not None else None


# --- claim versions (append-only) ------------------------------------------


def insert_claim_version(
    conn: sqlite3.Connection,
    run_id: str,
    claim: Claim,
    *,
    replaced_at: str | None = None,
) -> int:
    """Save ``claim`` as the version that ``run_id`` is about to replace."""
    cur = conn.execute(
        f"INSERT INTO claim_versions (run_id, replaced_at, {', '.join(CLAIM_COLUMNS)})"
        f" VALUES (?, ?, {', '.join('?' * len(CLAIM_COLUMNS))})",
        (run_id, replaced_at or _now(), *_claim_values(claim)),
    )
    return int(cur.lastrowid)


def get_claim_versions(conn: sqlite3.Connection, claim_id: str) -> list[ClaimVersion]:
    """Every superseded version of a claim, oldest first."""
    rows = conn.execute(
        "SELECT * FROM claim_versions WHERE claim_id = ? ORDER BY id", (claim_id,)
    ).fetchall()
    return [
        ClaimVersion(row["run_id"], datetime.fromisoformat(row["replaced_at"]), row_to_claim(row))
        for row in rows
    ]
