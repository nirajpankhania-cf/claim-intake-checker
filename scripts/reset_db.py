"""Developer reset: delete the SQLite database file so ``make seed`` can rebuild it.

    python -m scripts.reset_db [PATH]      (default: CIC_DB_PATH or claim_intake.db)

This is a development convenience for trying intake behaviour from a clean
state. It is not part of the web app and the app has no delete path: history
tables are append-only, so the only way to start over is to remove the file.
Refuses anything that doesn't look like a database file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import db

DB_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
SIDECARS = ("-wal", "-shm", "-journal")


def main(argv: list[str] | None = None) -> Path:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else db.DEFAULT_DB_PATH
    if path.suffix not in DB_SUFFIXES:
        raise SystemExit(
            f"{path} does not look like a database file (expected .db, .sqlite or .sqlite3); nothing removed."
        )
    removed = []
    for candidate in (path, *(path.with_name(path.name + suffix) for suffix in SIDECARS)):
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate)
    if removed:
        print("Removed " + ", ".join(str(c) for c in removed))
    else:
        print(f"{path} does not exist; nothing to remove.")
    return path


if __name__ == "__main__":
    main()
