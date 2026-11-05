"""R1b — cover_type_mismatch."""

import pytest

from app import config
from app.models import COVER_TYPES, LOSS_TYPES
from app.rules.cover_type_mismatch import RULE_ID, cover_type_mismatch
from tests.rules.helpers import make_claim, make_ctx, make_policy


def test_no_flag_when_comprehensive_covers_collision() -> None:
    claim = make_claim(loss_type="collision")
    policy = make_policy(cover_type="comprehensive")
    assert cover_type_mismatch(claim, policy, make_ctx()) is None


def test_flags_glass_on_third_party_only_cover() -> None:
    claim = make_claim(loss_type="glass")
    policy = make_policy(cover_type="tpo")

    flag = cover_type_mismatch(claim, policy, make_ctx())

    assert flag is not None
    assert flag.rule_id == RULE_ID
    assert "glass" in flag.reason
    assert "tpo" in flag.reason
    assert "third_party" in flag.reason
    assert flag.inputs == {
        "loss_type": "glass",
        "cover_type": "tpo",
        "covered_loss_types": ["third_party"],
    }


def test_no_flag_when_tpft_covers_theft() -> None:
    claim = make_claim(loss_type="theft")
    policy = make_policy(cover_type="tpft")
    assert cover_type_mismatch(claim, policy, make_ctx()) is None


def test_flags_collision_on_tpft_cover() -> None:
    claim = make_claim(loss_type="collision")
    policy = make_policy(cover_type="tpft")

    flag = cover_type_mismatch(claim, policy, make_ctx())

    assert flag is not None
    assert "collision" in flag.reason
    assert "tpft" in flag.reason


@pytest.mark.parametrize("cover_type", COVER_TYPES)
@pytest.mark.parametrize("loss_type", LOSS_TYPES)
def test_every_combination_follows_config(cover_type: str, loss_type: str) -> None:
    claim = make_claim(loss_type=loss_type)
    policy = make_policy(cover_type=cover_type)

    flag = cover_type_mismatch(claim, policy, make_ctx())

    expected_covered = loss_type in config.COVERED_LOSS_TYPES[cover_type]
    assert (flag is None) == expected_covered


def test_config_override_drives_result() -> None:
    claim = make_claim(loss_type="glass")
    policy = make_policy(cover_type="tpo")
    ctx = make_ctx(COVERED_LOSS_TYPES={"tpo": ["glass"]})

    assert cover_type_mismatch(claim, policy, ctx) is None


def test_unknown_cover_type_covers_nothing() -> None:
    claim = make_claim(loss_type="collision")
    policy = make_policy(cover_type="comprehensive")
    ctx = make_ctx(COVERED_LOSS_TYPES={})

    flag = cover_type_mismatch(claim, policy, ctx)

    assert flag is not None
    assert "no loss types" in flag.reason
    assert flag.inputs["covered_loss_types"] == []


def test_no_flag_when_policy_is_missing() -> None:
    """R0 owns the missing-policy case; R1b stays silent."""
    assert cover_type_mismatch(make_claim(), None, make_ctx()) is None
