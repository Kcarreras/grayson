"""Stand-in for the `snow` CLI used by end-to-end tests."""

from __future__ import annotations

import json
import sys


def main() -> int:
    args = sys.argv[1:]
    if args[:2] == ["connection", "list"]:
        print(json.dumps([{"connection_name": "default"}]))
        return 0
    if args and args[0] == "sql":
        sql = args[args.index("-q") + 1]
        upper = sql.upper()
        multi = "ALTER SESSION" in upper
        if "FAIL_AUTH" in upper:
            sys.stderr.write("Authentication token has expired. Error 390114.")
            return 1
        if "FAIL_TIMEOUT" in upper:
            sys.stderr.write("Statement reached its statement or warehouse timeout")
            return 1
        # a timeout ALTER SESSION may be prepended, so match anywhere
        if "DESCRIBE TABLE" in upper:
            rows = [
                {"name": "ID", "type": "NUMBER", "kind": "COLUMN", "null?": "N"},
                {"name": "VAL", "type": "VARCHAR", "kind": "COLUMN", "null?": "Y"},
            ]
        elif "INFORMATION_SCHEMA.TABLES" in upper:
            rows = [
                {
                    "TABLE_CATALOG": "DB",
                    "TABLE_SCHEMA": "S",
                    "TABLE_NAME": "T1",
                    "ROW_COUNT": 100,
                    "LAST_ALTERED": "2026-08-20 00:00:00",
                }
            ]
        else:
            rows = [{"ID": i, "VAL": f"v{i}"} for i in range(1, 6)]
        if multi:
            print(json.dumps([[{"status": "Statement executed successfully."}], rows]))
        else:
            print(json.dumps(rows))
        return 0
    sys.stderr.write(f"fake snow: unsupported args {args}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
