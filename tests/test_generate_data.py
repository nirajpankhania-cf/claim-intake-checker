"""Tests for scripts/generate_data.py, run into a temporary DB and CSV dir."""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

import pytest

from app import config, db
from app.models import RuleContext
from app.rules import ALL_RULES
from scripts import generate_data

TAG = re.compile(r"^SCENARIO (S\d+):")
SCENARIOS = [f"S{n}" for n in range(1, 11)]

# The rule each tagged claim must trip (and no other). S4 and S10 have no
# flag under current defaults; S8 originals and S9 rows are intake cases.
EXPECTED_RULE = {"S1": "R0", "S2": "R1", "S3": "R1", "S5": "R1b", "S6": "R2", "S7": "R3"}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("gen")
    generate_data.main(["--db", str(out / "test.db"), "--out", str(out)])
    return out


def _tags(descriptions: list[str]) -> Counter[str]:
    return Counter(m.group(1) for d in descriptions if (m := TAG.match(d or "")))


def test_every_scenario_tagged_at_least_three_times(generated: Path) -> None:
    conn = db.get_connection(generated / "test.db")
    db_tags = _tags([r["description"] for r in conn.execute("SELECT description FROM claims")])
    conn.close()
    with open(generated / "claims.csv", newline="", encoding="utf-8") as fh:
        csv_tags = _tags([row["description"] for row in csv.DictReader(fh)])

    for scenario in SCENARIOS:
        assert csv_tags[scenario] >= 3, scenario
        if scenario != "S9":  # malformed rows cannot be stored as a Claim
            assert db_tags[scenario] >= 3, scenario
    assert (generated / "policies.csv").exists()


def test_db_claims_trip_only_the_rule_their_tag_names(generated: Path) -> None:
    conn = db.get_connection(generated / "test.db")
    claims = [db.row_to_claim(r) for r in conn.execute("SELECT * FROM claims ORDER BY claim_id")]
    policies = {r["policy_number"]: db.row_to_policy(r) for r in conn.execute("SELECT * FROM policies")}
    conn.close()

    for i, claim in enumerate(claims):
        ctx = RuleContext(existing_claims=claims[:i], config=config)
        policy = policies.get(claim.policy_number)
        fired = {f.rule_id for rule in ALL_RULES if (f := rule(claim, policy, ctx))}
        match = TAG.match(claim.description or "")
        expected = EXPECTED_RULE.get(match.group(1)) if match else None
        assert fired == ({expected} if expected else set()), (claim.claim_id, claim.description, fired)
