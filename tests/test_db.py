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
from scripts import reset_db, seed
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


@pytest.mark.parametrize("table", ["flags", "check_results", "claim_versions"])
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
        db.insert_claim_version(conn, run_id, make_claim())

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
    assert {"policies", "claims", "check_runs", "check_results", "flags", "claim_versions"} <= tables


# --- current claim and its versions ------------------------------------------


def test_update_claim_keeps_rowid(conn: sqlite3.Connection) -> None:
    with conn:
        db.insert_claim(conn, make_claim(claim_id="TEST-CLAIM-0001"))
        db.insert_claim(conn, make_claim(claim_id="TEST-CLAIM-0002"))
    before = conn.execute("SELECT rowid FROM claims WHERE claim_id = 'TEST-CLAIM-0001'").fetchone()[0]
    changed = make_claim(claim_id="TEST-CLAIM-0001", vehicle_reg="ZZ99 ZZZ", attachments=["new.jpg"])
    with conn:
        db.update_claim(conn, changed)

    after = conn.execute("SELECT rowid FROM claims WHERE claim_id = 'TEST-CLAIM-0001'").fetchone()[0]
    assert after == before
    assert db.get_claim(conn, "TEST-CLAIM-0001") == changed
    assert db.get_claim(conn, "TEST-CLAIM-0002") == make_claim(claim_id="TEST-CLAIM-0002")
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2


def test_update_claim_unknown_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError, match="TEST-CLAIM-NOPE"):
        db.update_claim(conn, make_claim(claim_id="TEST-CLAIM-NOPE"))


def test_claim_version_round_trip_and_order(conn: sqlite3.Connection) -> None:
    first = make_claim(description="first", claimed_amount=Decimal("0.10"), attachments=["a.jpg"])
    second = make_claim(description="second")
    with conn:
        db.insert_claim(conn, first)
        run_a = db.insert_run(conn, "csv", 1)
        db.insert_claim_version(conn, run_a, first, replaced_at="2026-11-09T10:00:00+00:00")
        db.update_claim(conn, second)
        run_b = db.insert_run(conn, "form", 1)
        db.insert_claim_version(conn, run_b, second)
        db.update_claim(conn, make_claim(description="third"))

    versions = db.get_claim_versions(conn, first.claim_id)
    assert [v.claim for v in versions] == [first, second]
    assert [v.run_id for v in versions] == [run_a, run_b]
    assert versions[0].replaced_at.isoformat() == "2026-11-09T10:00:00+00:00"
    assert isinstance(versions[0].claim.claimed_amount, Decimal)
    assert db.get_claim_versions(conn, "TEST-CLAIM-NOPE") == []
    assert db.get_claim(conn, first.claim_id).description == "third"


def test_one_version_per_claim_per_run(conn: sqlite3.Connection) -> None:
    with conn:
        run_id = db.insert_run(conn, "csv", 1)
        db.insert_claim_version(conn, run_id, make_claim())
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.insert_claim_version(conn, run_id, make_claim())


# --- developer reset ---------------------------------------------------------


def test_reset_db_removes_file_and_sidecars(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "x.db"
    for name in ("x.db", "x.db-wal", "x.db-shm"):
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "keep.csv").write_text("a,b\n")

    assert reset_db.main([str(path)]) == path
    assert not any((tmp_path / name).exists() for name in ("x.db", "x.db-wal", "x.db-shm"))
    assert (tmp_path / "keep.csv").exists()
    assert "Removed" in capsys.readouterr().out
    reset_db.main([str(path)])  # already gone: fine
    assert "nothing to remove" in capsys.readouterr().out


def test_reset_db_refuses_non_db_path(tmp_path: Path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("keep me")
    with pytest.raises(SystemExit, match="does not look like a database file"):
        reset_db.main([str(other)])
    assert other.read_text() == "keep me"


def test_reset_db_defaults_to_configured_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "configured.db"
    path.write_bytes(b"")
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    assert reset_db.main([]) == path and not path.exists()
