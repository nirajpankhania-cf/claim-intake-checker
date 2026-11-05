"""Read-only queries for the review queue and claim detail pages.

Nothing here writes. The review pages show what was flagged, why and when;
they never approve, deny, pay, cancel or contact anyone. The filter helpers
are shared with the history page (CIC-31).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

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
        return self.created_at.date()


@dataclass
class QueueItem:
    """One claim (or unchecked intake row) with its flags from one run."""

    run_id: str
    run_started_at: datetime
    source: str
    claim_id: str
    outcome: str
    claim: Claim | None
    flags: list[FlagRow] = field(default_factory=list)

    @property
    def flagged_on(self) -> date | None:
        return min((f.flagged_on for f in self.flags), default=None)

    @property
    def rule_ids(self) -> list[str]:
        return list(dict.fromkeys(f.rule_id for f in self.flags))


@dataclass(frozen=True)
class Filters:
    run_id: str | None = None
    rule: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    def matches(self, item: QueueItem) -> bool:
        """True when a single flag meets the rule and date filters together."""
        return any(self._flag_matches(flag) for flag in item.flags)

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


def parse_filters(
    run_id: str | None, rule: str | None, date_from: str | None, date_to: str | None
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

    filters = Filters(
        run_id=(run_id or "").strip() or None,
        rule=(rule or "").strip() or None,
        date_from=a_date("From date", date_from),
        date_to=a_date("To date", date_to),
    )
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise FilterError("From date is after To date.")
    return filters


def _results_with_flags(
    conn: sqlite3.Connection, where: str, params: tuple[str, ...], *, unflagged: bool = False
) -> list[QueueItem]:
    rows = conn.execute(
        "SELECT r.run_id, r.claim_id, r.outcome, ru.started_at, ru.source,"
        " f.id AS flag_id, f.rule_id, f.reason, f.inputs_json, f.created_at"
        " FROM check_results r"
        " JOIN check_runs ru ON ru.run_id = r.run_id"
        f" {'LEFT JOIN' if unflagged else 'JOIN'} flags f ON f.run_id = r.run_id AND f.claim_id = r.claim_id"
        f" WHERE {where}"
        " ORDER BY ru.started_at DESC, r.run_id, r.id, f.id",
        params,
    ).fetchall()
    claims = get_claims(conn, {row["claim_id"] for row in rows})
    items: dict[tuple[str, str], QueueItem] = {}
    seen_flags: set[int] = set()
    for row in rows:
        key = (row["run_id"], row["claim_id"])
        if key not in items:
            items[key] = QueueItem(
                run_id=row["run_id"],
                run_started_at=datetime.fromisoformat(row["started_at"]),
                source=row["source"],
                claim_id=row["claim_id"],
                outcome=row["outcome"],
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
