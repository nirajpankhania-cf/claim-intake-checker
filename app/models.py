"""Shared data models for Claim Intake Check.

These frozen dataclasses are the contract between the three layers of the
app: the rules (pure functions that receive a ``Claim``, a ``Policy`` and a
``RuleContext`` and return a ``Flag`` or ``None``), the runner (which calls
the rules and persists ``CheckResult`` rows), and the UI (which renders
them). Nothing else should define its own shape for a claim, policy or flag.

Instances are immutable so a rule cannot accidentally alter the claim it is
inspecting. Dates are ``datetime.date`` objects, never strings. Free-text
fields (``description``, ``loss_location``, names) are opaque data: they are
stored and displayed verbatim and never interpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

LossType = Literal[
    "collision", "theft", "glass", "vandalism", "weather", "third_party"
]
LOSS_TYPES: tuple[str, ...] = (
    "collision",
    "theft",
    "glass",
    "vandalism",
    "weather",
    "third_party",
)

SubmittedVia = Literal["email", "form"]
SUBMITTED_VIA: tuple[str, ...] = ("email", "form")

CoverType = Literal["comprehensive", "tpft", "tpo"]
COVER_TYPES: tuple[str, ...] = ("comprehensive", "tpft", "tpo")

Outcome = Literal["READY_FOR_ADJUSTER", "NEEDS_REVIEW"]


@dataclass(frozen=True)
class Claim:
    """A single incoming motor claim as submitted, before any checks run."""

    claim_id: str
    policy_number: str
    policyholder_name: str
    claimant_name: str | None
    vehicle_reg: str
    loss_date: date
    report_date: date
    loss_type: LossType
    loss_location: str | None
    description: str | None
    claimed_amount: Decimal
    third_party_involved: bool
    police_reference: str | None
    attachments: list[str]
    submitted_via: SubmittedVia


@dataclass(frozen=True)
class Policy:
    """The policy a claim is checked against."""

    policy_number: str
    policyholder_name: str
    vehicle_reg: str
    cover_type: CoverType
    effective_date: date
    expiration_date: date
    cancellation_date: date | None
    policy_limit: Decimal


@dataclass(frozen=True)
class ClaimVersion:
    """A superseded version of a claim, kept when a resubmission changed it.

    ``claim`` is the claim as it was before ``run_id`` replaced it. Identical
    resubmissions write no version, so the trail only grows on a change.
    """

    run_id: str
    replaced_at: datetime
    claim: Claim


@dataclass(frozen=True)
class Flag:
    """One rule's finding on one claim.

    ``inputs`` is the snapshot of the values the rule looked at, kept so the
    report can show *why* the flag was raised. It must be JSON-serialisable.
    """

    rule_id: str
    reason: str
    inputs: dict[str, Any]


@dataclass(frozen=True)
class CheckResult:
    """The outcome of running every rule over one claim.

    This is a routing hint for a human adjuster, not a decision: no outcome
    approves, denies or pays anything.
    """

    claim_id: str
    outcome: Outcome
    flags: list[Flag]


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule may need beyond the claim and policy themselves.

    Rules have no database access; the runner builds this once per batch.
    ``config`` is normally the ``app.config`` module, but tests may pass any
    object exposing the same attribute names.
    """

    existing_claims: list[Claim]
    config: Any
