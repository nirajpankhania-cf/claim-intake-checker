"""R3 — another claim already exists with the same identifying fields.

Two claims are duplicates when every field named in
``DUPLICATE_MATCH_FIELDS`` is equal and the claim ids differ.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.models import Claim, Flag, Policy, RuleContext

RULE_ID = "R3"


def _plain(value: Any) -> Any:
    """Render a field value for the reason text and the JSON inputs snapshot."""
    if isinstance(value, date):
        return value.isoformat()
    return value


def duplicate(
    claim: Claim, policy: Policy | None, ctx: RuleContext
) -> Flag | None:
    """Flag when an earlier claim in ``ctx.existing_claims`` matches this one.

    The reason names the first matching claim; every matching claim id is
    recorded in the inputs snapshot.
    """
    match_fields: list[str] = list(ctx.config.DUPLICATE_MATCH_FIELDS)

    matches = [
        other
        for other in ctx.existing_claims
        if other.claim_id != claim.claim_id
        and all(
            getattr(other, field) == getattr(claim, field)
            for field in match_fields
        )
    ]
    if not matches:
        return None

    matched_values = {field: _plain(getattr(claim, field)) for field in match_fields}
    earlier = matches[0]
    fields_text = " and ".join(
        f"{field} {matched_values[field]}" for field in match_fields
    )
    return Flag(
        rule_id=RULE_ID,
        reason=(
            f"Claim matches earlier claim {earlier.claim_id} on {fields_text}."
        ),
        inputs={
            "match_fields": match_fields,
            "matched_values": matched_values,
            "earlier_claim_id": earlier.claim_id,
            "matching_claim_ids": [other.claim_id for other in matches],
        },
    )
