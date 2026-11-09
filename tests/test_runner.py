"""Tests for app/runner.py (CIC-28): run_checks and the live intake paths.

All data is synthetic: built with the rule-test helpers or by
scripts/generate_data.py.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app, get_db
from app.models import Flag
from app.runner import run_checks
from scripts import generate_data
from tests.rules.helpers import MID_TERM, make_claim, make_policy
from tests.test_claim_form import form_data


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.get_connection(tmp_path / "test.db")
    db.init_db(connection)
    with connection:
        db.insert_policy(connection, make_policy())
    yield connection
    connection.close()


def store(conn: sqlite3.Connection, *claims) -> str:  # type: ignore[no-untyped-def]
    """Insert claims and a run the way intake does, and return the run_id."""
    with conn:
        run_id = db.insert_run(conn, "csv", len(claims))
        for claim in claims:
            db.insert_claim(conn, claim)
    return run_id


def results(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    rows = conn.execute("SELECT claim_id, outcome FROM check_results WHERE run_id = ?", (run_id,))
    return {r["claim_id"]: r["outcome"] for r in rows}


def flags(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM flags WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


def test_clean_claim_is_ready_and_flagged_claim_needs_review(conn: sqlite3.Connection) -> None:
    clean = make_claim(claim_id="TEST-CLAIM-0001")
    unknown = make_claim(claim_id="TEST-CLAIM-0002", policy_number="TEST-POLICY-MISSING")
    run_id = store(conn, clean, unknown)

    with conn:
        run_checks(conn, run_id, [clean.claim_id, unknown.claim_id])

    assert results(conn, run_id) == {
        "TEST-CLAIM-0001": "READY_FOR_ADJUSTER",
        "TEST-CLAIM-0002": "NEEDS_REVIEW",
    }
    (flag,) = flags(conn, run_id)
    assert (flag["claim_id"], flag["rule_id"]) == ("TEST-CLAIM-0002", "R0")
    assert "TEST-POLICY-MISSING" in flag["reason"]
    assert json.loads(flag["inputs_json"]) == {"policy_number": "TEST-POLICY-MISSING"}


def test_one_flag_row_per_flag(conn: sqlite3.Connection) -> None:
    with conn:
        db.insert_policy(conn, make_policy(policy_number="TEST-POLICY-TPO", cover_type="tpo"))
    claim = make_claim(
        policy_number="TEST-POLICY-TPO", loss_type="glass", report_date=MID_TERM + timedelta(days=45)
    )
    run_id = store(conn, claim)

    with conn:
        run_checks(conn, run_id, [claim.claim_id])

    assert [f["rule_id"] for f in flags(conn, run_id)] == ["R1b", "R2"]
    assert results(conn, run_id) == {claim.claim_id: "NEEDS_REVIEW"}


@pytest.mark.parametrize("order", [["first", "second"], ["second", "first"]])
def test_later_duplicate_gets_the_flag_by_submission_order(
    conn: sqlite3.Connection, order: list[str]
) -> None:
    # The original's id sorts *after* the duplicate's, so claim_id order would
    # pick the wrong one; submission order must win.
    first = make_claim(claim_id="TEST-CLAIM-ZZZZ")
    second = make_claim(claim_id="TEST-CLAIM-AAAA")
    run_id = store(conn, first, second)
    ids = {"first": first.claim_id, "second": second.claim_id}

    with conn:
        run_checks(conn, run_id, [ids[o] for o in order])

    (flag,) = flags(conn, run_id)
    assert (flag["claim_id"], flag["rule_id"]) == ("TEST-CLAIM-AAAA", "R3")
    assert json.loads(flag["inputs_json"])["earlier_claim_id"] == "TEST-CLAIM-ZZZZ"
    assert results(conn, run_id)["TEST-CLAIM-ZZZZ"] == "READY_FOR_ADJUSTER"


def test_duplicate_of_a_claim_from_an_earlier_run(conn: sqlite3.Connection) -> None:
    store(conn, make_claim(claim_id="TEST-CLAIM-0001"))
    later = make_claim(claim_id="TEST-CLAIM-0002")
    run_id = store(conn, later)

    with conn:
        run_checks(conn, run_id, [later.claim_id])

    assert [f["rule_id"] for f in flags(conn, run_id)] == ["R3"]


def test_updated_claim_keeps_its_place_for_r3(conn: sqlite3.Connection) -> None:
    """An overwrite is an UPDATE, so the claim keeps its submission order: the
    later-stored claim gets R3, not the one that was updated to match it."""
    first = make_claim(claim_id="TEST-CLAIM-0001", loss_date=MID_TERM - timedelta(days=3))
    second = make_claim(claim_id="TEST-CLAIM-0002")
    store(conn, first, second)
    with conn:
        run_id = db.insert_run(conn, "csv", 1)
        db.insert_claim_version(conn, run_id, first)
        db.update_claim(conn, make_claim(claim_id="TEST-CLAIM-0001"))  # now the same loss_date as 0002
        run_checks(conn, run_id, ["TEST-CLAIM-0001", "TEST-CLAIM-0002"])

    (flag,) = flags(conn, run_id)
    assert (flag["claim_id"], flag["rule_id"]) == ("TEST-CLAIM-0002", "R3")
    assert results(conn, run_id)["TEST-CLAIM-0001"] == "READY_FOR_ADJUSTER"


def test_only_given_claims_are_checked_and_no_run_or_commit(conn: sqlite3.Connection) -> None:
    run_id = store(conn, make_claim(claim_id="TEST-CLAIM-0001"), make_claim(claim_id="TEST-CLAIM-0002"))

    conn.execute("BEGIN")
    run_checks(conn, run_id, ["TEST-CLAIM-0002"])
    assert conn.in_transaction  # nothing committed inside run_checks
    assert list(results(conn, run_id)) == ["TEST-CLAIM-0002"]
    assert conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] == 1
    conn.rollback()

    assert results(conn, run_id) == {}


def test_unknown_claim_id_raises(conn: sqlite3.Connection) -> None:
    run_id = store(conn)
    with pytest.raises(LookupError, match="TEST-CLAIM-NOPE"):
        run_checks(conn, run_id, ["TEST-CLAIM-NOPE"])


# --- live app: the real runner behind both intake paths ------------------------


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """The app with only the DB swapped; get_run_checks is the real one."""
    path = tmp_path / "app.db"

    async def test_db() -> AsyncIterator[sqlite3.Connection]:
        connection = db.get_connection(path)
        db.init_db(connection)
        yield connection
        connection.close()

    app.dependency_overrides[get_db] = test_db
    yield TestClient(app), path
    app.dependency_overrides.clear()


def test_form_submission_is_checked_by_the_real_runner(live: tuple[TestClient, Path]) -> None:
    client, path = live
    response = client.post("/intake/claim", data=form_data())

    assert response.status_code == 200
    conn = db.get_connection(path)
    outcome = conn.execute("SELECT outcome FROM check_results").fetchall()
    conn.close()
    assert [r["outcome"] for r in outcome] == ["NEEDS_REVIEW"]  # no policy stored: R0


def test_demo_csv_is_checked_by_the_real_runner(live: tuple[TestClient, Path], tmp_path: Path) -> None:
    client, path = live
    data = generate_data.generate()
    conn = db.get_connection(path)
    db.init_db(conn)
    with conn:
        for policy in data.policies:
            db.insert_policy(conn, policy)
    generate_data.write_csvs(tmp_path, data)

    upload = (tmp_path / "claims.csv").read_bytes()
    response = client.post("/intake/csv", files={"file": ("claims.csv", upload, "text/csv")})

    assert response.status_code == 200
    counts = dict(conn.execute("SELECT outcome, COUNT(*) FROM check_results GROUP BY outcome").fetchall())
    rules = {r[0] for r in conn.execute("SELECT DISTINCT rule_id FROM flags")}
    conn.close()
    assert sum(counts.values()) == 506  # 500 checked + 6 malformed intake rows
    assert {"R0", "R1", "R1b", "R2", "R3", "INTAKE"} <= rules
    # 6 each of S1, S2, S3, S5, S6, S7 are flagged by rules; 6 by intake.
    assert counts["NEEDS_REVIEW"] == 6 * 6 + 6


# --- idempotent re-runs ---------------------------------------------------------


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM check_results").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0],
    )


def test_same_run_twice_writes_nothing_more(conn: sqlite3.Connection) -> None:
    claim = make_claim(policy_number="TEST-POLICY-MISSING")
    run_id = store(conn, claim)

    with conn:
        run_checks(conn, run_id, [claim.claim_id])
    with conn:
        run_checks(conn, run_id, [claim.claim_id])

    assert counts(conn) == (1, 1)


def test_repeated_claim_id_in_one_call_is_checked_once(conn: sqlite3.Connection) -> None:
    claim = make_claim(policy_number="TEST-POLICY-MISSING")
    run_id = store(conn, claim)

    with conn:
        run_checks(conn, run_id, [claim.claim_id, claim.claim_id])

    assert counts(conn) == (1, 1)


def test_new_run_rechecks_and_keeps_the_old_rows(conn: sqlite3.Connection) -> None:
    claim = make_claim(policy_number="TEST-POLICY-MISSING")
    first = store(conn, claim)
    with conn:
        run_checks(conn, first, [claim.claim_id])
        second = db.insert_run(conn, "csv", 1)
        run_checks(conn, second, [claim.claim_id])

    assert counts(conn) == (2, 2)
    assert results(conn, first) == results(conn, second) == {claim.claim_id: "NEEDS_REVIEW"}


@pytest.mark.parametrize(
    "insert",
    [
        lambda c, r: db.insert_result(c, r, "TEST-CLAIM-0001", "NEEDS_REVIEW"),
        lambda c, r: db.insert_flag(c, r, "TEST-CLAIM-0001", Flag("R0", "Test", {})),
    ],
    ids=["check_results", "flags"],
)
def test_schema_rejects_a_second_row_for_the_same_run(conn: sqlite3.Connection, insert) -> None:  # type: ignore[no-untyped-def]
    run_id = store(conn)
    with conn:
        insert(conn, run_id)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(conn, run_id)
