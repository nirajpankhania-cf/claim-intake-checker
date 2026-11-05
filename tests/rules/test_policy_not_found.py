"""R0 — policy_not_found."""

from app.rules.policy_not_found import RULE_ID, policy_not_found
from tests.rules.helpers import make_claim, make_ctx, make_policy


def test_flags_when_policy_is_missing() -> None:
    claim = make_claim(policy_number="TEST-POLICY-MISSING")

    flag = policy_not_found(claim, None, make_ctx())

    assert flag is not None
    assert flag.rule_id == RULE_ID
    assert "TEST-POLICY-MISSING" in flag.reason
    assert flag.inputs == {"policy_number": "TEST-POLICY-MISSING"}


def test_no_flag_when_policy_is_present() -> None:
    assert policy_not_found(make_claim(), make_policy(), make_ctx()) is None
