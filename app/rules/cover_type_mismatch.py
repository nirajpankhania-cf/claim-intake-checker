"""R1b — the loss type is not covered by the policy's cover type."""

from __future__ import annotations

from app.models import Claim, Flag, Policy, RuleContext

RULE_ID = "R1b"


def cover_type_mismatch(
    claim: Claim, policy: Policy | None, ctx: RuleContext
) -> Flag | None:
    """Flag when ``COVERED_LOSS_TYPES`` for the cover type excludes the loss type.

    Returns ``None`` when there is no policy; R0 reports that case. A cover
    type missing from the config is treated as covering nothing.
    """
    if policy is None:
        return None

    covered: list[str] = list(
        ctx.config.COVERED_LOSS_TYPES.get(policy.cover_type, [])
    )
    if claim.loss_type in covered:
        return None

    covered_text = ", ".join(covered) if covered else "no loss types"
    return Flag(
        rule_id=RULE_ID,
        reason=(
            f"Loss type {claim.loss_type} is not covered by the policy's "
            f"{policy.cover_type} cover, which covers {covered_text}."
        ),
        inputs={
            "loss_type": claim.loss_type,
            "cover_type": policy.cover_type,
            "covered_loss_types": covered,
        },
    )
