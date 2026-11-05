"""R2 — the claim was reported more than ``REPORTING_WINDOW_DAYS`` after the loss."""

from __future__ import annotations

from app.models import Claim, Flag, Policy, RuleContext

RULE_ID = "R2"


def late_report(
    claim: Claim, policy: Policy | None, ctx: RuleContext
) -> Flag | None:
    """Flag when report_date minus loss_date exceeds the reporting window.

    A gap of exactly ``REPORTING_WINDOW_DAYS`` is inside the window.
    """
    window_days: int = ctx.config.REPORTING_WINDOW_DAYS
    days_to_report = (claim.report_date - claim.loss_date).days
    if days_to_report <= window_days:
        return None

    return Flag(
        rule_id=RULE_ID,
        reason=(
            f"Claim was reported on {claim.report_date.isoformat()}, "
            f"{days_to_report} days after the loss on "
            f"{claim.loss_date.isoformat()}, which is outside the "
            f"{window_days}-day reporting window."
        ),
        inputs={
            "loss_date": claim.loss_date.isoformat(),
            "report_date": claim.report_date.isoformat(),
            "days_to_report": days_to_report,
            "reporting_window_days": window_days,
        },
    )
