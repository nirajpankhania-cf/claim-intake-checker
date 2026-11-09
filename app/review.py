"""Read-only queries for the review queue and claim detail pages.

Nothing here writes. The review pages show what was flagged, why and when;
they never approve, deny, pay, cancel or contact anyone. The filter helpers
are shared with the history page (CIC-31).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app import db
from app.intake import INTAKE_RULE_ID
from app.models import Claim

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class FlagRow:
    rule_id: str
    reason: str
    inputs: dict
    created_at: datetime

    @property
    def flagged_on(self) -> date:
        return utc_date(self.created_at)


@dataclass
class QueueItem:
    """One claim (or unchecked intake row) with its flags from one run."""

    run_id: str
    run_started_at: datetime
    source: str
    claim_id: str
    outcome: str
    checked_at: datetime
    claim: Claim | None
    flags: list[FlagRow] = field(default_factory=list)

    @property
    def flagged_on(self) -> date | None:
        return min((f.flagged_on for f in self.flags), default=None)

    @property
    def checked_on(self) -> date:
        return utc_date(self.checked_at)

    @property
    def rule_ids(self) -> list[str]:
        return list(dict.fromkeys(f.rule_id for f in self.flags))


def utc_date(moment: datetime) -> date:
    """The UTC calendar date of a stored timestamp."""
    return moment.astimezone(timezone.utc).date() if moment.tzinfo else moment.date()


@dataclass(frozen=True)
class Filters:
    """Parsed filters, shared by the queue and history pages.

    Each page uses one matching rule: the queue uses ``matches`` (rule and
    flag date), history uses ``matches_history`` (outcome and check date).
    Both apply ``run_id`` in SQL.
    """

    run_id: str | None = None
    rule: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    outcome: str | None = None

    def matches(self, item: QueueItem) -> bool:
        """True when a single flag meets the rule and date filters together."""
        return any(self._flag_matches(flag) for flag in item.flags)

    def matches_history(self, item: QueueItem) -> bool:
        """History filters on the check date and outcome, since passed claims have no flags."""
        if self.outcome and item.outcome != self.outcome:
            return False
        if self.date_from and item.checked_on < self.date_from:
            return False
        if self.date_to and item.checked_on > self.date_to:
            return False
        return True

    def _flag_matches(self, flag: FlagRow) -> bool:
        if self.rule and flag.rule_id != self.rule:
            return False
        if self.date_from and flag.flagged_on < self.date_from:
            return False
        if self.date_to and flag.flagged_on > self.date_to:
            return False
        return True


class FilterError(ValueError):
    """A filter value from the query string can't be used."""


OUTCOMES: dict[str, str] = {
    "READY_FOR_ADJUSTER": "Ready for adjuster",
    "NEEDS_REVIEW": "Needs review",
}


def parse_filters(
    run_id: str | None,
    rule: str | None,
    date_from: str | None,
    date_to: str | None,
    outcome: str | None = None,
) -> Filters:
    """Turn raw query-string values into ``Filters``. Blank means no filter."""

    def a_date(name: str, value: str | None) -> date | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            if _ISO_DATE.fullmatch(value):
                return date.fromisoformat(value)
        except ValueError:
            pass
        raise FilterError(f"{name} must be a YYYY-MM-DD date.")

    outcome = (outcome or "").strip() or None
    if outcome is not None and outcome not in OUTCOMES:
        raise FilterError("Outcome must be Ready for adjuster or Needs review.")
    filters = Filters(
        run_id=(run_id or "").strip() or None,
        rule=(rule or "").strip() or None,
        date_from=a_date("From date", date_from),
        date_to=a_date("To date", date_to),
        outcome=outcome,
    )
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise FilterError("From date is after To date.")
    return filters


def _results_with_flags(
    conn: sqlite3.Connection, where: str, params: tuple[str, ...], *, unflagged: bool = False
) -> list[QueueItem]:
    rows = conn.execute(
        "SELECT r.id AS result_id, r.run_id, r.claim_id, r.outcome, r.checked_at,"
        " ru.started_at, ru.source,"
        " f.id AS flag_id, f.rule_id, f.reason, f.inputs_json, f.created_at"
        " FROM check_results r"
        " JOIN check_runs ru ON ru.run_id = r.run_id"
        f" {'LEFT JOIN' if unflagged else 'JOIN'} flags f ON f.run_id = r.run_id AND f.claim_id = r.claim_id"
        f" WHERE {where}"
        " ORDER BY ru.started_at DESC, ru.rowid DESC, r.id, f.id",
        params,
    ).fetchall()
    claims = get_claims(conn, {row["claim_id"] for row in rows})
    items: dict[tuple[str, str], QueueItem] = {}
    latest_result: dict[tuple[str, str], int] = {}
    seen_flags: set[int] = set()
    for row in rows:
        key = (row["run_id"], row["claim_id"])
        if key in items and row["result_id"] > latest_result[key]:
            # The run recorded this claim more than once: the latest result wins.
            items[key].outcome = row["outcome"]
            items[key].checked_at = datetime.fromisoformat(row["checked_at"])
            latest_result[key] = row["result_id"]
        if key not in items:
            latest_result[key] = row["result_id"]
            items[key] = QueueItem(
                run_id=row["run_id"],
                run_started_at=datetime.fromisoformat(row["started_at"]),
                source=row["source"],
                claim_id=row["claim_id"],
                outcome=row["outcome"],
                checked_at=datetime.fromisoformat(row["checked_at"]),
                claim=claims.get(row["claim_id"]),
            )
        # No flag (unflagged=True), or the same flag again because the run
        # holds more than one result row for this claim.
        if row["flag_id"] is None or row["flag_id"] in seen_flags:
            continue
        seen_flags.add(row["flag_id"])
        flag = db.row_to_flag(row)
        items[key].flags.append(
            FlagRow(flag.rule_id, flag.reason, flag.inputs, datetime.fromisoformat(row["created_at"]))
        )
    return list(items.values())


def review_queue(conn: sqlite3.Connection, filters: Filters) -> list[QueueItem]:
    """Every result with at least one flag, newest run first, filtered."""
    where, params = "r.outcome = 'NEEDS_REVIEW'", ()
    if filters.run_id:
        where, params = where + " AND r.run_id = ?", (filters.run_id,)
    return [item for item in _results_with_flags(conn, where, params) if filters.matches(item)]


def history(conn: sqlite3.Connection, filters: Filters) -> list[QueueItem]:
    """Every result, passed or flagged, newest run first, filtered by run,
    check date and outcome."""
    clauses, params = ["1 = 1"], []
    if filters.run_id:
        clauses.append("r.run_id = ?")
        params.append(filters.run_id)
    if filters.outcome:
        # Coarse pre-filter in SQL; matches_history still applies the latest
        # result's outcome when a run recorded a claim twice.
        clauses.append(
            "EXISTS (SELECT 1 FROM check_results o WHERE o.run_id = r.run_id"
            " AND o.claim_id = r.claim_id AND o.outcome = ?)"
        )
        params.append(filters.outcome)
    items = _results_with_flags(conn, " AND ".join(clauses), tuple(params), unflagged=True)
    return [item for item in items if filters.matches_history(item)]


def run_source(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute("SELECT source FROM check_runs WHERE run_id = ?", (run_id,)).fetchone()
    return row["source"] if row is not None else None


def run_results(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Each claim's latest outcome in one run."""
    return conn.execute(
        "SELECT r.claim_id, r.outcome FROM check_results r"
        " WHERE r.run_id = ? AND r.id = (SELECT MAX(id) FROM check_results"
        " WHERE run_id = r.run_id AND claim_id = r.claim_id)"
        " ORDER BY r.id",
        (run_id,),
    ).fetchall()


def list_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every check run, newest first, for the run picker."""
    return conn.execute(
        "SELECT run_id, started_at, source, claim_count FROM check_runs"
        " ORDER BY started_at DESC, run_id"
    ).fetchall()


def claim_history(conn: sqlite3.Connection, claim_id: str) -> list[QueueItem]:
    """Every result recorded for one claim, flagged or not, newest run first.

    Includes intake rows that resubmitted this claim_id; intake keys those
    ``ROW-<run_id>-<line_no>`` (see app/intake.py).
    """
    where = (
        "r.claim_id = ? OR r.claim_id IN (SELECT claim_id FROM flags"
        f" WHERE rule_id = '{INTAKE_RULE_ID}'"
        " AND json_extract(inputs_json, '$.submitted_claim_id') = ?)"
    )
    return _results_with_flags(conn, where, (claim_id, claim_id), unflagged=True)


def get_claim(conn: sqlite3.Connection, claim_id: str) -> Claim | None:
    return get_claims(conn, {claim_id}).get(claim_id)


def get_claims(conn: sqlite3.Connection, claim_ids: set[str]) -> dict[str, Claim]:
    """Stored claims by id, in one query per 500 ids. Unknown ids are absent."""
    ids, found = sorted(claim_ids), {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM claims WHERE claim_id IN ({marks})", chunk):
            found[row["claim_id"]] = db.row_to_claim(row)
    return found


def rule_ids(conn: sqlite3.Connection) -> list[str]:
    """Rule ids that have raised at least one flag, for the filter menu."""
    return [r[0] for r in conn.execute("SELECT DISTINCT rule_id FROM flags ORDER BY rule_id")]
