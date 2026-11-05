"""Rule thresholds and settings for Claim Intake Check.

Every threshold lives here with a documented default and the client decision
that set it (see docs/decisions.md). Rules must read values from this module
rather than hard-coding them.
"""

# Number of days after the loss date within which a claim must be reported.
REPORTING_WINDOW_DAYS: int = 30  # default — pending client confirmation

# Whether a loss occurring on the policy expiry day itself counts as covered.
EXPIRY_DAY_COVERED: bool = True  # default — pending client confirmation

# Claim fields that must all match for two claims to be treated as duplicates.
DUPLICATE_MATCH_FIELDS: list[str] = [  # default — pending client confirmation
    "policy_number",
    "loss_date",
]

# Which loss types each cover type pays out on. Keys are Policy.cover_type
# values; lists are Claim.loss_type values (see app/models.py).
COVERED_LOSS_TYPES: dict[str, list[str]] = {  # default — pending client confirmation
    "comprehensive": [
        "collision",
        "theft",
        "glass",
        "vandalism",
        "weather",
        "third_party",
    ],
    "tpft": ["third_party", "theft"],
    "tpo": ["third_party"],
}
