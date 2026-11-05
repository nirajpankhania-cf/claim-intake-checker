"""R1 — the loss date falls outside the policy's period of cover.

A loss is outside cover when it is before the effective date, after the
expiration date (whether the expiration day itself counts is set by
``EXPIRY_DAY_COVERED``), or on or after the cancellation date.
"""

from __future__ import annotations

from app.models import Claim, Flag, Policy, RuleContext

RULE_ID = "R1"


def coverage_gap(
    claim: Claim, policy: Policy | None, ctx: RuleContext
) -> Flag | None:
    """Flag when the loss date is not within the policy's cover period.

    Returns ``None`` when there is no policy; R0 reports that case.
    """
    if policy is None:
        return None

    loss = claim.loss_date
    expiry_day_covered: bool = ctx.config.EXPIRY_DAY_COVERED
    inputs = {
        "loss_date": loss.isoformat(),
        "effective_date": policy.effective_date.isoformat(),
        "expiration_date": policy.expiration_date.isoformat(),
        "cancellation_date": (
            policy.cancellation_date.isoformat()
            if policy.cancellation_date is not None
            else None
        ),
        "expiry_day_covered": expiry_day_covered,
    }

    if loss < policy.effective_date:
        return Flag(
            rule_id=RULE_ID,
            reason=(
                f"Loss on {loss.isoformat()} is before the policy's effective "
                f"date of {policy.effective_date.isoformat()}."
            ),
            inputs=inputs,
        )

    if policy.cancellation_date is not None and loss >= policy.cancellation_date:
        return Flag(
            rule_id=RULE_ID,
            reason=(
                f"Loss on {loss.isoformat()} is on or after the policy's "
                f"cancellation date of {policy.cancellation_date.isoformat()}."
            ),
            inputs=inputs,
        )

    if loss > policy.expiration_date:
        return Flag(
            rule_id=RULE_ID,
            reason=(
                f"Loss on {loss.isoformat()} is after the policy's expiration "
                f"date of {policy.expiration_date.isoformat()}."
            ),
            inputs=inputs,
        )

    if loss == policy.expiration_date and not expiry_day_covered:
        return Flag(
            rule_id=RULE_ID,
            reason=(
                f"Loss on {loss.isoformat()} is on the policy's expiration "
                f"date of {policy.expiration_date.isoformat()}, which is not "
                f"a covered day."
            ),
            inputs=inputs,
        )

    return None
