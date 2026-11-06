"""Tests for the history page and CSV export (CIC-31).

Reuses the review-page fixtures: four synthetic claims over three runs, one
passed (TEST-CLAIM-0002) and three flagged.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, history
from app.main import app, get_run_checks
from scripts import generate_data
from tests.test_review import DAY1, add_claim, claim_ids, r, record, rules_runner, seeded, setup  # noqa: F401

Client = tuple[TestClient, sqlite3.Connection]
ALL = ["TEST-CLAIM-0004", "TEST-CLAIM-0003", "TEST-CLAIM-0001", "TEST-CLAIM-0002"]


def csv_rows(text: str) -> list[dict[str, str]]:
    assert text.startswith(history.BOM)
    return list(csv.DictReader(io.StringIO(text.removeprefix(history.BOM))))


# --- page -------------------------------------------------------------------


def test_history_lists_passed_and_flagged_with_totals(seeded: Client) -> None:
    response = seeded[0].get("/history")
    assert response.status_code == 200
    assert claim_ids(response.text) == ALL
    assert "4 claims checked: 1 ready for adjuster, 3 need review." in response.text
    assert "Ready for adjuster" in response.text and "Late report" in response.text


@pytest.mark.parametrize(
    "query, expected",
    [
        ("outcome=READY_FOR_ADJUSTER", ["TEST-CLAIM-0002"]),
        ("outcome=NEEDS_REVIEW", ["TEST-CLAIM-0004", "TEST-CLAIM-0003", "TEST-CLAIM-0001"]),
        ("date_from=2026-11-02", ["TEST-CLAIM-0004", "TEST-CLAIM-0003"]),
        ("date_to=2026-11-01", ["TEST-CLAIM-0001", "TEST-CLAIM-0002"]),
        ("date_from=2026-11-02&date_to=2026-11-02", ["TEST-CLAIM-0003"]),
        ("run_id=RUN-A", ["TEST-CLAIM-0001", "TEST-CLAIM-0002"]),
        ("run_id=RUN-A&outcome=NEEDS_REVIEW", ["TEST-CLAIM-0001"]),
        ("run_id=&date_from=&date_to=&outcome=", ALL),
    ],
)
def test_filters(seeded: Client, query: str, expected: list[str]) -> None:
    response = seeded[0].get(f"/history?{query}")
    assert response.status_code == 200
    assert claim_ids(response.text) == expected


@pytest.mark.parametrize(
    "query, message",
    [
        ("outcome=APPROVED", "Outcome must be"),
        ("date_from=2026-13-01", "From date must be a YYYY-MM-DD date"),
        ("date_from=2026-11-03&date_to=2026-11-01", "From date is after To date"),
    ],
)
def test_bad_filter(seeded: Client, query: str, message: str) -> None:
    page = seeded[0].get(f"/history?{query}")
    assert page.status_code == 400 and message in page.text and claim_ids(page.text) == []
    export = seeded[0].get(f"/history.csv?{query}")
    assert export.status_code == 400 and message in export.text


def test_run_picker_lists_runs_newest_first(seeded: Client) -> None:
    text = seeded[0].get("/history?run_id=RUN-B").text
    assert re.findall(r'<option value="(RUN-[A-Z])"', text) == ["RUN-C", "RUN-B", "RUN-A"]
    assert '<option value="RUN-B" selected>' in text


def test_intake_rows_appear_in_history(seeded: Client) -> None:
    client, conn = seeded
    record(conn, "RUN-E", "2026-11-04T09:00:00+00:00", "ROW-RUN-E-7", [r("INTAKE", "Line 7: malformed", row={})])
    text = client.get("/history?run_id=RUN-E").text
    assert claim_ids(text) == ["ROW-RUN-E-7"] and "INTAKE" in text


def test_page_escapes_text_and_offers_no_actions(setup: Client) -> None:  # noqa: F811
    client, conn = setup
    add_claim(conn, "TEST-CLAIM-0001")
    record(conn, "RUN-A", DAY1, "TEST-CLAIM-0001", [r("R2", "<script>alert(1)</script>")])
    text = client.get("/history").text
    assert "<script>alert" not in text and "&lt;script&gt;" in text
    assert 'method="post"' not in text.lower()
    assert re.findall(r"<button[^>]*>(.*?)</button>", text) == ["Filter"]


# --- export -----------------------------------------------------------------


def test_export_matches_page_and_carries_filters(seeded: Client) -> None:
    client = seeded[0]
    page = client.get("/history?run_id=RUN-A&outcome=NEEDS_REVIEW").text
    assert 'href="/history.csv?run_id=RUN-A&amp;outcome=NEEDS_REVIEW"' in page

    response = client.get("/history.csv?run_id=RUN-A&outcome=NEEDS_REVIEW")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    (row,) = csv_rows(response.text)
    assert row["claim_id"] == "TEST-CLAIM-0001" and row["policy_number"] == "TEST-POLICY-0001"
    assert row["outcome"] == "NEEDS_REVIEW" and row["rule_ids"] == "R2;R3"
    assert row["reasons"] == "Late report | Duplicate"
    assert row["checked_at"] == DAY1


def test_export_of_everything(seeded: Client) -> None:
    text = seeded[0].get("/history.csv").text
    assert text.splitlines()[0] == history.BOM + ",".join(history.COLUMNS)
    rows = csv_rows(text)
    assert [row["claim_id"] for row in rows] == ALL
    passed = rows[-1]
    assert (passed["outcome"], passed["rule_ids"], passed["reasons"]) == ("READY_FOR_ADJUSTER", "", "")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tx", "'\tx"),
        ("\n=HYPERLINK(1)", "'\n=HYPERLINK(1)"),
        ("  =1+1", "'  =1+1"),
        ("Claim was reported late", "Claim was reported late"),
        ("2026-11-01", "2026-11-01"),
        (None, ""),
    ],
)
def test_safe_cell(value: str | None, expected: str) -> None:
    assert history.safe_cell(value) == expected


def test_export_neutralises_formulas_from_submitted_text(setup: Client) -> None:  # noqa: F811
    client, conn = setup
    add_claim(conn, "TEST-CLAIM-0001")
    record(conn, "RUN-A", DAY1, "TEST-CLAIM-0001", [r("R2", "=cmd|' /C calc'!A0")])
    record(conn, "RUN-A", DAY1, "-ROW-1", [r("INTAKE", "@malformed")])
    rows = csv_rows(client.get("/history.csv").text)
    assert rows[0]["reasons"] == "'=cmd|' /C calc'!A0"
    assert (rows[1]["claim_id"], rows[1]["reasons"]) == ("'-ROW-1", "'@malformed")


# --- intake -> history ---------------------------------------------------------


def test_demo_upload_shows_every_row_in_history_and_export(setup: Client, tmp_path: Path) -> None:  # noqa: F811
    client, conn = setup
    data = generate_data.generate()
    with conn:
        for policy in data.policies:
            db.insert_policy(conn, policy)
    generate_data.write_csvs(tmp_path, data)
    app.dependency_overrides[get_run_checks] = lambda: rules_runner
    upload = (tmp_path / "claims.csv").read_bytes()
    assert client.post("/intake/csv", files={"file": ("claims.csv", upload, "text/csv")}).status_code == 200

    needs = conn.execute("SELECT COUNT(*) FROM check_results WHERE outcome = 'NEEDS_REVIEW'").fetchone()[0]
    page = client.get("/history").text
    assert f"512 claims checked: {512 - needs} ready for adjuster, {needs} need review." in page
    rows = csv_rows(client.get("/history.csv").text)
    assert len(rows) == 512
    assert sum(row["outcome"] == "NEEDS_REVIEW" for row in rows) == needs
    assert sum(row["rule_ids"] == "INTAKE" for row in rows) == 12


# --- review fixes -------------------------------------------------------------


def test_second_result_in_a_run_is_rejected(seeded: Client) -> None:
    # Since CIC-28 the schema allows one result per claim per run, so the
    # first result stands and history is unchanged.
    client, conn = seeded
    before = client.get("/history").text
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), conn:
        db.insert_result(conn, "RUN-A", "TEST-CLAIM-0002", "NEEDS_REVIEW", checked_at="2026-11-01T10:00:00+00:00")
    assert client.get("/history").text == before


def test_dates_are_utc(setup: Client) -> None:  # noqa: F811
    client, conn = setup
    add_claim(conn, "TEST-CLAIM-0001")
    record(conn, "RUN-A", "2026-11-02T00:30:00+01:00", "TEST-CLAIM-0001", [])
    assert claim_ids(client.get("/history?date_to=2026-11-01").text) == ["TEST-CLAIM-0001"]
    assert claim_ids(client.get("/history?date_from=2026-11-02").text) == []


def test_run_filter_is_trimmed_and_stays_selected(seeded: Client) -> None:
    text = seeded[0].get("/history?run_id=RUN-B%20").text
    assert claim_ids(text) == ["TEST-CLAIM-0003"]
    assert '<option value="RUN-B" selected>' in text
