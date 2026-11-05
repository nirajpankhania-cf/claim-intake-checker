"""R2 — late_report."""

from datetime import date, timedelta

from app.rules.late_report import RULE_ID, late_report
from tests.rules.helpers import make_claim, make_ctx, make_policy

LOSS = date(2026, 6, 1)


def _claim_reported_after(days: int):
    return make_claim(loss_date=LOSS, report_date=LOSS + timedelta(days=days))


def test_no_flag_when_reported_well_inside_window() -> None:
    assert late_report(_claim_reported_after(5), make_policy(), make_ctx()) is None


def test_flags_when_reported_well_outside_window() -> None:
    claim = _claim_reported_after(90)

    flag = late_report(claim, make_policy(), make_ctx())

    assert flag is not None
    assert flag.rule_id == RULE_ID
    assert LOSS.isoformat() in flag.reason
    assert claim.report_date.isoformat() in flag.reason
    assert "90 days" in flag.reason
    assert "30-day" in flag.reason
    assert flag.inputs == {
        "loss_date": LOSS.isoformat(),
        "report_date": claim.report_date.isoformat(),
        "days_to_report": 90,
        "reporting_window_days": 30,
    }


def test_no_flag_at_exactly_thirty_days() -> None:
    ctx = make_ctx(REPORTING_WINDOW_DAYS=30)
    assert late_report(_claim_reported_after(30), make_policy(), ctx) is None


def test_flags_at_thirty_one_days() -> None:
    ctx = make_ctx(REPORTING_WINDOW_DAYS=30)

    flag = late_report(_claim_reported_after(31), make_policy(), ctx)

    assert flag is not None
    assert "31 days" in flag.reason


def test_no_flag_when_reported_same_day() -> None:
    assert late_report(_claim_reported_after(0), make_policy(), make_ctx()) is None


def test_window_comes_from_config() -> None:
    ctx = make_ctx(REPORTING_WINDOW_DAYS=10)

    assert late_report(_claim_reported_after(10), make_policy(), ctx) is None
    flag = late_report(_claim_reported_after(11), make_policy(), ctx)
    assert flag is not None
    assert "10-day" in flag.reason


def test_runs_without_a_policy() -> None:
    """Lateness depends only on the claim, so a missing policy changes nothing."""
    flag = late_report(_claim_reported_after(31), None, make_ctx())
    assert flag is not None
