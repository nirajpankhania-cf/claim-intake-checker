"""R3 — duplicate."""

from datetime import date, timedelta

from app.rules.duplicate import RULE_ID, duplicate
from tests.rules.helpers import POLICY_NUMBER, make_claim, make_ctx, make_policy

LOSS = date(2026, 6, 1)


def test_flags_matching_claim_with_different_id() -> None:
    earlier = make_claim(claim_id="TEST-CLAIM-0001", loss_date=LOSS)
    incoming = make_claim(claim_id="TEST-CLAIM-0002", loss_date=LOSS)

    flag = duplicate(incoming, make_policy(), make_ctx(existing_claims=[earlier]))

    assert flag is not None
    assert flag.rule_id == RULE_ID
    assert "TEST-CLAIM-0001" in flag.reason
    assert POLICY_NUMBER in flag.reason
    assert LOSS.isoformat() in flag.reason
    assert flag.inputs == {
        "match_fields": ["policy_number", "loss_date"],
        "matched_values": {
            "policy_number": POLICY_NUMBER,
            "loss_date": LOSS.isoformat(),
        },
        "earlier_claim_id": "TEST-CLAIM-0001",
        "matching_claim_ids": ["TEST-CLAIM-0001"],
    }


def test_no_flag_when_only_match_is_itself() -> None:
    """The claim being checked may already be in the batch; it is not its own duplicate."""
    claim = make_claim(claim_id="TEST-CLAIM-0001", loss_date=LOSS)
    assert duplicate(claim, make_policy(), make_ctx(existing_claims=[claim])) is None


def test_no_flag_when_loss_date_differs() -> None:
    earlier = make_claim(claim_id="TEST-CLAIM-0001", loss_date=LOSS)
    incoming = make_claim(
        claim_id="TEST-CLAIM-0002", loss_date=LOSS + timedelta(days=1)
    )
    assert duplicate(incoming, make_policy(), make_ctx(existing_claims=[earlier])) is None


def test_no_flag_when_policy_number_differs() -> None:
    earlier = make_claim(
        claim_id="TEST-CLAIM-0001", policy_number="TEST-POLICY-0002", loss_date=LOSS
    )
    incoming = make_claim(claim_id="TEST-CLAIM-0002", loss_date=LOSS)
    assert duplicate(incoming, make_policy(), make_ctx(existing_claims=[earlier])) is None


def test_no_flag_with_no_existing_claims() -> None:
    assert duplicate(make_claim(), make_policy(), make_ctx(existing_claims=[])) is None


def test_reason_names_first_match_and_inputs_list_all() -> None:
    first = make_claim(claim_id="TEST-CLAIM-0001", loss_date=LOSS)
    second = make_claim(claim_id="TEST-CLAIM-0002", loss_date=LOSS)
    incoming = make_claim(claim_id="TEST-CLAIM-0003", loss_date=LOSS)

    flag = duplicate(
        incoming, make_policy(), make_ctx(existing_claims=[first, second])
    )

    assert flag is not None
    assert "TEST-CLAIM-0001" in flag.reason
    assert flag.inputs["matching_claim_ids"] == ["TEST-CLAIM-0001", "TEST-CLAIM-0002"]


def test_match_fields_come_from_config() -> None:
    earlier = make_claim(
        claim_id="TEST-CLAIM-0001", vehicle_reg="TEST999", loss_date=LOSS
    )
    incoming = make_claim(
        claim_id="TEST-CLAIM-0002",
        vehicle_reg="TEST999",
        loss_date=LOSS + timedelta(days=30),
    )
    ctx = make_ctx(existing_claims=[earlier], DUPLICATE_MATCH_FIELDS=["vehicle_reg"])

    flag = duplicate(incoming, make_policy(), ctx)

    assert flag is not None
    assert "vehicle_reg TEST999" in flag.reason
    assert flag.inputs["match_fields"] == ["vehicle_reg"]


def test_runs_without_a_policy() -> None:
    earlier = make_claim(claim_id="TEST-CLAIM-0001", loss_date=LOSS)
    incoming = make_claim(claim_id="TEST-CLAIM-0002", loss_date=LOSS)
    assert duplicate(incoming, None, make_ctx(existing_claims=[earlier])) is not None
