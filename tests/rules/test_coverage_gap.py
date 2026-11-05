"""R1 — coverage_gap."""

from datetime import date, timedelta

import pytest

from app.rules.coverage_gap import RULE_ID, coverage_gap
from tests.rules.helpers import (
    EFFECTIVE,
    EXPIRATION,
    MID_TERM,
    make_claim,
    make_ctx,
    make_policy,
)

ONE_DAY = timedelta(days=1)
CANCELLATION = date(2026, 9, 1)


def test_no_flag_when_loss_is_clearly_inside_cover() -> None:
    assert coverage_gap(make_claim(loss_date=MID_TERM), make_policy(), make_ctx()) is None


def test_flags_loss_clearly_before_effective_date() -> None:
    loss = EFFECTIVE - timedelta(days=90)

    flag = coverage_gap(make_claim(loss_date=loss), make_policy(), make_ctx())

    assert flag is not None
    assert flag.rule_id == RULE_ID
    assert loss.isoformat() in flag.reason
    assert EFFECTIVE.isoformat() in flag.reason
    assert "before" in flag.reason


def test_flags_loss_clearly_after_expiration_date() -> None:
    loss = EXPIRATION + timedelta(days=90)

    flag = coverage_gap(make_claim(loss_date=loss), make_policy(), make_ctx())

    assert flag is not None
    assert loss.isoformat() in flag.reason
    assert EXPIRATION.isoformat() in flag.reason
    assert "after" in flag.reason


def test_no_flag_on_effective_date() -> None:
    assert coverage_gap(make_claim(loss_date=EFFECTIVE), make_policy(), make_ctx()) is None


def test_flags_day_before_effective_date() -> None:
    flag = coverage_gap(
        make_claim(loss_date=EFFECTIVE - ONE_DAY), make_policy(), make_ctx()
    )
    assert flag is not None
    assert "before" in flag.reason


def test_no_flag_on_expiration_date_when_expiry_day_covered() -> None:
    ctx = make_ctx(EXPIRY_DAY_COVERED=True)
    assert coverage_gap(make_claim(loss_date=EXPIRATION), make_policy(), ctx) is None


def test_flags_on_expiration_date_when_expiry_day_not_covered() -> None:
    ctx = make_ctx(EXPIRY_DAY_COVERED=False)

    flag = coverage_gap(make_claim(loss_date=EXPIRATION), make_policy(), ctx)

    assert flag is not None
    assert EXPIRATION.isoformat() in flag.reason
    assert flag.inputs["expiry_day_covered"] is False


@pytest.mark.parametrize("expiry_day_covered", [True, False])
def test_flags_day_after_expiration_regardless_of_setting(
    expiry_day_covered: bool,
) -> None:
    ctx = make_ctx(EXPIRY_DAY_COVERED=expiry_day_covered)
    loss = EXPIRATION + ONE_DAY

    flag = coverage_gap(make_claim(loss_date=loss), make_policy(), ctx)

    assert flag is not None
    assert loss.isoformat() in flag.reason
    assert "after" in flag.reason


@pytest.mark.parametrize("expiry_day_covered", [True, False])
def test_no_flag_day_before_expiration_regardless_of_setting(
    expiry_day_covered: bool,
) -> None:
    ctx = make_ctx(EXPIRY_DAY_COVERED=expiry_day_covered)
    claim = make_claim(loss_date=EXPIRATION - ONE_DAY)
    assert coverage_gap(claim, make_policy(), ctx) is None


def test_flags_loss_on_cancellation_date() -> None:
    policy = make_policy(cancellation_date=CANCELLATION)

    flag = coverage_gap(make_claim(loss_date=CANCELLATION), policy, make_ctx())

    assert flag is not None
    assert CANCELLATION.isoformat() in flag.reason
    assert "cancellation" in flag.reason
    assert flag.inputs["cancellation_date"] == CANCELLATION.isoformat()


def test_flags_loss_after_cancellation_date() -> None:
    policy = make_policy(cancellation_date=CANCELLATION)
    flag = coverage_gap(make_claim(loss_date=CANCELLATION + ONE_DAY), policy, make_ctx())
    assert flag is not None
    assert "cancellation" in flag.reason


def test_no_flag_day_before_cancellation_date() -> None:
    policy = make_policy(cancellation_date=CANCELLATION)
    claim = make_claim(loss_date=CANCELLATION - ONE_DAY)
    assert coverage_gap(claim, policy, make_ctx()) is None


def test_no_flag_without_cancellation_date() -> None:
    policy = make_policy(cancellation_date=None)
    assert coverage_gap(make_claim(loss_date=MID_TERM), policy, make_ctx()) is None


def test_inputs_snapshot_is_json_friendly() -> None:
    policy = make_policy(cancellation_date=CANCELLATION)
    flag = coverage_gap(make_claim(loss_date=CANCELLATION), policy, make_ctx())

    assert flag is not None
    assert flag.inputs == {
        "loss_date": CANCELLATION.isoformat(),
        "effective_date": EFFECTIVE.isoformat(),
        "expiration_date": EXPIRATION.isoformat(),
        "cancellation_date": CANCELLATION.isoformat(),
        "expiry_day_covered": True,
    }


def test_no_flag_when_policy_is_missing() -> None:
    """R0 owns the missing-policy case; R1 stays silent."""
    assert coverage_gap(make_claim(), None, make_ctx()) is None
