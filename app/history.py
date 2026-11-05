"""CSV export of the check history (CIC-31).

One row per claim per run, matching the history page. Several flag reasons
for one claim are joined with `` | ``.

Reasons and ids can carry submitted text. A spreadsheet treats a cell that
starts with ``=``, ``+``, ``-`` or ``@`` as a formula, and some importers
trim leading whitespace first. Such cells, and cells starting with a tab,
carriage return or newline, are prefixed with ``'``. The text is otherwise
unchanged. The file starts with a UTF-8 BOM so Excel reads it as UTF-8.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from app.review import QueueItem

COLUMNS: tuple[str, ...] = (
    "run_id",
    "run_started_at",
    "source",
    "claim_id",
    "policy_number",
    "outcome",
    "checked_at",
    "rule_ids",
    "reasons",
)
REASON_SEPARATOR = " | "
_FORMULA_START = ("=", "+", "-", "@")
_CONTROL_START = ("\t", "\r", "\n")
BOM = "\ufeff"


def safe_cell(value: object) -> str:
    """The value as text, with a ``'`` in front if a spreadsheet would run it."""
    text = "" if value is None else str(value)
    if text.startswith(_CONTROL_START) or text.lstrip().startswith(_FORMULA_START):
        return "'" + text
    return text


def to_row(item: QueueItem) -> list[str]:
    return [
        safe_cell(value)
        for value in (
            item.run_id,
            item.run_started_at.isoformat(),
            item.source,
            item.claim_id,
            item.claim.policy_number if item.claim else "",
            item.outcome,
            item.checked_at.isoformat(),
            ";".join(item.rule_ids),
            REASON_SEPARATOR.join(flag.reason for flag in item.flags),
        )
    ]


def to_csv(items: Iterable[QueueItem]) -> str:
    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(to_row(item) for item in items)
    return buffer.getvalue()
