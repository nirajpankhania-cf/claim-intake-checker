"""View helpers for the manager's review desk (the /desk pages).

These only reshape what intake and review already produce, into ordinary
words. Nothing here parses a CSV, runs a check or writes to the database.
Rule ids, run ids, ROW- keys and inputs snapshots stop here: templates only
receive the fields below.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from app import review
from app.intake import INTAKE_RULE_ID, IntakeFileError
from app.models import Claim
from app.rules import ALL_RULES
from app.rules.policy_not_found import RULE_ID as NO_POLICY_RULE

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "data" / "claims.csv"
SAMPLE_MISSING = "The sample file isn't there yet. Run make seed, then try again."
ALREADY_SUBMITTED = "This claim was already submitted, so it was not checked again."

READY, LOOK, ALREADY, UNREADABLE = "ready", "look", "already", "unreadable"
PASSED, FLAGGED, NOT_RUN = "passed", "flagged", "not_run"
STATE_LABELS = {PASSED: "Passed", FLAGGED: "Flagged", NOT_RUN: "Not checked"}
QUEUES = {
    "gates": "Policy gates",
    "other": "Other checks",
    "resubmitted": "Resubmitted",
    "malformed": "Malformed data",
    "ready": "Ready for adjuster",
}
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
    problems = "; ".join(plain_problem(p, labels) for p in flag.inputs.get("problems", []))
    line = flag.inputs.get("line_no")
    where = f"Line {line} of the file" if line else "This row"
    return f"{where} could not be read, so it was not checked" + (f": {problems}." if problems else ".")


def to_rows(
    conn: sqlite3.Connection, items: list[review.QueueItem], labels: dict[str, str]
) -> list[DeskRow]:
    """Turn stored results into display rows, one per result."""
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
                )
            )
            continue
        # A row that never reached the rules: show what was submitted, keyed
        # by the claim number on the row, never the internal ROW- key.
        already = flag.inputs.get("kind") == "resubmission"
        submitted = flag.inputs.get("submitted_claim_id") or ""
        raw = flag.inputs.get("row") or {}
        rows.append(
            DeskRow(
                claim_id=submitted or "No claim number",
                has_page=already and submitted in claims,
                policy_number=raw.get("policy_number") or "—",
                policyholder=raw.get("policyholder_name") or "—",
                status=ALREADY if already else UNREADABLE,
                outcome=outcome,
                reasons=[ALREADY_SUBMITTED if already else _unreadable_reason(flag, labels)],
                checked_on=item.checked_on,
                checks=[],
                policy_gate=False,
                loss_date=raw.get("loss_date") or "—",
                report_date=raw.get("report_date") or "—",
            )
        )
    return rows


def _is_resubmission(item: review.QueueItem) -> bool:
    flag = _intake_flag(item)
    return flag is not None and flag.inputs.get("kind") == "resubmission"


def _claim_key(item: review.QueueItem) -> tuple[str, str]:
    """What makes two results the same claim. Rows that were never stored have
    a different internal key on every upload, so they match on what was submitted."""
    flag = _intake_flag(item)
    if flag is None:
        return ("claim", item.claim_id)
    submitted = flag.inputs.get("submitted_claim_id") or ""
    if _is_resubmission(item):
        return ("claim", submitted)
    return ("row", submitted or json.dumps(flag.inputs.get("row") or item.claim_id, sort_keys=True))


def latest_per_claim(items: list[review.QueueItem]) -> list[review.QueueItem]:
    """Each claim once, from results ordered newest first. A real check always
    wins over a copy that was skipped as a resubmission. Only affects the view:
    every result stays stored and on the claim's audit trail."""
    chosen: dict[tuple[str, str], review.QueueItem] = {}
    for item in items:
        key = _claim_key(item)
        if key not in chosen or (_is_resubmission(chosen[key]) and not _is_resubmission(item)):
            chosen[key] = item
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
        "resubmitted": [r for r in rows if r.status == ALREADY],
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
    """The result-page counts: the two outcomes, then why claims need review."""
    n = Counter(row.status for row in rows)
    gates = sum(row.status == LOOK and row.policy_gate for row in rows)
    return {
        "Received": len(rows),
        "Ready for adjuster": n[READY],
        "Needs review": n[LOOK] + n[ALREADY] + n[UNREADABLE],
        "Policy gates": gates,
        "Other checks": n[LOOK] - gates,
        "Resubmitted": n[ALREADY],
        "Malformed data": n[UNREADABLE],
    }


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


def rechecked_count(conn: sqlite3.Connection, run_id: str) -> int:
    """Claims in this run that already had a check result before it."""
    return conn.execute(
        "SELECT COUNT(*) FROM check_results r WHERE r.run_id = ?"
        " AND r.claim_id IN (SELECT claim_id FROM claims)"
        " AND EXISTS (SELECT 1 FROM check_results o WHERE o.claim_id = r.claim_id AND o.id < r.id)",
        (run_id,),
    ).fetchone()[0]


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


def claim_result(history: list[review.QueueItem], claim_id: str) -> dict[str, object]:
    """The claim's latest check and how many later copies were skipped."""
    checks = [i for i in history if i.claim_id == claim_id and _intake_flag(i) is None]
    latest = checks[0] if checks else None
    return {
        "outcome": review.OUTCOMES[latest.outcome] if latest else None,
        "checked_on": latest.checked_on if latest else None,
        "reasons": [f.reason for f in latest.flags] if latest else [],
        "checks": check_lines({f.rule_id: f.reason for f in latest.flags}) if latest else [],
        "times_checked": len(checks),
        "resubmitted": sum(
            1 for i in history if (f := _intake_flag(i)) and f.inputs.get("kind") == "resubmission"
        ),
    }


SOURCES = {"csv": "File upload", "form": "Single claim"}


def audit_trail(history: list[review.QueueItem], labels: dict[str, str]) -> list[dict[str, object]]:
    """Every check recorded for a claim, newest first, so its handling can be reconstructed."""
    entries = []
    for item in history:
        flag = _intake_flag(item)
        if flag is None:
            recorded = [f.reason for f in item.flags] or ["All checks passed."]
        elif flag.inputs.get("kind") == "resubmission":
            recorded = [ALREADY_SUBMITTED]
        else:
            recorded = [_unreadable_reason(flag, labels)]
        entries.append(
            {
                "when": _utc_text(item.checked_at),
                "source": SOURCES.get(item.source, item.source),
                "outcome": review.OUTCOMES[item.outcome],
                "recorded": recorded,
            }
        )
    return entries
