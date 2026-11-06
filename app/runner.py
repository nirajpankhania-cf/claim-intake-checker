"""Rule runner.

``run_checks(conn, run_id, claim_ids)`` runs every rule in ``app.rules`` over
the given claims and writes one ``check_results`` row per claim plus one
``flags`` row per flag. It is the middle step of the run contract in
``docs/decisions.md``:

    intake.create_run -> runner.run_checks -> intake.record_intake_flags

all inside intake's single transaction. So this module never commits, rolls
back or creates a run, and only touches the claim_ids it is given. Resubmitted
and malformed rows are intake's to record.

Idempotent per run: a claim that already has a result in ``run_id`` is
skipped, so calling ``run_checks`` again with the same arguments writes
nothing. The schema backs this with unique indexes on ``check_results
(run_id, claim_id)`` and ``flags (run_id, claim_id, rule_id)``. Re-checking a
claim later means a new run, which adds new rows next to the old ones.

Rules are pure; this is the only layer that reads the DB to build their
inputs. History is append-only: rows are inserted, never updated or deleted.
"""

from __future__ import annotations

import sqlite3

from app import config, db
from app.models import Claim, Outcome, Policy, RuleContext
from app.rules import ALL_RULES


def _load_claim(conn: sqlite3.Connection, claim_id: str) -> tuple[int, Claim]:
    row = conn.execute(
        "SELECT rowid AS seq, * FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"run_checks was given claim_id {claim_id!r}, which is not stored")
    return row["seq"], db.row_to_claim(row)


def _earlier_claims_on_policy(
    conn: sqlite3.Connection, policy_number: str, seq: int
) -> list[Claim]:
    """Claims on the same policy stored before this one, oldest first.

    "Earlier" is insertion order (SQLite rowid), not claim_id order: form
    claim_ids are free-form, so sorting them says nothing about which came
    first. Passing only earlier claims means the later claim of a duplicate
    pair gets the R3 flag and the original does not.
    """
    rows = conn.execute(
        "SELECT * FROM claims WHERE policy_number = ? AND rowid < ? ORDER BY rowid",
        (policy_number, seq),
    ).fetchall()
    return [db.row_to_claim(r) for r in rows]


def _already_checked(conn: sqlite3.Connection, run_id: str, claim_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM check_results WHERE run_id = ? AND claim_id = ?", (run_id, claim_id)
    ).fetchone()
    return row is not None


def run_checks(conn: sqlite3.Connection, run_id: str, claim_ids: list[str]) -> None:
    """Check each claim against ALL_RULES and record its result and flags.

    Expects the run and the claims to exist already. Claims already checked in
    this run, and repeats within ``claim_ids``, are skipped. Raises
    ``LookupError`` (leaving the caller to roll back) if a claim_id is not
    stored.
    """
    policies: dict[str, Policy | None] = {}
    for claim_id in claim_ids:
        if _already_checked(conn, run_id, claim_id):
            continue
        seq, claim = _load_claim(conn, claim_id)
        if claim.policy_number not in policies:
            policies[claim.policy_number] = db.get_policy(conn, claim.policy_number)
        policy = policies[claim.policy_number]
        ctx = RuleContext(
            existing_claims=_earlier_claims_on_policy(conn, claim.policy_number, seq),
            config=config,
        )

        flags = [flag for rule in ALL_RULES if (flag := rule(claim, policy, ctx)) is not None]
        outcome: Outcome = "NEEDS_REVIEW" if flags else "READY_FOR_ADJUSTER"
        db.insert_result(conn, run_id, claim_id, outcome)
        for flag in flags:
            db.insert_flag(conn, run_id, claim_id, flag)
