"""ALL_RULES is the ordered contract the runner will iterate."""

from app.rules import (
    ALL_RULES,
    cover_type_mismatch,
    coverage_gap,
    duplicate,
    late_report,
    policy_not_found,
)
from tests.rules.helpers import make_claim, make_ctx, make_policy


def test_all_rules_is_ordered_r0_to_r3() -> None:
    assert ALL_RULES == [
        policy_not_found,
        coverage_gap,
        cover_type_mismatch,
        late_report,
        duplicate,
    ]


def test_every_rule_accepts_the_shared_signature() -> None:
    claim, policy, ctx = make_claim(), make_policy(), make_ctx()
    for rule in ALL_RULES:
        assert rule(claim, policy, ctx) is None
        rule(claim, None, ctx)  # must tolerate a missing policy without raising


def test_rule_ids_are_distinct() -> None:
    ids = {rule.__module__.rsplit(".", 1)[-1] for rule in ALL_RULES}
    assert len(ids) == len(ALL_RULES)
