"""Intake rules package.

One module per rule. Each rule is a pure function with the signature
``(claim, policy, context) -> Flag | None`` and no database access; the
runner passes in everything a rule needs.
"""
