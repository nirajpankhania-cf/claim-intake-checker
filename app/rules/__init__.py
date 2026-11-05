"""Intake rules package.

One module per rule. Each rule is a pure function with the signature
``(claim, policy, ctx) -> Flag | None`` and no database or I/O access; the
runner passes in everything a rule needs via ``RuleContext``.

``ALL_RULES`` is the ordered list the runner iterates. Order is the order
flags are reported in, so the most fundamental check (no policy at all)
comes first.
"""

from __future__ import annotations

from collections.abc import Callable

from app.models import Claim, Flag, Policy, RuleContext
from app.rules.cover_type_mismatch import cover_type_mismatch
from app.rules.coverage_gap import coverage_gap
from app.rules.duplicate import duplicate
from app.rules.late_report import late_report
from app.rules.policy_not_found import policy_not_found

Rule = Callable[[Claim, Policy | None, RuleContext], Flag | None]

ALL_RULES: list[Rule] = [
    policy_not_found,  # R0
    coverage_gap,  # R1
    cover_type_mismatch,  # R1b
    late_report,  # R2
    duplicate,  # R3
]

__all__ = [
    "ALL_RULES",
    "Rule",
    "cover_type_mismatch",
    "coverage_gap",
    "duplicate",
    "late_report",
    "policy_not_found",
]
