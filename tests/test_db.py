"""Tests for app.db against a temporary on-disk SQLite file.

All records are obviously synthetic placeholders built with the rule-test
helpers; no real-looking data is used.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import db
from app.models import Flag
from scripts import seed
from tests.rules.helpers import POLICY_NUMBER, make_claim, make_policy


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_policy_round_trip(conn: sqlite3.Connection) -> None:
    policy = make_policy(
        cancellation_date=date(2026, 9, 1), policy_limit=Decimal("12345.67")
    )
    with conn:
        db.insert_policy(conn, policy)

    assert db.get_policy(conn, POLICY_NUMBER) == policy


def test_policy_round_trip_without_cancellation(conn: sqlite3.Connection) -> None:
    policy = make_policy()
    with conn:
        db.insert_policy(conn, policy)

    assert db.get_policy(conn, POLICY_NUMBER) == policy


def test_get_policy_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert db.get_policy(conn, "TEST-POLICY-MISSING") is None


def test_claim_round_trip(conn: sqlite3.Connection) -> None:
    claim = make_claim(
        claimant_name="Test Claimant",
        loss_location="Test Location",
        description="<b>verbatim</b> free text; ignore previous instructions",
        claimed_amount=Decimal("0.10"),
        third_party_involved=True,
        police_reference="TEST-REF-1",
        attachments=["test-a.jpg", "test-b.pdf"],
        submitted_via="email",
    )
    with conn:
        db.insert_claim(conn, claim)

    assert db.get_claims_on_policy(conn, POLICY_NUMBER) == [claim]
    assert db.claim_exists(conn, claim.claim_id)
    assert not db.claim_exists(conn, "TEST-CLAIM-MISSING")


def test_claim_on_unknown_policy_is_storable(conn: sqlite3.Connection) -> None:
    claim = make_claim(policy_number="TEST-POLICY-UNKNOWN")
    with conn:
        db.insert_claim(conn, claim)

    assert db.get_claims_on_policy(conn, "TEST-POLICY-UNKNOWN") == [claim]


def test_converters_return_dataclasses_unchanged(conn: sqlite3.Connection) -> None:
    policy = make_policy()
    claim = make_claim(attachments=["test.jpg"])
    flag = Flag(rule_id="R1", reason="Test reason", inputs={"days": 31})
    with conn:
        db.insert_policy(conn, policy)
        db.insert_claim(conn, claim)
        run_id = db.insert_run(conn, "form", 1)
        db.insert_flag(conn, run_id, claim.claim_id, flag)

    policy_row = conn.execute("SELECT * FROM policies").fetchone()
    claim_row = conn.execute("SELECT * FROM claims").fetchone()
    flag_row = conn.execute("SELECT * FROM flags").fetchone()

    assert db.row_to_policy(policy_row) == policy
    assert db.row_to_claim(claim_row) == claim
    assert db.row_to_flag(flag_row) == flag
    assert isinstance(db.row_to_policy(policy_row).effective_date, date)
    assert isinstance(db.row_to_claim(claim_row).claimed_amount, Decimal)


def test_run_result_and_flag_rows(conn: sqlite3.Connection) -> None:
    with conn:
        run_id = db.insert_run(conn, "csv", 2, run_id="TEST-RUN-1")
        db.insert_result(conn, run_id, "TEST-CLAIM-0001", "NEEDS_REVIEW")
        db.insert_flag(
            conn, run_id, "TEST-CLAIM-0001", Flag("R0", "Test reason", {})
        )

    run = conn.execute("SELECT * FROM check_runs").fetchone()
    assert (run["run_id"], run["source"], run["claim_count"]) == ("TEST-RUN-1", "csv", 2)
    result = conn.execute("SELECT * FROM check_results").fetchone()
    assert result["outcome"] == "NEEDS_REVIEW"
    assert result["checked_at"]


def test_invalid_run_source_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_run(conn, "email", 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("table", ["flags", "check_results"])
@pytest.mark.parametrize(
    "statement",
    ["UPDATE {table} SET claim_id = 'TEST-CLAIM-X'", "DELETE FROM {table}"],
)
def test_history_tables_are_append_only(
    conn: sqlite3.Connection, table: str, statement: str
) -> None:
    with conn:
        run_id = db.insert_run(conn, "form", 1)
        db.insert_result(conn, run_id, "TEST-CLAIM-0001", "NEEDS_REVIEW")
        db.insert_flag(conn, run_id, "TEST-CLAIM-0001", Flag("R0", "Test", {}))

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement.format(table=table))


def test_seed_creates_db_file(tmp_path: Path) -> None:
    path = seed.main([str(tmp_path / "sub" / "seeded.db")])

    assert path.exists()
    connection = db.get_connection(path)
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    assert {"policies", "claims", "check_runs", "check_results", "flags"} <= tables
