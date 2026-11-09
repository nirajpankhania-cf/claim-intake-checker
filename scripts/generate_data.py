"""Synthetic data generator.

The only source of policies, policyholders and claims in this project. Uses
Faker to produce clearly synthetic records; real-looking data must never be
hard-coded, pasted, or fetched anywhere else, including tests.

Output is deterministic for a given seed. It writes:

* the SQLite DB (via ``app.db`` helpers): every policy and every well-formed
  claim, each claim_id once;
* ``data/policies.csv`` and ``data/claims.csv``: the same records, plus the
  S8 re-submitted rows (same claim_id again, with a changed description, so
  intake overwrites and re-checks the stored claim and keeps the old version)
  and the S9 malformed rows, which cannot be stored as a ``Claim`` and only
  exist to exercise CSV intake.

Deliberate scenario claims carry a tag at the start of ``description``, e.g.
``"SCENARIO S6: late report 41 days."``, and boundary cases carry
``"BOUNDARY: ..."``. The tags are for people running the demo; no rule reads
them. Scenario numbers follow the table in ``docs/decisions.md``.

Usage: ``python -m scripts.generate_data [--db PATH] [--out DIR] [--seed N]``
"""

from __future__ import annotations

import argparse
import csv
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from faker import Faker

from app import config, db
from app.models import COVER_TYPES, LOSS_TYPES, Claim, Policy

SEED = 4242
POLICY_COUNT = 200
CLEAN_CLAIM_COUNT = 440
PER_SCENARIO = 6
CANCELLED_SHARE = 0.10

# The dataset's "today": nothing is reported after this date.
REFERENCE_DATE = date(2026, 10, 31)
EFFECTIVE_FROM = date(2025, 1, 1)
EFFECTIVE_TO = date(2026, 6, 30)
CLEAN_MAX_REPORT_GAP = 21  # well inside any plausible reporting window

COVER_WEIGHTS = (0.6, 0.25, 0.15)  # comprehensive, tpft, tpo
POLICY_LIMITS = (5_000, 10_000, 15_000, 20_000, 30_000, 50_000)
REG_LETTERS = "ABCDEFGHJKLMNOPRSTUVWXY"  # no I, Q, Z
REG_AGE_IDS = ("19", "69", "20", "70", "21", "71", "22", "72", "23", "73", "24", "74", "25", "75")
ATTACHMENTS = ("photo_front.jpg", "photo_rear.jpg", "repair_estimate.pdf", "dashcam.mp4")
DESCRIPTIONS = {
    "collision": ("Low-speed collision at a junction.", "Hit while parked on the street."),
    "theft": ("Vehicle taken from driveway overnight.", "Vehicle stolen from a car park."),
    "glass": ("Windscreen cracked by a stone chip.", "Side window shattered."),
    "vandalism": ("Paintwork scratched along one side.", "Mirrors broken while parked."),
    "weather": ("Hail damage to roof and bonnet.", "Tree branch fell on vehicle in a storm."),
    "third_party": ("Other driver reversed into the vehicle.", "Third party pulled out at a roundabout."),
}

PolicyFilter = Callable[[Policy], bool]
CENT = Decimal("0.01")

CLAIM_COLUMNS = [f.name for f in fields(Claim)]
POLICY_COLUMNS = [f.name for f in fields(Policy)]


@dataclass
class Dataset:
    policies: list[Policy]
    claims: list[Claim]  # well-formed, unique claim_ids; goes to the DB
    csv_claim_rows: list[dict[str, str]]  # claims + S8 resubmissions (changed) + S9 rows


def _add_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year + 1, day=28)


def cover_end(policy: Policy) -> date:
    """Last covered day: the expiry date, or the day before cancellation."""
    if policy.cancellation_date is not None:
        return min(policy.expiration_date, policy.cancellation_date - timedelta(days=1))
    return policy.expiration_date


class _Generator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.fake = Faker("en_GB")
        self.fake.seed_instance(seed)
        self.policies: list[Policy] = []
        self.claims: list[dict[str, Any]] = []
        self.used_loss_keys: set[tuple[str, date]] = set()

    # -- policies ------------------------------------------------------------

    def vehicle_reg(self) -> str:
        pick = lambda n: "".join(self.rng.choice(REG_LETTERS) for _ in range(n))  # noqa: E731
        return f"{pick(2)}{self.rng.choice(REG_AGE_IDS)} {pick(3)}"

    def make_policies(self) -> None:
        span = (EFFECTIVE_TO - EFFECTIVE_FROM).days
        for n in range(1, POLICY_COUNT + 1):
            effective = EFFECTIVE_FROM + timedelta(days=self.rng.randint(0, span))
            expiration = _add_year(effective) - timedelta(days=1)
            cancellation = None
            if self.rng.random() < CANCELLED_SHARE:
                latest = min((REFERENCE_DATE - effective).days - 1, 330)
                cancellation = effective + timedelta(days=self.rng.randint(30, latest))
            self.policies.append(Policy(
                policy_number=f"POL-{n:06d}",
                policyholder_name=self.fake.name(),
                vehicle_reg=self.vehicle_reg(),
                cover_type=self.rng.choices(COVER_TYPES, COVER_WEIGHTS)[0],
                effective_date=effective,
                expiration_date=expiration,
                cancellation_date=cancellation,
                policy_limit=Decimal(self.rng.choice(POLICY_LIMITS)).quantize(CENT),
            ))

    def pick_policy(self, ok: PolicyFilter = lambda p: True) -> Policy:
        candidates = [p for p in self.policies if ok(p)]
        return self.rng.choice(candidates)

    # -- claims --------------------------------------------------------------

    def base_claim(self, policy: Policy, loss: date, gap: int, **over: Any) -> dict[str, Any]:
        """A claim that is clean against ``policy`` unless ``over`` changes it."""
        covered = config.COVERED_LOSS_TYPES.get(policy.cover_type, [])
        loss_type = over.pop("loss_type", None) or self.rng.choice(covered)
        third_party = loss_type == "third_party" or (
            loss_type == "collision" and self.rng.random() < 0.3
        )
        cap = min(int(policy.policy_limit), 6_000)
        claim: dict[str, Any] = {
            "claim_id": "",  # assigned once all claims are ordered
            "policy_number": policy.policy_number,
            "policyholder_name": policy.policyholder_name,
            "claimant_name": self.fake.name() if third_party and self.rng.random() < 0.5 else None,
            "vehicle_reg": policy.vehicle_reg,
            "loss_date": loss,
            "report_date": loss + timedelta(days=gap),
            "loss_type": loss_type,
            "loss_location": (
                None if self.rng.random() < 0.1
                else f"{self.fake.street_name()}, {self.fake.city()}"
            ),
            "description": self.rng.choice(DESCRIPTIONS[loss_type]),
            "claimed_amount": (Decimal(self.rng.randint(15_000, cap * 100)) / 100).quantize(CENT),
            "third_party_involved": third_party,
            "police_reference": (
                f"PR-{self.rng.randint(0, 9_999_999):07d}"
                if loss_type in ("theft", "vandalism") else None
            ),
            "attachments": self.rng.sample(ATTACHMENTS, self.rng.randint(0, 3)),
            "submitted_via": self.rng.choices(("form", "email"), (0.7, 0.3))[0],
        }
        claim.update(over)
        return claim

    def add(self, claim: dict[str, Any], tag: str | None = None) -> dict[str, Any]:
        if tag:
            claim["description"] = f"{tag}. {claim['description']}"
        self.used_loss_keys.add((claim["policy_number"], claim["loss_date"]))
        self.claims.append(claim)
        return claim

    def free_loss_date(self, policy: Policy, start: date, end: date) -> date | None:
        """A date in [start, end] with no other claim on this policy that day."""
        if end < start:
            return None
        for _ in range(20):
            d = start + timedelta(days=self.rng.randint(0, (end - start).days))
            if (policy.policy_number, d) not in self.used_loss_keys:
                return d
        return None

    def clean(
        self,
        gap: int | None = None,
        tag: str | None = None,
        ok: PolicyFilter = lambda p: True,
        **over: Any,
    ) -> dict[str, Any]:
        """Add a claim inside cover, reported within ``gap`` days, no duplicate."""
        while True:
            policy = self.pick_policy(ok)
            gap_days = self.rng.randint(0, CLEAN_MAX_REPORT_GAP) if gap is None else gap
            end = min(cover_end(policy) - timedelta(days=1), REFERENCE_DATE - timedelta(days=gap_days))
            loss = self.free_loss_date(policy, policy.effective_date, end)
            if loss is not None:
                return self.add(self.base_claim(policy, loss, gap_days, **over), tag)

    def on_date(
        self,
        ok: PolicyFilter,
        loss_for: Callable[[Policy, int], date],
        tag_for: Callable[[Policy, date], str],
        count: int = PER_SCENARIO,
    ) -> None:
        """Add claims whose loss date is set relative to the policy's dates.

        ``loss_for`` gets the policy and the index of the claim being made, so
        the first claim of a scenario can sit exactly on the boundary.
        """
        made = 0
        while made < count:
            policy = self.pick_policy(ok)
            loss = loss_for(policy, made)
            gap = self.rng.randint(0, 10)
            if (policy.policy_number, loss) in self.used_loss_keys or loss + timedelta(days=gap) > REFERENCE_DATE:
                continue
            self.add(self.base_claim(policy, loss, gap), tag_for(policy, loss))
            made += 1

    def make_claims(self) -> list[dict[str, str]]:
        """Build every claim. Returns the CSV-only extra rows (S8, S9)."""
        for _ in range(CLEAN_CLAIM_COUNT):
            self.clean()

        # Boundary cases that should come out clean.
        self.on_date(lambda p: True, lambda p, i: p.effective_date,
                     lambda p, d: "BOUNDARY: loss on effective date", count=2)
        for gap in (29, 30) * 2:
            self.clean(gap=gap, tag=f"BOUNDARY: report at {gap} days")

        # S1 policy number not found.
        for n in range(PER_SCENARIO):
            c = self.clean(tag="SCENARIO S1: policy number not found")
            c["policy_number"] = f"POL-9{n:05d}"

        # S2 loss before effective date (first one is the day before).
        self.on_date(lambda p: True,
                     lambda p, i: p.effective_date - timedelta(days=1 if i == 0 else self.rng.randint(2, 60)),
                     lambda p, d: f"SCENARIO S2: loss {(p.effective_date - d).days} days before effective date")

        # S3 loss after expiry (day after) or on / after cancellation.
        expired = lambda p: p.cancellation_date is None and p.expiration_date + timedelta(days=60) < REFERENCE_DATE  # noqa: E731
        cancelled = lambda p: p.cancellation_date is not None and p.cancellation_date + timedelta(days=60) < REFERENCE_DATE  # noqa: E731
        self.on_date(expired, lambda p, i: p.expiration_date + timedelta(days=1 if i < 2 else self.rng.randint(2, 30)),
                     lambda p, d: f"SCENARIO S3: loss {(d - p.expiration_date).days} days after expiry", count=3)
        self.on_date(cancelled, lambda p, i: p.cancellation_date + timedelta(days=0 if i < 2 else self.rng.randint(1, 30)),
                     lambda p, d: "SCENARIO S3: loss on cancellation date" if d == p.cancellation_date
                     else "SCENARIO S3: loss after cancellation", count=3)

        # S4 loss on the expiry date itself.
        self.on_date(expired, lambda p, i: p.expiration_date,
                     lambda p, d: "SCENARIO S4: loss on expiry date")

        # S5 loss type not covered by cover type.
        for cover in ("tpo", "tpft") * (PER_SCENARIO // 2):
            covered = config.COVERED_LOSS_TYPES.get(cover, [])
            loss_type = self.rng.choice([t for t in LOSS_TYPES if t not in covered])
            self.clean(tag=f"SCENARIO S5: {loss_type} not covered by {cover}",
                       ok=lambda p, cover=cover: p.cover_type == cover, loss_type=loss_type)

        # S6 late report (first one is one day over the window).
        window = config.REPORTING_WINDOW_DAYS
        for gap in [window + 1] + [self.rng.randint(window + 5, 120) for _ in range(PER_SCENARIO - 1)]:
            self.clean(gap=gap, tag=f"SCENARIO S6: late report {gap} days")

        # S7 different claim_id, same policy + loss date, reported later.
        for original in self.rng.sample(self.claims[:CLEAN_CLAIM_COUNT], PER_SCENARIO):
            policy = next(p for p in self.policies if p.policy_number == original["policy_number"])
            gap = (original["report_date"] - original["loss_date"]).days + self.rng.randint(1, 5)
            dup = self.base_claim(policy, original["loss_date"], gap)
            dup["_duplicate_of"] = original
            self.add(dup, "SCENARIO S7: duplicate of {original}")

        # S8 same claim_id submitted again (originals here; the CSV also has a
        # resubmission with the same report_date and a changed description).
        s8_originals = [self.clean(tag="SCENARIO S8: original submission") for _ in range(PER_SCENARIO)]

        # S10 claimed amount above policy limit.
        for _ in range(PER_SCENARIO):
            c = self.clean(tag="SCENARIO S10: claimed amount above policy limit")
            limit = next(p.policy_limit for p in self.policies if p.policy_number == c["policy_number"])
            c["claimed_amount"] = limit + (Decimal(self.rng.randint(1, 500_000)) / 100).quantize(CENT)

        self.assign_ids()

        extra = []
        for c in s8_originals:
            row = _csv_row(c)
            row["description"] = f"SCENARIO S8: resubmission of {c['claim_id']}. " + row["description"].split(". ", 1)[1]
            extra.append(row)
        extra.extend(self.malformed_rows(len(self.claims)))
        return extra

    def assign_ids(self) -> None:
        """Order claims by report date and number them in that order."""
        self.claims.sort(key=lambda c: (c["report_date"], c["loss_date"], c["policy_number"]))
        for n, c in enumerate(self.claims, start=1):
            c["claim_id"] = f"CLM-{n:06d}"
        for c in self.claims:
            original = c.pop("_duplicate_of", None)
            if original is not None:
                c["description"] = c["description"].replace("{original}", original["claim_id"])

    def malformed_rows(self, offset: int) -> list[dict[str, str]]:
        """S9: rows with a required field missing or a date that won't parse."""
        problems = [
            ("vehicle_reg", "", "vehicle_reg missing"),
            ("loss_date", "", "loss_date missing"),
            ("loss_date", "2026-02-30", "loss_date not a real date"),
            ("report_date", "15/06/2026", "report_date not ISO format"),
            ("policy_number", "", "policy_number missing"),
            ("report_date", "", "report_date missing"),
        ]
        loss = date(2026, 6, 1)
        rows = []
        for n, (field, value, what) in enumerate(problems, start=offset + 1):
            policy = self.pick_policy(
                lambda p: p.effective_date <= loss < cover_end(p)
                and (p.policy_number, loss) not in self.used_loss_keys
            )
            self.used_loss_keys.add((policy.policy_number, loss))
            row = _csv_row(self.base_claim(policy, loss, 2))
            row["claim_id"] = f"CLM-{n:06d}"
            row[field] = value
            row["description"] = f"SCENARIO S9: {what}. {row['description']}"
            rows.append(row)
        return rows


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return ";".join(value)
    return str(value)


def _csv_row(record: dict[str, Any]) -> dict[str, str]:
    return {k: _csv_value(record[k]) for k in CLAIM_COLUMNS if k in record}


def generate(seed: int = SEED) -> Dataset:
    gen = _Generator(seed)
    gen.make_policies()
    extra_rows = gen.make_claims()
    claims = [Claim(**{k: c[k] for k in CLAIM_COLUMNS}) for c in gen.claims]
    rows = [_csv_row(asdict(c)) for c in claims] + extra_rows
    return Dataset(policies=gen.policies, claims=claims, csv_claim_rows=rows)


def write_db(path: Path, data: Dataset) -> None:
    conn = db.get_connection(path)
    try:
        db.init_db(conn)
        if conn.execute("SELECT EXISTS (SELECT 1 FROM policies UNION ALL SELECT 1 FROM claims)").fetchone()[0]:
            raise SystemExit(f"{path} already has data; delete the file to regenerate.")
        with conn:
            for policy in data.policies:
                db.insert_policy(conn, policy)
            for claim in data.claims:
                db.insert_claim(conn, claim)
    finally:
        conn.close()


def write_csvs(out_dir: Path, data: Dataset) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "policies.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=POLICY_COLUMNS)
        writer.writeheader()
        writer.writerows(
            {k: _csv_value(v) for k, v in asdict(p).items()} for p in data.policies
        )
    with open(out_dir / "claims.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CLAIM_COLUMNS)
        writer.writeheader()
        writer.writerows(data.csv_claim_rows)


def main(argv: list[str] | None = None) -> Dataset:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)

    data = generate(args.seed)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    write_db(args.db, data)
    write_csvs(args.out, data)
    print(
        f"Wrote {len(data.policies)} policies and {len(data.claims)} claims to {args.db}; "
        f"{len(data.csv_claim_rows)} claim rows to {args.out / 'claims.csv'}"
    )
    return data


if __name__ == "__main__":
    main()
