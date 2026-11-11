"""View helpers for the manager's review desk (the /desk pages).

These only reshape what intake and review already produce, into ordinary
words. Nothing here parses a CSV, runs a check or writes to the database.
Rule ids, run ids, ROW- keys and inputs snapshots stop here: templates only
receive the fields below.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from app import config, db, review
from app.intake import INTAKE_RULE_ID, IntakeFileError, changed_fields
from app.models import Claim, ClaimVersion
from app.rules import ALL_RULES
from app.rules.policy_not_found import RULE_ID as NO_POLICY_RULE

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "data" / "claims.csv"
SAMPLE_MISSING = "The sample file isn't there yet. Run make seed, then try again."
LEGACY_RESUBMISSION = "Submitted again under the earlier rules; it was not checked then."

READY, LOOK, UNREADABLE = "ready", "look", "unreadable"
PASSED, FLAGGED, NOT_RUN = "passed", "flagged", "not_run"
STATE_LABELS = {PASSED: "Passed", FLAGGED: "Flagged", NOT_RUN: "Not checked"}
# How a claim in a run relates to what was stored before it.
NEW, UPDATED, RECHECKED = "new", "updated", "rechecked"
CHANGE_LABELS = {UPDATED: "Updated", RECHECKED: "Re-checked, no change"}
QUEUES = {
    "gates": "Policy gates",
    "other": "Other checks",
    "malformed": "Malformed data",
    "ready": "Ready for adjuster",
}
# Fields whose old and new values may be shown when a claim is updated. The
# rest are free text or identifiers typed by a person: only "changed" is said.
SHOWN_DIFF_FIELDS = frozenset(
    {"policy_number", "loss_date", "report_date", "loss_type", "claimed_amount", "third_party_involved", "submitted_via"}
)
PAGE_SIZE = 50  # how many rows one page of a table shows; display only


@dataclass(frozen=True)
class Check:
    rule_id: str
    name: str
    short: str
    policy_gate: bool
    needs_policy: bool = False  # can't run when no policy was found


# Plain wording only. Which checks exist, and their order, comes from ALL_RULES,
# so a new rule shows up here without any change to this file.
CHECK_WORDING = {
    check.rule_id: check
    for check in (
        Check("R0", "Policy exists", "Policy", policy_gate=True),
        Check("R1", "Loss date is within cover", "Dates", policy_gate=True, needs_policy=True),
        Check("R1b", "Cover type includes this loss", "Cover", policy_gate=True, needs_policy=True),
        Check("R2", "Reported on time", "On time", policy_gate=False),
        Check("R3", "Not a duplicate", "Duplicate", policy_gate=False),
    )
}


def _worded(rule_id: str) -> Check:
    return CHECK_WORDING.get(rule_id, Check(rule_id, f"Check {rule_id}", rule_id, policy_gate=False))


CHECKS = tuple(_worded(sys.modules[rule.__module__].RULE_ID) for rule in ALL_RULES)


@dataclass(frozen=True)
class CheckLine:
    name: str
    state: str
    policy_gate: bool
    reason: str = ""

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]


@dataclass(frozen=True)
class DeskRow:
    claim_id: str
    has_page: bool
    policy_number: str
    policyholder: str
    status: str
    outcome: str
    reasons: list[str]
    checked_on: date
    checks: list[CheckLine]
    policy_gate: bool
    loss_date: str = "—"
    report_date: str = "—"
    change: str = NEW

    @property
    def change_label(self) -> str:
        return CHANGE_LABELS.get(self.change, "")

    @property
    def flag_count(self) -> int:
        return sum(line.state == FLAGGED for line in self.checks)


def check_lines(flags: dict[str, str]) -> list[CheckLine]:
    """What each rule did for one claim, given its stored flags (rule id to reason).

    A rule records nothing when it passes, so no flag means passed, unless it
    needed a policy and none exists.
    """
    no_policy = NO_POLICY_RULE in flags
    lines = []
    for check in CHECKS:
        state = FLAGGED if check.rule_id in flags else NOT_RUN if no_policy and check.needs_policy else PASSED
        lines.append(CheckLine(check.name, state, check.policy_gate, flags.get(check.rule_id, "")))
    known = {check.rule_id for check in CHECKS}
    for rule_id, reason in flags.items():  # a flag from a rule that has since been removed
        if rule_id not in known:
            lines.append(CheckLine(_worded(rule_id).name, FLAGGED, False, reason))
    return lines


def _intake_flag(item: review.QueueItem) -> review.FlagRow | None:
    return next((flag for flag in item.flags if flag.rule_id == INTAKE_RULE_ID), None)


def plain_problem(problem: str, labels: dict[str, str]) -> str:
    """One field problem, with the column name swapped for its form label."""
    if problem.startswith("row has "):
        return "The row has the wrong number of values"
    for name, label in labels.items():
        if problem.startswith(f"{name} "):
            return label + problem[len(name):]
    return problem


FILE_ERRORS = {
    "header": "This file doesn't have the columns a claims file needs. Check it against the sample file.",
    "too_large": "This file is too large to check. Split it into smaller files and try again.",
    "encoding": "This file couldn't be read as text. Save it as CSV UTF-8 and try again.",
}


def plain_file_error(error: IntakeFileError) -> str:
    """A whole-file error, without column lists or configured limits."""
    return FILE_ERRORS.get(error.kind, str(error))


def _unreadable_reason(flag: review.FlagRow, labels: dict[str, str]) -> str:
    if flag.inputs.get("kind") == "resubmission":  # recorded before 2026-11-09, when S8 rows were skipped
        return LEGACY_RESUBMISSION
    problems = "; ".join(plain_problem(p, labels) for p in flag.inputs.get("problems", []))
    line = flag.inputs.get("line_no")
    where = f"Line {line} of the file" if line else "This row"
    return f"{where} could not be read, so it was not checked" + (f": {problems}." if problems else ".")


def to_rows(
    conn: sqlite3.Connection,
    items: list[review.QueueItem],
    labels: dict[str, str],
    changes: dict[str, str] | None = None,
) -> list[DeskRow]:
    """Turn stored results into display rows, one per result.

    ``changes`` (from ``change_markers``) says which claims were updated or
    re-checked in the run being shown; without it every row reads as new.
    """
    marks = changes or {}
    wanted = {i.claim_id for i in items} | {
        flag.inputs.get("submitted_claim_id") or "" for i in items if (flag := _intake_flag(i))
    }
    claims = review.get_claims(conn, wanted)
    rows = []
    for item in items:
        flag = _intake_flag(item)
        outcome = review.OUTCOMES[item.outcome]
        if flag is None:
            claim = claims.get(item.claim_id)
            checks = check_lines({f.rule_id: f.reason for f in item.flags})
            rows.append(
                DeskRow(
                    claim_id=item.claim_id,
                    has_page=claim is not None,
                    policy_number=claim.policy_number if claim else "—",
                    policyholder=claim.policyholder_name if claim else "—",
                    status=READY if item.outcome == "READY_FOR_ADJUSTER" else LOOK,
                    outcome=outcome,
                    reasons=[f.reason for f in item.flags],
                    checked_on=item.checked_on,
                    checks=checks,
                    policy_gate=any(c.policy_gate and c.state == FLAGGED for c in checks),
                    loss_date=claim.loss_date.isoformat() if claim else "—",
                    report_date=claim.report_date.isoformat() if claim else "—",
                    change=marks.get(item.claim_id, NEW),
                )
            )
            continue
        # A row that never reached the rules: show what was submitted, keyed
        # by the claim number on the row, never the internal ROW- key.
        submitted = flag.inputs.get("submitted_claim_id") or ""
        raw = flag.inputs.get("row") or {}
        rows.append(
            DeskRow(
                claim_id=submitted or "No claim number",
                has_page=bool(submitted) and submitted in claims,
                policy_number=raw.get("policy_number") or "—",
                policyholder=raw.get("policyholder_name") or "—",
                status=UNREADABLE,
                outcome=outcome,
                reasons=[_unreadable_reason(flag, labels)],
                checked_on=item.checked_on,
                checks=[],
                policy_gate=False,
                loss_date=raw.get("loss_date") or "—",
                report_date=raw.get("report_date") or "—",
            )
        )
    return rows


def _claim_key(item: review.QueueItem) -> tuple[str, str]:
    """What makes two results the same claim. Rows that were never stored have
    a different internal key on every upload, so they match on what was submitted."""
    flag = _intake_flag(item)
    if flag is None:
        return ("claim", item.claim_id)
    submitted = flag.inputs.get("submitted_claim_id") or ""
    return ("row", submitted or json.dumps(flag.inputs.get("row") or item.claim_id, sort_keys=True))


def latest_per_claim(items: list[review.QueueItem]) -> list[review.QueueItem]:
    """Each claim once, from results ordered newest first. Only affects the
    view: every result stays stored and on the claim's audit trail."""
    chosen: dict[tuple[str, str], review.QueueItem] = {}
    for item in items:
        chosen.setdefault(_claim_key(item), item)
    return list(chosen.values())


def one_per_claim(conn: sqlite3.Connection, filters: review.Filters) -> list[review.QueueItem]:
    """History with repeats hidden. The outcome filter applies after choosing
    the latest result, so a claim is listed under the outcome it has now."""
    items = latest_per_claim(review.history(conn, replace(filters, outcome=None)))
    return [item for item in items if filters.outcome in (None, item.outcome)]


def split_queues(rows: list[DeskRow]) -> dict[str, list[DeskRow]]:
    """Each row in exactly one work queue."""
    return {
        "gates": [r for r in rows if r.status == LOOK and r.policy_gate],
        "other": [r for r in rows if r.status == LOOK and not r.policy_gate],
        "malformed": [r for r in rows if r.status == UNREADABLE],
        "ready": [r for r in rows if r.status == READY],
    }


def first_queue(queues: dict[str, list[DeskRow]]) -> str:
    """The queue to open first: the first one with anything in it."""
    return next((name for name, rows in queues.items() if rows), "gates")


def paginate(rows: list[DeskRow], page: str | None) -> dict[str, object]:
    """One page of a table. A missing, bad or too-large page number is clamped."""
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    number = int(page) if page and page.isascii() and page.isdecimal() and len(page) < 9 else 1
    number = min(max(number, 1), pages)
    start = (number - 1) * PAGE_SIZE
    shown = rows[start : start + PAGE_SIZE]
    return {"rows": shown, "page": number, "pages": pages, "first": start + 1, "last": start + len(shown), "total": len(rows)}


def neighbours(rows: list[DeskRow], claim_id: str) -> dict[str, object] | None:
    """Where a claim sits in its queue, for Previous and Next."""
    ids = [row.claim_id for row in rows if row.has_page]
    if claim_id not in ids:
        return None
    at = ids.index(claim_id)
    return {
        "position": at + 1,
        "total": len(ids),
        "previous": ids[at - 1] if at else None,
        "next": ids[at + 1] if at + 1 < len(ids) else None,
        "page": rows.index(next(r for r in rows if r.claim_id == claim_id)) // PAGE_SIZE + 1,
    }


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, object]]:
    """The latest checks with their outcome counts, newest first."""
    found = conn.execute(
        "SELECT ru.run_id, ru.started_at, ru.source, COUNT(r.id) AS received,"
        " COALESCE(SUM(r.outcome = 'READY_FOR_ADJUSTER'), 0) AS ready,"
        " COALESCE(SUM(r.outcome = 'NEEDS_REVIEW'), 0) AS review"
        " FROM check_runs ru LEFT JOIN check_results r ON r.run_id = ru.run_id"
        " GROUP BY ru.run_id ORDER BY ru.started_at DESC, ru.rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "when": _utc_text(datetime.fromisoformat(row["started_at"])),
            "source": SOURCES.get(row["source"], row["source"]),
            "received": row["received"],
            "ready": row["ready"],
            "review": row["review"],
        }
        for row in found
    ]


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def counts(rows: list[DeskRow]) -> dict[str, int]:
    """The result-page counts: the two outcomes, why claims need review, and
    how many claims in the run had been seen before."""
    n = Counter(row.status for row in rows)
    gates = sum(row.status == LOOK and row.policy_gate for row in rows)
    return {
        "Received": len(rows),
        "Ready for adjuster": n[READY],
        "Needs review": n[LOOK] + n[UNREADABLE],
        "Policy gates": gates,
        "Other checks": n[LOOK] - gates,
        "Malformed data": n[UNREADABLE],
        "Updated": sum(row.change == UPDATED for row in rows),
        "Re-checked unchanged": sum(row.change == RECHECKED for row in rows),
    }


def nothing_changed(rows: list[DeskRow]) -> bool:
    """True when every checked claim in the run had been checked before, unchanged."""
    checked = [row for row in rows if row.status != UNREADABLE]
    return bool(checked) and all(row.change == RECHECKED for row in checked)


def check_summary(rows: list[DeskRow]) -> list[dict[str, object]]:
    """For each check, how many claims passed, were flagged or couldn't be checked."""
    tally: Counter[tuple[str, str]] = Counter((c.name, c.state) for row in rows for c in row.checks)
    return [
        {
            "name": check.name,
            "policy_gate": check.policy_gate,
            "passed": tally[(check.name, PASSED)],
            "flagged": tally[(check.name, FLAGGED)],
            "not_run": tally[(check.name, NOT_RUN)],
        }
        for check in CHECKS
    ]


def change_markers(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    """For each claim in the run that was seen before: ``UPDATED`` if this run
    replaced it (a version was saved), else ``RECHECKED`` if it had an earlier
    rule check. A claim first seen as a malformed row counts as new."""
    marks: dict[str, str] = {}
    rechecked = conn.execute(
        "SELECT r.claim_id FROM check_results r WHERE r.run_id = ?"
        " AND r.claim_id IN (SELECT claim_id FROM claims)"
        " AND EXISTS (SELECT 1 FROM check_results o WHERE o.claim_id = r.claim_id AND o.id < r.id"
        "   AND NOT EXISTS (SELECT 1 FROM flags f WHERE f.run_id = o.run_id"
        "                   AND f.claim_id = o.claim_id AND f.rule_id = ?))",
        (run_id, INTAKE_RULE_ID),
    )
    marks.update({row[0]: RECHECKED for row in rechecked})
    updated = conn.execute("SELECT claim_id FROM claim_versions WHERE run_id = ?", (run_id,))
    marks.update({row[0]: UPDATED for row in updated})
    return marks


def in_file_repeats(conn: sqlite3.Connection, run_id: str) -> int:
    """Rows in the upload that repeated a claim_id and were dropped.

    Every kept row leaves exactly one result (a checked claim or a malformed
    row), and ``check_runs.claim_count`` is every row in the file, so the
    difference is the dropped repeats. No extra column needed.
    """
    row = conn.execute(
        "SELECT ru.claim_count - (SELECT COUNT(*) FROM check_results r WHERE r.run_id = ru.run_id)"
        " FROM check_runs ru WHERE ru.run_id = ?",
        (run_id,),
    ).fetchone()
    return max(0, row[0]) if row is not None else 0


def _shown(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(value) or "—"
    return "—" if value is None or value == "" else str(value)


def claim_fields(claim: Claim, labels: dict[str, str]) -> list[tuple[str, str]]:
    """Every submitted field as (label, value), in form order."""
    fields = []
    for name, label in labels.items():
        if name == "claimed_amount":
            label = f"{label} (as shown, not verified)"
        value = _shown(getattr(claim, name))
        fields.append((label, value.replace("_", " ") if name == "loss_type" else value))
    return fields


def claim_result(
    history: list[review.QueueItem], claim_id: str, versions: list[ClaimVersion]
) -> dict[str, object]:
    """The claim's latest check, how often it was checked and how often updated."""
    checks = [i for i in history if i.claim_id == claim_id and _intake_flag(i) is None]
    latest = checks[0] if checks else None
    return {
        "outcome": review.OUTCOMES[latest.outcome] if latest else None,
        "checked_on": latest.checked_on if latest else None,
        "reasons": [f.reason for f in latest.flags] if latest else [],
        "checks": check_lines({f.rule_id: f.reason for f in latest.flags}) if latest else [],
        "times_checked": len(checks),
        "updated": len(versions),
    }


def _label(name: str, labels: dict[str, str]) -> str:
    return labels.get(name, name).split(" (")[0]


def describe_changes(old: Claim, new: Claim, labels: dict[str, str]) -> list[str]:
    """One line per changed field. Values are shown only for the fields in
    ``SHOWN_DIFF_FIELDS``; anything a person typed freely just says "changed"."""
    lines = []
    for name in changed_fields(old, new):
        label = _label(name, labels)
        if name in SHOWN_DIFF_FIELDS:
            lines.append(f"{label}: {_shown(getattr(old, name))} → {_shown(getattr(new, name))}")
        else:
            lines.append(f"{label} changed")
    return lines


def version_changes(
    versions: list[ClaimVersion], current: Claim, labels: dict[str, str]
) -> dict[str, list[str]]:
    """What each replacing run changed, keyed by run_id. Each superseded
    version is compared with the one that followed it, the last with the
    stored claim."""
    out = {}
    for n, version in enumerate(versions):
        after = versions[n + 1].claim if n + 1 < len(versions) else current
        out[version.run_id] = describe_changes(version.claim, after, labels)
    return out


def _dates_text(dates: list[str]) -> str:
    if len(dates) == 1:
        return dates[0]
    return ", ".join(dates[:-1]) + " and " + dates[-1]


SOURCES = {"csv": "File upload", "form": "Single claim"}


def audit_trail(
    history: list[review.QueueItem],
    versions: list[ClaimVersion],
    current: Claim,
    labels: dict[str, str],
) -> list[dict[str, object]]:
    """Every check recorded for a claim, newest first, so its handling can be
    reconstructed. Repeat checks that said the same thing are shown as one
    entry with the later dates; a run that updated the claim starts a new
    entry and lists what changed. Nothing stored is altered."""
    changes = version_changes(versions, current, labels)
    entries = []
    for group in review.group_repeats(history, set(changes)):
        item = group.first
        flag = _intake_flag(item)
        if flag is None:
            recorded = [f.reason for f in item.flags] or ["All checks passed."]
        else:
            recorded = [_unreadable_reason(flag, labels)]
        dates = list(dict.fromkeys(again.checked_on.isoformat() for again in group.again))
        entries.append(
            {
                "when": _utc_text(item.checked_at),
                "source": SOURCES.get(item.source, item.source),
                "outcome": review.OUTCOMES[item.outcome],
                "recorded": recorded,
                "updated": changes.get(item.run_id, []),
                "again": f"Checked again on {_dates_text(dates)} with the same result." if dates else "",
            }
        )
    return entries


# Plain names for the cover types in the policy records.
COVER_NAMES = {"comprehensive": "Comprehensive", "tpft": "Third party, fire and theft", "tpo": "Third party only"}


def _latest_outcomes(conn: sqlite3.Connection, claim_ids: list[str]) -> dict[str, str]:
    if not claim_ids:
        return {}
    marks = ",".join("?" * len(claim_ids))
    rows = conn.execute(
        f"SELECT claim_id, outcome FROM check_results WHERE id IN"
        f" (SELECT MAX(id) FROM check_results WHERE claim_id IN ({marks}) GROUP BY claim_id)",
        claim_ids,
    )
    return {row[0]: review.OUTCOMES[row[1]] for row in rows}


def policy_panel(conn: sqlite3.Connection, claim: Claim) -> dict[str, object]:
    """The stored policy a claim was checked against, and its other claims.

    Read-only, and it judges nothing: the rules decide what is flagged. What a
    cover type covers and whether the expiry day counts come from app.config,
    the same settings the rules use.
    """
    policy = db.get_policy(conn, claim.policy_number)
    others = [c for c in db.get_claims_on_policy(conn, claim.policy_number) if c.claim_id != claim.claim_id]
    outcomes = _latest_outcomes(conn, [c.claim_id for c in others])
    fields: list[tuple[str, str]] = []
    if policy is not None:
        covered = config.COVERED_LOSS_TYPES.get(policy.cover_type, [])
        expiry = "expiry day covered" if config.EXPIRY_DAY_COVERED else "expiry day not covered"
        fields = [
            ("Policyholder", policy.policyholder_name),
            ("Vehicle", policy.vehicle_reg),
            ("Cover", COVER_NAMES.get(policy.cover_type, policy.cover_type)),
            ("Covers", ", ".join(t.replace("_", " ") for t in covered) or "No loss types"),
            ("In force", f"{policy.effective_date.isoformat()} to {policy.expiration_date.isoformat()} ({expiry})"),
            ("Cancelled", policy.cancellation_date.isoformat() if policy.cancellation_date else "No"),
            ("Policy limit", f"{policy.policy_limit:,.2f}"),
        ]
    return {
        "number": claim.policy_number,
        "found": policy is not None,
        "fields": fields,
        "others": [
            {
                "claim_id": c.claim_id,
                "loss_date": c.loss_date.isoformat(),
                "loss_type": c.loss_type.replace("_", " "),
                "outcome": outcomes.get(c.claim_id, "Not checked yet"),
            }
            for c in others
        ],
    }
