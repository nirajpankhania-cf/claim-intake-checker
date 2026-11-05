"""Construct each shared model once to pin the contract.

Values are obviously synthetic placeholders, per the "synthetic data only"
rule; real-looking policy or claim data must never appear here.
"""

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from app import config
from app.models import Claim, CheckResult, Flag, Policy, RuleContext


def test_models_construct() -> None:
    policy = Policy(
        policy_number="TEST-POLICY-0001",
        policyholder_name="Test Policyholder",
        vehicle_reg="TEST001",
        cover_type="comprehensive",
        effective_date=date(2026, 1, 1),
        expiration_date=date(2026, 12, 31),
        cancellation_date=None,
        policy_limit=Decimal("10000.00"),
    )

    claim = Claim(
        claim_id="TEST-CLAIM-0001",
        policy_number=policy.policy_number,
        policyholder_name=policy.policyholder_name,
        claimant_name=None,
        vehicle_reg=policy.vehicle_reg,
        loss_date=date(2026, 6, 1),
        report_date=date(2026, 6, 3),
        loss_type="collision",
        loss_location="Test location",
        description="Test description, stored verbatim.",
        claimed_amount=Decimal("1234.56"),
        third_party_involved=False,
        police_reference=None,
        attachments=["test-photo-1.jpg"],
        submitted_via="form",
    )

    flag = Flag(
        rule_id="test_rule",
        reason="Test reason in plain English.",
        inputs={"loss_date": "2026-06-01", "report_date": "2026-06-03"},
    )

    result = CheckResult(
        claim_id=claim.claim_id,
        outcome="NEEDS_REVIEW",
        flags=[flag],
    )

    context = RuleContext(existing_claims=[claim], config=config)

    assert claim.policy_number == policy.policy_number
    assert result.flags[0] is flag
    assert context.config.REPORTING_WINDOW_DAYS == 30
    assert "collision" in context.config.COVERED_LOSS_TYPES["comprehensive"]

    with pytest.raises(FrozenInstanceError):
        claim.claimed_amount = Decimal("0")  # type: ignore[misc]
