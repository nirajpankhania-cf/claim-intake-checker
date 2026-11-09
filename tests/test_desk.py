"""Tests for the manager's review desk (CIC-41). All data is synthetic."""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, desk, intake
from app.main import app, get_run_checks
from app.models import Claim, Flag, Policy, RuleContext
from app.rules import ALL_RULES
from scripts import generate_data
from tests.rules.helpers import EXPIRATION, make_claim, make_policy
from tests.test_intake import FakeRunChecks, client, row, to_csv  # noqa: F401

Client = tuple[TestClient, FakeRunChecks, Path]
SENTENCE = "This records what the check found; it does not approve, deny, or pay anything."
REASON = "Claim was reported 40 days after the loss."


def flagging_runner(conn: sqlite3.Connection, run_id: str, claim_ids: list[str]) -> None:
    """The first claim needs review; the rest are ready."""
    for n, claim_id in enumerate(claim_ids):
        db.insert_result(conn, run_id, claim_id, "NEEDS_REVIEW" if n == 0 else "READY_FOR_ADJUSTER")
        if n == 0:
            flag = Flag(rule_id="R2", reason=REASON, inputs={"snapshot_key": "snapshot-value"})
            db.insert_flag(conn, run_id, claim_id, flag)


def mixed_csv() -> str:
    return to_csv(
        [
            row(claim_id="TEST-CLAIM-0001"),
            row(claim_id="TEST-CLAIM-0002"),
            row(claim_id="TEST-CLAIM-0001"),  # repeated in the file
            row(claim_id="", loss_date="15/06/2026"),
        ]
    )


def count(path: Path, table: str) -> int:
    conn = db.get_connection(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def upload(test_client: TestClient, text: str):  # type: ignore[no-untyped-def]
    files = {"file": ("claims.csv", text.encode(), "text/csv")}
    return test_client.post("/intake/csv", data={"next": "desk"}, files=files)


def tiles(html: str) -> dict[str, int]:
    """The headline counts and the count on each queue tab."""
    found = {label: int(n) for n, label in re.findall(r"<dd>(\d+)</dd><dt>([^<]+)</dt>", html)}
    found.update({label: int(n) for label, n in re.findall(r">([A-Za-z ]+) <b>(\d+)</b></a>", html)})
    return found


def visible(html: str) -> str:
    """The text a person sees, leaving out link addresses."""
    return re.sub(r"<[^>]+>", " ", html)


def tab(test_client: TestClient, response, queue: str) -> str:  # type: ignore[no-untyped-def]
    return test_client.get(f"{response.url.path}?queue={queue}").text


@pytest.fixture
def result(client: Client):  # type: ignore[no-untyped-def]  # noqa: F811
    app.dependency_overrides[get_run_checks] = lambda: flagging_runner
    return upload(client[0], mixed_csv())


# --- intake ------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"claim_id,wrong_column\nA,B\n", "columns a claims file needs"),
        (b"\xff\xfe not text", "be read as text"),
        (b"", "Choose a CSV file"),
    ],
)
def test_bad_file_stores_nothing(client: Client, payload: bytes, message: str) -> None:  # noqa: F811
    test_client, fake, path = client
    response = test_client.post(
        "/intake/csv", data={"next": "desk"}, files={"file": ("x.csv", payload, "text/csv")}
    )
    assert response.status_code == 400 and message in response.text and "Nothing was stored" in response.text
    assert "wrong_column" not in response.text
    assert count(path, "claims") == 0 and count(path, "check_runs") == 0 and fake.calls == []


def test_upload_goes_to_the_result_page(client: Client) -> None:  # noqa: F811
    response = client[0].post(
        "/intake/csv", data={"next": "desk", "csv_text": to_csv([row()])}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/desk/runs/")


def test_sample_load_calls_ingest_once(
    client: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    sample = tmp_path / "claims.csv"
    sample.write_text(to_csv([row()]), encoding="utf-8")
    monkeypatch.setattr(desk, "SAMPLE_CSV", sample)
    calls: list[str] = []
    real = intake.ingest_csv

    def counting(conn: sqlite3.Connection, text: str, run_checks: intake.RunChecks, **kw: bool) -> intake.IntakeSummary:
        calls.append(text)
        return real(conn, text, run_checks, **kw)

    monkeypatch.setattr(intake, "ingest_csv", counting)
    response = client[0].post("/desk/sample", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/desk/runs/")
    assert calls == [sample.read_text(encoding="utf-8")]


def test_missing_sample_says_run_make_seed(
    client: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(desk, "SAMPLE_CSV", tmp_path / "absent.csv")
    response = client[0].post("/desk/sample")
    assert response.status_code == 400 and "make seed" in response.text
    assert count(client[2], "check_runs") == 0


def test_every_upload_runs_the_checks_and_keeps_earlier_results(client: Client) -> None:  # noqa: F811
    test_client, fake, path = client
    bad = row(claim_id="TEST-CLAIM-0009", loss_date="bad")
    text = to_csv([row(), row(claim_id="TEST-CLAIM-0002"), row(), bad])
    upload(test_client, text)
    second = upload(test_client, text)
    assert fake.calls[1][1] == ["TEST-CLAIM-0001", "TEST-CLAIM-0002"]
    assert tiles(second.text)["Ready for adjuster"] == 2
    assert "2 of these claims had been checked before" in second.text
    assert count(path, "claims") == 2 and count(path, "check_runs") == 2
    assert count(path, "check_results") == 6  # 2 claims + 1 malformed row, twice; the in-file repeat is dropped


def test_changed_row_is_updated_and_rechecked(client: Client) -> None:  # noqa: F811
    test_client, fake, path = client
    upload(test_client, to_csv([row()]))
    response = upload(test_client, to_csv([row(vehicle_reg="ZZ99 ZZZ"), row(claim_id="", loss_date="bad")]))
    assert response.status_code == 200 and 'href="?queue=malformed"' in response.text
    assert fake.calls[1][1] == ["TEST-CLAIM-0001"]
    assert count(path, "claim_versions") == 1
    conn = db.get_connection(path)
    assert db.get_claim(conn, "TEST-CLAIM-0001").vehicle_reg == "ZZ99 ZZZ"
    conn.close()


def test_results_are_split_by_route_and_show_each_check(client: Client) -> None:  # noqa: F811
    def runner(conn: sqlite3.Connection, run_id: str, claim_ids: list[str]) -> None:
        for claim_id, rule in zip(claim_ids, ("R0", "R2", None)):
            db.insert_result(conn, run_id, claim_id, "NEEDS_REVIEW" if rule else "READY_FOR_ADJUSTER")
            if rule:
                db.insert_flag(conn, run_id, claim_id, Flag(rule_id=rule, reason=f"reason-{rule}", inputs={}))

    app.dependency_overrides[get_run_checks] = lambda: runner
    ids = ("TEST-CLAIM-0001", "TEST-CLAIM-0002", "TEST-CLAIM-0003")
    response = upload(client[0], to_csv([row(claim_id=i) for i in ids]))
    counted = tiles(response.text)
    assert (counted["Policy gates"], counted["Other checks"], counted["Ready for adjuster"]) == (1, 1, 1)
    gates, other = response.text, tab(client[0], response, "other")
    assert "reason-R0" in gates and "reason-R2" not in gates and "reason-R2" in other
    assert 'title="Policy exists: Flagged"' in gates and 'title="Loss date is within cover: Not checked"' in gates
    assert 'title="Reported on time: Passed"' in gates
    assert 'title="Reported on time: Flagged"' in other and 'title="Policy exists: Passed"' in other
    text = tab(client[0], response, "summary")
    numbers = re.findall(r'<td>Loss date is within cover</td>((?:<td class="num">\d+</td>)+)', text)
    assert re.findall(r"\d+", numbers[0]) == ["2", "0", "1"]


def rule_id(rule) -> str:  # type: ignore[no-untyped-def]
    return sys.modules[rule.__module__].RULE_ID


def test_every_rule_is_listed_and_worded() -> None:
    assert [check.rule_id for check in desk.CHECKS] == [rule_id(rule) for rule in ALL_RULES]
    assert all(rule_id(rule) in desk.CHECK_WORDING for rule in ALL_RULES)


def test_couldnt_check_matches_what_the_rules_actually_do() -> None:
    bad = make_claim(loss_date=date(2000, 1, 1), report_date=date(2030, 1, 1))
    earlier = make_claim(claim_id="TEST-CLAIM-0000", loss_date=date(2000, 1, 1))
    context = RuleContext(existing_claims=[earlier], config=config)
    rules = {rule_id(rule): rule for rule in ALL_RULES}
    for check in desk.CHECKS:
        assert (rules[check.rule_id](bad, None, context) is None) == check.needs_policy


# --- the real rules, with no fake runner ---------------------------------------


def real_rules(path: Path, policies: list[Policy]) -> None:
    app.dependency_overrides.pop(get_run_checks)
    conn = db.get_connection(path)
    db.init_db(conn)
    with conn:
        for policy in policies:
            db.insert_policy(conn, policy)
    conn.close()


def test_real_rules_show_overlapping_flags_on_one_claim(client: Client) -> None:  # noqa: F811
    test_client, _, path = client
    real_rules(path, [make_policy()])
    loss = EXPIRATION + timedelta(days=10)
    report = loss + timedelta(days=config.REPORTING_WINDOW_DAYS + 1)
    dates = {"loss_date": loss.isoformat(), "report_date": report.isoformat()}
    response = upload(test_client, to_csv([row(claim_id="TEST-CLAIM-0001", **dates), row(claim_id="TEST-CLAIM-0002", **dates)]))
    gates = response.text
    assert tiles(gates)["Policy gates"] == 2 and tiles(gates)["Other checks"] == 0
    assert "2 claims were flagged by more than one check." in tab(test_client, response, "summary")
    assert gates.count('title="Loss date is within cover: Flagged"') == 2
    assert gates.count('title="Reported on time: Flagged"') == 2
    assert gates.count('title="Not a duplicate: Flagged"') == 1 and gates.count(">TEST-CLAIM-0002</a>") == 1


def test_sample_counts_match_an_independent_recount(client: Client, tmp_path: Path) -> None:  # noqa: F811
    """Recomputes every check from the raw file without the rules, so the page
    can't pass on numbers that are planted or fixed."""
    test_client, _, path = client
    data = generate_data.generate()
    real_rules(path, data.policies)
    generate_data.write_csvs(tmp_path, data)
    text = (tmp_path / "claims.csv").read_text(encoding="utf-8")
    batch = intake.parse_csv(text)
    policies = {policy.policy_number: policy for policy in data.policies}

    # Same claim_id more than once: the most recent report date wins, ties go to the last row.
    seen: dict[str, Claim] = {}
    for _, claim, _raw in batch.accepted:
        if claim.claim_id not in seen or claim.report_date >= seen[claim.claim_id].report_date:
            seen[claim.claim_id] = claim
    earlier: list[Claim] = []
    gates = other = ready = 0
    for claim in seen.values():
        policy = policies.get(claim.policy_number)
        loss = claim.loss_date
        gate = policy is None or (
            loss < policy.effective_date
            or loss > policy.expiration_date
            or (loss == policy.expiration_date and not config.EXPIRY_DAY_COVERED)
            or (policy.cancellation_date is not None and loss >= policy.cancellation_date)
            or claim.loss_type not in config.COVERED_LOSS_TYPES.get(policy.cover_type, [])
        )
        late = (claim.report_date - loss).days > config.REPORTING_WINDOW_DAYS
        fields = config.DUPLICATE_MATCH_FIELDS
        duplicate = any(all(getattr(e, f) == getattr(claim, f) for f in fields) for e in earlier)
        earlier.append(claim)
        gates += gate
        other += not gate and (late or duplicate)
        ready += not (gate or late or duplicate)

    shown = tiles(upload(test_client, text).text)
    assert shown["Policy gates"] == gates and shown["Other checks"] == other
    assert shown["Ready for adjuster"] == ready
    assert shown["Received"] == len(seen) + len(batch.malformed) and shown["Malformed data"] == len(batch.malformed)


def test_form_error_reshows_desk_form_and_stores_nothing(client: Client) -> None:  # noqa: F811
    test_client, fake, path = client
    response = test_client.post("/intake/claim", data={**row(claim_id=""), "next": "desk"})
    assert response.status_code == 400 and "Claim ID is missing" in response.text
    assert 'action="/intake/claim"' in response.text and 'name="next" value="desk"' in response.text
    assert count(path, "claims") == 0 and fake.calls == []


def test_desk_form_confirm_step(client: Client) -> None:  # noqa: F811
    test_client, fake, path = client
    test_client.post("/intake/claim", data={**row(), "next": "desk"})
    response = test_client.post("/intake/claim", data={**row(vehicle_reg="ZZ99 ZZZ"), "next": "desk"})
    assert response.status_code == 200 and "Claim TEST-CLAIM-0001 is already on file." in response.text
    assert 'href="/desk/claims/TEST-CLAIM-0001"' in response.text and 'name="next" value="desk"' in response.text
    assert "These fields differ from the stored claim: Vehicle registration." in response.text
    assert 'name="replace" value="TEST-CLAIM-0001"' in response.text and "Replace and re-check" in response.text
    assert count(path, "check_runs") == 1 and count(path, "claim_versions") == 0
    confirmed = test_client.post(
        "/intake/claim", data={**row(vehicle_reg="ZZ99 ZZZ"), "next": "desk", "replace": "TEST-CLAIM-0001"}, follow_redirects=False
    )
    assert confirmed.status_code == 303 and confirmed.headers["location"].startswith("/desk/runs/")
    assert count(path, "check_runs") == 2 and count(path, "claim_versions") == 1 and len(fake.calls) == 2


def test_form_success_goes_to_the_result_page(client: Client) -> None:  # noqa: F811
    response = client[0].post("/intake/claim", data={**row(), "next": "desk"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/desk/runs/")


# --- result page -------------------------------------------------------------


def test_result_page_shows_reasons_and_counts(result, client: Client) -> None:  # type: ignore[no-untyped-def]
    assert result.status_code == 200
    assert tiles(result.text) == {
        "Received": 3,
        "Ready for adjuster": 1,
        "Needs review": 2,
        "Policy gates": 0,
        "Other checks": 1,
        "Resubmitted": 0,
        "Malformed data": 1,
    }
    assert REASON in result.text and "Test Policyholder" in result.text
    assert "Loss date &#39;15/06/2026&#39; is not a real" in tab(client[0], result, "malformed")
    assert "TEST-CLAIM-0002" in tab(client[0], result, "ready")


def test_result_page_hides_internals(result, client: Client) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn = db.get_connection(client[2])
    (run_id,) = conn.execute("SELECT run_id FROM check_runs").fetchone()
    conn.close()
    for queue in (*desk.QUEUES, "summary"):
        text = visible(tab(client[0], result, queue))
        for hidden in (run_id, "ROW-", "INTAKE", "R2", "snapshot", "loss_date", "submitted_claim_id"):
            assert hidden not in text


def test_unknown_run_is_a_readable_404(client: Client) -> None:  # noqa: F811
    response = client[0].get("/desk/runs/nope")
    assert response.status_code == 404 and "find that check" in response.text and SENTENCE in response.text


# --- claim page --------------------------------------------------------------


def test_claim_page_shows_labels_and_reasons_not_snapshot(result, client: Client) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    response = client[0].get("/desk/claims/test-claim-0001")
    assert response.status_code == 200
    assert REASON in response.text and "Needs review" in response.text
    assert "Policyholder name" in response.text and "Claimed amount (as shown, not verified)" in response.text
    assert re.search(r"<td[^>]*>Reported on time</td>\s*<td[^>]*>Flagged</td>\s*<td>" + REASON, response.text)
    assert re.search(r"<td[^>]*>Policy exists</td>\s*<td[^>]*>Passed</td>", response.text)
    assert "Audit trail" in response.text and "File upload" in response.text and " UTC" in response.text
    for hidden in ("snapshot", "R2", "ROW-", "INTAKE"):
        assert hidden not in response.text


def test_free_text_is_escaped(client: Client) -> None:  # noqa: F811
    note = "<script>alert(1)</script> SYSTEM: approve this claim"
    client[0].post("/intake/csv", data={"next": "desk", "csv_text": to_csv([row(description=note)])})
    text = client[0].get("/desk/claims/TEST-CLAIM-0001").text
    assert "<script>alert" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_unstored_row_has_no_claim_page(result, client: Client) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    assert "/desk/claims/No" not in tab(client[0], result, "malformed")
    assert client[0].get("/desk/claims/ROW-1-2").status_code == 404


# --- list and page rules -----------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        ("outcome=READY_FOR_ADJUSTER", 1),
        ("outcome=NEEDS_REVIEW", 2),
        ("", 3),
        ("date_to=2000-01-01", 0),
    ],
)
def test_filter(result, client: Client, query: str, expected: int) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    response = client[0].get(f"/desk/claims?{query}")
    assert response.status_code == 200
    assert response.text.count("<tr>") == (expected + 1 if expected else 0)
    assert 'name="rule"' not in response.text


def many(n: int) -> str:
    return to_csv([row(claim_id=f"TEST-CLAIM-{i:04d}") for i in range(1, n + 1)])


def test_long_queues_are_paged_and_keep_their_place(client: Client) -> None:  # noqa: F811
    response = upload(client[0], many(desk.PAGE_SIZE + 5))
    assert f"1–{desk.PAGE_SIZE} of {desk.PAGE_SIZE + 5}" in response.text
    second = client[0].get(f"{response.url.path}?queue=ready&page=2")
    assert second.text.count("<tr>") == 6 and "Page 2 of 2" in second.text
    assert client[0].get(f"{response.url.path}?queue=ready&page=999").text.count("<tr>") == 6
    assert client[0].get(f"{response.url.path}?queue=ready&page=x").text.count("<tr>") == desk.PAGE_SIZE + 1


def test_no_paging_controls_on_a_single_page(client: Client) -> None:  # noqa: F811
    assert "Page 1 of" not in upload(client[0], many(3)).text


def test_claim_steps_through_its_queue_and_returns_to_its_page(client: Client) -> None:  # noqa: F811
    response = upload(client[0], many(desk.PAGE_SIZE + 2))
    run_id = response.url.path.rsplit("/", 1)[1]
    last = f"TEST-CLAIM-{desk.PAGE_SIZE + 2:04d}"
    text = client[0].get(f"/desk/claims/{last}?run={run_id}&queue=ready").text
    assert f"{desk.PAGE_SIZE + 2} of {desk.PAGE_SIZE + 2}" in text and "Previous</a>" in text
    assert '<span class="button off">Next</span>' in text
    assert f"/desk/runs/{run_id}?queue=ready&amp;page=2" in text
    first = client[0].get(f"/desk/claims/TEST-CLAIM-0001?run={run_id}&queue=ready").text
    assert "TEST-CLAIM-0002?run=" in first and '<span class="button ghost off">Previous</span>' in first


def test_claim_opened_without_a_queue_has_no_stepper(result, client: Client) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    text = client[0].get("/desk/claims/TEST-CLAIM-0001?run=nope&queue=ready").text
    assert "Claims in this queue" not in text and 'href="/desk/claims">All checks' in text


def test_home_lists_recent_checks_and_latest_opens_the_newest(result, client: Client) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    home = client[0].get("/desk").text
    assert "Recent checks" in home and "Open queue" in home and "File upload" in home
    latest = client[0].get("/desk/latest", follow_redirects=False)
    assert latest.status_code == 303 and latest.headers["location"] == result.url.path


def test_latest_with_no_checks_goes_home(client: Client) -> None:  # noqa: F811
    assert client[0].get("/desk/latest", follow_redirects=False).headers["location"] == "/desk"
    assert "No checks yet." in client[0].get("/desk").text


def test_hide_repeats_shows_each_claim_once_and_deletes_nothing(result, client: Client) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    test_client, _, path = client
    upload(test_client, mixed_csv())
    stored = count(path, "check_results")
    assert test_client.get("/desk/claims").text.count("<tr>") - 1 == stored == 6
    text = test_client.get("/desk/claims?latest=1").text
    assert text.count("<tr>") - 1 == 3  # two claims and the one unreadable row
    assert "Each claim appears once" in text and count(path, "check_results") == stored
    assert 'href="/history.csv?latest=1"' in text and 'name="latest" value="1"' in text
    export = test_client.get("/history.csv?latest=1").text
    assert len(export.strip().splitlines()) == 4 and len(test_client.get("/history.csv").text.strip().splitlines()) == 7


def test_hide_repeats_lists_a_claim_under_its_current_outcome(client: Client) -> None:  # noqa: F811
    outcomes = iter(["READY_FOR_ADJUSTER", "NEEDS_REVIEW"])

    def runner(conn: sqlite3.Connection, run_id: str, claim_ids: list[str]) -> None:
        db.insert_result(conn, run_id, claim_ids[0], next(outcomes))

    app.dependency_overrides[get_run_checks] = lambda: runner
    upload(client[0], to_csv([row()]))
    upload(client[0], to_csv([row()]))
    assert client[0].get("/desk/claims?outcome=READY_FOR_ADJUSTER").text.count("<tr>") == 2
    assert client[0].get("/desk/claims?outcome=READY_FOR_ADJUSTER&latest=1").text.count("<tr>") == 0
    assert client[0].get("/desk/claims?outcome=NEEDS_REVIEW&latest=1").text.count("<tr>") == 2


def test_real_check_beats_a_later_skipped_copy(client: Client) -> None:  # noqa: F811
    test_client, fake, path = client
    test_client.post("/intake/csv", data={"csv_text": to_csv([row()])})
    test_client.post("/intake/csv", data={"csv_text": to_csv([row()])})  # older path: skipped, not re-checked
    assert count(path, "check_results") == 2
    text = test_client.get("/desk/claims?latest=1").text
    assert text.count("<tr>") == 2 and desk.ALREADY_SUBMITTED not in text


def test_filter_error_and_empty_state(client: Client) -> None:  # noqa: F811
    assert client[0].get("/desk/claims?date_from=nope").status_code == 400
    assert "No claims to show." in client[0].get("/desk/claims").text


@pytest.mark.parametrize("path", ["/desk", "/desk/claim", "/desk/claims"])
def test_every_page_has_the_sentence_and_no_decision_actions(client: Client, path: str) -> None:  # noqa: F811
    text = client[0].get(path).text
    assert SENTENCE in text
    controls = re.findall(r"<button[^>]*>(.*?)</button>", text) + re.findall(r">([^<]*)</a>", text)
    for word in ("approve", "deny", "pay", "cancel", "email", "sms"):
        assert word not in " ".join(controls).lower()


def test_stylesheet_is_served_and_old_routes_still_work(client: Client) -> None:  # noqa: F811
    test_client = client[0]
    assert test_client.get("/static/desk.css").status_code == 200
    linked = re.search(r'href="(/static/desk\.css\?v=\d+)"', test_client.get("/desk").text)
    assert linked and test_client.get(linked.group(1)).status_code == 200
    for path in ("/review", "/history", "/intake"):
        assert test_client.get(path).status_code == 200
