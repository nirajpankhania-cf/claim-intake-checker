"""Fixture builders for rule tests.

Rules are pure, so tests build claims, policies and a context in memory with
these helpers; no database is involved. Every value is an obviously
synthetic placeholder, per the "synthetic data only" rule.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app import config
from app.models import Claim, Policy, RuleContext

POLICY_NUMBER = "TEST-POLICY-0001"
POLICYHOLDER = "Test Policyholder"
VEHICLE_REG = "TEST001"

EFFECTIVE = date(2026, 1, 1)
EXPIRATION = date(2026, 12, 31)
MID_TERM = date(2026, 6, 15)

CONFIG_KEYS = (
    "REPORTING_WINDOW_DAYS",
    "EXPIRY_DAY_COVERED",
    "DUPLICATE_MATCH_FIELDS",
    "COVERED_LOSS_TYPES",
)


def make_policy(**overrides: Any) -> Policy:
    values: dict[str, Any] = {
        "policy_number": POLICY_NUMBER,
        "policyholder_name": POLICYHOLDER,
        "vehicle_reg": VEHICLE_REG,
        "cover_type": "comprehensive",
        "effective_date": EFFECTIVE,
        "expiration_date": EXPIRATION,
        "cancellation_date": None,
        "policy_limit": Decimal("10000.00"),
    }
    values.update(overrides)
    return Policy(**values)


def make_claim(**overrides: Any) -> Claim:
    values: dict[str, Any] = {
        "claim_id": "TEST-CLAIM-0001",
        "policy_number": POLICY_NUMBER,
        "policyholder_name": POLICYHOLDER,
        "claimant_name": None,
        "vehicle_reg": VEHICLE_REG,
        "loss_date": MID_TERM,
        "report_date": MID_TERM,
        "loss_type": "collision",
        "loss_location": None,
        "description": None,
        "claimed_amount": Decimal("500.00"),
        "third_party_involved": False,
        "police_reference": None,
        "attachments": [],
        "submitted_via": "form",
    }
    values.update(overrides)
    return Claim(**values)


def make_config(**overrides: Any) -> SimpleNamespace:
    """A stand-in for ``app.config`` with selected values overridden."""
    values = {key: getattr(config, key) for key in CONFIG_KEYS}
    values.update(overrides)
    return SimpleNamespace(**values)


def make_ctx(
    existing_claims: list[Claim] | None = None, **config_overrides: Any
) -> RuleContext:
    return RuleContext(
        existing_claims=list(existing_claims or []),
        config=make_config(**config_overrides),
    )
