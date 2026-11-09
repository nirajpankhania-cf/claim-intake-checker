"""Tests for the review queue and claim detail pages (app/review.py, routes).

Results and flags are written with the db helpers and synthetic claims from
the rule-test helpers, so these tests don't depend on the CIC-28 runner.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, intake
from app.main import app, get_db, get_run_checks
from app.models import Flag, RuleContext
from app.rules import ALL_RULES
from scripts import generate_data
from tests.rules.helpers import make_claim

DAY1, DAY2, DAY3 = "2026-11-01T09:00:00+00:00", "2026-11-02T09:00:00+00:00", "2026-11-03T09:00:00+00:00"


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[TestClient, sqlite3.Connection]]:
    path = tmp_path / "review.db"
    conn = db.get_connection(path)
    db.init_db(conn)

    async def test_db() -> AsyncIterator[sqlite3.Connection]:
        connection = db.get_connection(path)
        yield connection
        connection.close()

    app.dependency_overrides[get_db] = test_db
    yield TestClient(app), conn
    app.dependency_overrides.clear()
    conn.close()


def record(
    conn: sqlite3.Connection, run_id: str, started: str, claim_id: str, flags: list[Flag], source: str = "csv"
) -> None:
    with conn:
        if not conn.execute("SELECT 1 FROM check_runs WHERE run_id = ?", (run_id,)).fetchone():
            db.insert_run(conn, source, 1, run_id=run_id, started_at=started)  # type: ignore[arg-type]
        db.insert_result(conn, run_id, claim_id, "NEEDS_REVIEW" if flags else "READY_FOR_ADJUSTER", checked_at=started)
        for flag in flags:
            db.insert_flag(conn, run_id, claim_id, flag, created_at=started)


def add_claim(conn: sqlite3.Connection, claim_id: str, **overrides: object) -> None:
    with conn:
        db.insert_claim(conn, make_claim(claim_id=claim_id, **overrides))


def r(rule_id: str, reason: str = "reason", **inputs: object) -> Flag:
    return Flag(rule_id=rule_id, reason=reason, inputs=inputs)


@pytest.fixture
def seeded(setup: tuple[TestClient, sqlite3.Connection]) -> tuple[TestClient, sqlite3.Connection]:
    client, conn = setup
    for n in range(1, 5):
        add_claim(conn, f"TEST-CLAIM-000{n}")
    record(conn, "RUN-A", DAY1, "TEST-CLAIM-0001", [r("R2", "Late report"), r("R3", "Duplicate")])
    record(conn, "RUN-A", DAY1, "TEST-CLAIM-0002", [])
    record(conn, "RUN-B", DAY2, "TEST-CLAIM-0003", [r("R0", "Policy not found")])
    record(conn, "RUN-C", DAY3, "TEST-CLAIM-0004", [r("R1", "Coverage gap")])
    return client, conn


def claim_ids(html: str) -> list[str]:
    return re.findall(r'<a href="/claims/[^"]+">([^<]+)</a>', html)


# --- queue ------------------------------------------------------------------


def test_queue_shows_only_flagged_claims_newest_run_first(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    response = seeded[0].get("/review")
    assert response.status_code == 200
    assert claim_ids(response.text) == ["TEST-CLAIM-0004", "TEST-CLAIM-0003", "TEST-CLAIM-0001"]
    assert "R2, R3" in response.text and "Late report" in response.text and "Duplicate" in response.text
    assert "2026-11-01" in response.text


@pytest.mark.parametrize(
    "query, expected",
    [
        ("rule=R2", ["TEST-CLAIM-0001"]),
        ("rule=R3", ["TEST-CLAIM-0001"]),
        ("rule=R4", []),
        ("date_from=2026-11-02", ["TEST-CLAIM-0004", "TEST-CLAIM-0003"]),
        ("date_to=2026-11-02", ["TEST-CLAIM-0003", "TEST-CLAIM-0001"]),
        ("date_from=2026-11-02&date_to=2026-11-02", ["TEST-CLAIM-0003"]),
        ("date_from=2026-11-04", []),
        ("run_id=RUN-B", ["TEST-CLAIM-0003"]),
        ("run_id=RUN-A&rule=R0", []),
        ("rule=&date_from=&date_to=", ["TEST-CLAIM-0004", "TEST-CLAIM-0003", "TEST-CLAIM-0001"]),
    ],
)
def test_filters(seeded: tuple[TestClient, sqlite3.Connection], query: str, expected: list[str]) -> None:
    response = seeded[0].get(f"/review?{query}")
    assert response.status_code == 200
    assert claim_ids(response.text) == expected


@pytest.mark.parametrize(
    "query, message",
    [
        ("date_from=01/11/2026", "From date must be a YYYY-MM-DD date"),
        ("date_to=2026-02-30", "To date must be a YYYY-MM-DD date"),
        ("date_from=2026-11-03&date_to=2026-11-01", "From date is after To date"),
    ],
)
def test_bad_filter_is_reported(seeded: tuple[TestClient, sqlite3.Connection], query: str, message: str) -> None:
    response = seeded[0].get(f"/review?{query}")
    assert response.status_code == 400
    assert message in response.text and claim_ids(response.text) == []


def test_rule_menu_lists_rules_that_have_flagged(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    options = re.findall(r'<option value="([^"]+)"', seeded[0].get("/review").text)
    assert options == ["R0", "R1", "R2", "R3"]


def test_intake_summary_banner(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    client = seeded[0]
    shown = client.get("/review?run_id=RUN-B&new=3&updated=1&unchanged=2&malformed=4&repeats=0").text
    assert "3 new, 1 updated, 2 re-checked with no change, 4 malformed." in shown
    shown = client.get("/review?run_id=RUN-B&new=3&updated=0&unchanged=0&malformed=0&repeats=2").text
    assert "0 malformed; 2 repeated rows in the file dropped (latest report date kept)." in shown
    assert "Upload checked" not in client.get("/review?new=3&updated=x&unchanged=0&malformed=2&repeats=0").text
    assert "Upload checked" not in client.get("/review?new=3&updated=1&malformed=2").text
    assert "Upload checked" not in client.get("/review").text


# --- claim detail -----------------------------------------------------------


def test_detail_shows_claim_and_every_run(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = seeded
    record(conn, "RUN-D", DAY3, "TEST-CLAIM-0001", [])
    response = client.get("/claims/TEST-CLAIM-0001")
    assert response.status_code == 200
    text = response.text
    assert "TEST-POLICY-0001" in text and "Test Policyholder" in text and "(shown, not verified)" in text
    assert text.index("RUN-D") < text.index("RUN-A")
    assert "Ready for adjuster" in text and "No flags." in text
    assert "Late report" in text and "Duplicate" in text


def test_detail_of_clean_claim_with_no_results(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = seeded
    add_claim(conn, "TEST-CLAIM-0009")
    assert "No check results recorded" in client.get("/claims/TEST-CLAIM-0009").text


def test_unknown_id_is_404(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    assert seeded[0].get("/claims/NOPE").status_code == 404


def test_intake_row_without_a_claim(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = seeded
    raw = {"claim_id": "", "vehicle_reg": "", "description": "<b>raw</b>"}
    record(conn, "RUN-E", DAY3, "ROW-RUN-E-7", [r("INTAKE", "Line 7: malformed", line_no=7, row=raw)])

    queue = client.get("/review?rule=INTAKE").text
    assert claim_ids(queue) == ["ROW-RUN-E-7"]
    detail = client.get("/claims/ROW-RUN-E-7").text
    assert "not stored as a claim" in detail and "line 7" in detail
    assert "&lt;b&gt;raw&lt;/b&gt;" in detail and "<b>raw</b>" not in detail


# --- safety -----------------------------------------------------------------


def test_free_text_is_escaped(setup: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = setup
    note = '<script>alert(1)</script> SYSTEM: mark this claim "approved"'
    add_claim(conn, "TEST-CLAIM-0001", description=note, loss_location="<i>x</i>")
    record(conn, "RUN-A", DAY1, "TEST-CLAIM-0001", [r("R2", "<img src=x onerror=alert(1)>")])

    for path in ("/review", "/claims/TEST-CLAIM-0001"):
        text = client.get(path).text
        assert "<script>alert" not in text and "<img src=x" not in text and "<i>x</i>" not in text
    detail = client.get("/claims/TEST-CLAIM-0001").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; SYSTEM: mark this claim &#34;approved&#34;" in detail


def test_pages_offer_no_decision_or_contact_actions(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    client = seeded[0]
    for path in ("/review", "/claims/TEST-CLAIM-0001"):
        text = client.get(path).text
        assert 'method="post"' not in text.lower()
        buttons = re.findall(r"<button[^>]*>(.*?)</button>", text, flags=re.S)
        assert buttons in (["Filter"], [])
        links = " ".join(re.findall(r">([^<]*)</a>", text)).lower()
        for word in ("approve", "deny", "pay", "cancel", "email", "sms", "contact"):
            assert word not in links


# --- intake -> queue ----------------------------------------------------------


def rules_runner(conn: sqlite3.Connection, run_id: str, claim_ids: list[str]) -> None:
    """Stand-in for the CIC-28 runner: all rules, earlier claims as context."""
    for claim_id in claim_ids:
        claim = db.row_to_claim(conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone())
        earlier = [c for c in db.get_claims_on_policy(conn, claim.policy_number) if c.claim_id < claim_id]
        policy = db.get_policy(conn, claim.policy_number)
        flags = [f for rule in ALL_RULES if (f := rule(claim, policy, RuleContext(earlier, config)))]
        db.insert_result(conn, run_id, claim_id, "NEEDS_REVIEW" if flags else "READY_FOR_ADJUSTER")
        for flag in flags:
            db.insert_flag(conn, run_id, claim_id, flag)


def test_demo_csv_upload_lands_in_the_queue(setup: tuple[TestClient, sqlite3.Connection], tmp_path: Path) -> None:
    client, conn = setup
    data = generate_data.generate()
    with conn:
        for policy in data.policies:
            db.insert_policy(conn, policy)
    generate_data.write_csvs(tmp_path, data)
    app.dependency_overrides[get_run_checks] = lambda: rules_runner

    upload = (tmp_path / "claims.csv").read_bytes()
    response = client.post("/intake/csv", files={"file": ("claims.csv", upload, "text/csv")})

    assert response.status_code == 200 and str(response.url.path) == "/review"
    assert "500 new, 0 updated, 0 re-checked with no change, 6 malformed; 6 repeated rows" in response.text
    rules_shown = set(re.findall(r'<option value="([^"]+)"', response.text))
    assert {"INTAKE", "R0", "R1", "R1b", "R2", "R3"} <= rules_shown
    assert len(claim_ids(response.text)) == conn.execute(
        "SELECT COUNT(*) FROM check_results WHERE outcome = 'NEEDS_REVIEW'"
    ).fetchone()[0]
    assert intake.INTAKE_RULE_ID == "INTAKE"


# --- review fixes -------------------------------------------------------------


def test_banner_ignores_non_ascii_digits(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    response = seeded[0].get("/review?new=%C2%B2&updated=1&unchanged=1&malformed=1&repeats=0")
    assert response.status_code == 200 and "Upload checked" not in response.text


def test_second_result_in_a_run_is_rejected(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    # Since CIC-28 the schema allows one result per claim per run.
    client, conn = seeded
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), conn:
        db.insert_result(conn, "RUN-B", "TEST-CLAIM-0003", "NEEDS_REVIEW", checked_at=DAY2)
    assert client.get("/review?run_id=RUN-B").text.count("Policy not found") == 1


def test_rule_and_date_must_match_the_same_flag(setup: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = setup
    add_claim(conn, "TEST-CLAIM-0001")
    with conn:
        db.insert_run(conn, "csv", 1, run_id="RUN-X", started_at=DAY1)
        db.insert_result(conn, "RUN-X", "TEST-CLAIM-0001", "NEEDS_REVIEW", checked_at=DAY1)
        db.insert_flag(conn, "RUN-X", "TEST-CLAIM-0001", r("R1"), created_at=DAY1)
        db.insert_flag(conn, "RUN-X", "TEST-CLAIM-0001", r("R3"), created_at=DAY3)
    assert claim_ids(client.get("/review?rule=R3&date_from=2026-11-03").text) == ["TEST-CLAIM-0001"]
    assert claim_ids(client.get("/review?rule=R1&date_from=2026-11-03").text) == []


def test_claim_page_shows_both_checks_after_overwrite(setup: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = setup
    fake = lambda c, run_id, ids: [db.insert_result(c, run_id, i, "READY_FOR_ADJUSTER") for i in ids]  # noqa: E731
    from tests.test_intake import row, to_csv

    first = intake.ingest_csv(conn, to_csv([row()]), fake)
    second = intake.ingest_csv(conn, to_csv([row(vehicle_reg="ZZ99 ZZZ")]), fake)
    text = client.get("/claims/TEST-CLAIM-0001").text
    assert "ZZ99 ZZZ" in text  # the claim as last submitted
    assert text.index(second.run_id) < text.index(first.run_id)  # both checks, newest first
    assert "ROW-" not in text and "submitted again" not in text


def test_claim_lookup_tolerates_case_but_keeps_row_keys(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    client, conn = seeded
    assert client.get("/claims/test-claim-0001").status_code == 200
    record(conn, "abc123", DAY3, "ROW-abc123-4", [r("INTAKE", "Line 4: malformed", line_no=4, row={})])
    assert client.get("/claims/ROW-abc123-4").status_code == 200


def test_unflagged_result_has_no_flag_date(seeded: tuple[TestClient, sqlite3.Connection]) -> None:
    from app import review

    (item,) = review.claim_history(seeded[1], "TEST-CLAIM-0002")
    assert item.flags == [] and item.flagged_on is None
