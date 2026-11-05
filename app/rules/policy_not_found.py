"""R0 — the claim's policy number does not match any policy."""

from __future__ import annotations

from app.models import Claim, Flag, Policy, RuleContext

RULE_ID = "R0"


def policy_not_found(
    claim: Claim, policy: Policy | None, ctx: RuleContext
) -> Flag | None:
    """Flag when no policy was found for the claim's policy number."""
    if policy is not None:
        return None
    return Flag(
        rule_id=RULE_ID,
        reason=(
            f"No policy was found for policy number {claim.policy_number} "
            f"given on the claim."
        ),
        inputs={"policy_number": claim.policy_number},
    )
