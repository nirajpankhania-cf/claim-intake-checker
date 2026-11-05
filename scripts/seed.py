"""Create the SQLite database file from ``app/schema.sql``.

Usage: ``python -m scripts.seed [path]`` (default: ``CIC_DB_PATH`` or
``claim_intake.db``). Creates empty tables only; synthetic data comes from
``scripts/generate_data.py``. Running it again on an existing file is safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.db import DEFAULT_DB_PATH, get_connection, init_db


def main(argv: list[str] | None = None) -> Path:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        init_db(conn)
    finally:
        conn.close()
    print(f"Initialised database at {path}")
    return path


if __name__ == "__main__":
    main()
