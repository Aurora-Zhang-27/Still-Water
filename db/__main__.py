"""Command line:

    python -m db init   create the database, or upgrade it to the latest version
    python -m db info   show each table's row count and its append-only guards
"""

from __future__ import annotations

import sys

from db.database import init_db, list_tables, schema_version
from settings import load_settings


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "info"
    if command not in ("init", "info"):
        print(__doc__)
        return 2

    db_path = load_settings().db_path
    conn = init_db(db_path)
    print(f"Database: {db_path}")
    print(f"Version: {schema_version(conn)}")
    print()

    guards: dict[str, list[str]] = {}
    for row in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'"):
        guards.setdefault(row["tbl_name"], []).append(row["name"])

    print(f"{'Table':<20}{'Rows':>6}  Guards")
    for table in list_tables(conn):
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        protection = ", ".join(sorted(guards.get(table, []))) or "-"
        print(f"{table:<20}{count:>6}  {protection}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
