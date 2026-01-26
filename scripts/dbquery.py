#!/usr/bin/env python3
"""
Database query helper for development.

Wraps `docker compose exec db mariadb` to execute SQL statements against
the Nextcloud MariaDB database.

Usage:
    ./scripts/dbquery.py "SELECT * FROM oc_notes LIMIT 5"
    ./scripts/dbquery.py -u root -p password "SHOW TABLES"
    ./scripts/dbquery.py --json "SELECT * FROM oc_oidc_clients"
"""

import argparse
import subprocess
import sys
from pathlib import Path


def find_compose_dir() -> Path:
    """Find the directory containing docker-compose.yml."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "docker-compose.yml").exists():
            return current
        if (current / "compose.yml").exists():
            return current
        current = current.parent
    # Default to script's parent directory
    return Path(__file__).resolve().parent.parent


def run_query(
    sql: str,
    user: str = "root",
    password: str = "password",
    database: str = "nextcloud",
    vertical: bool = False,
    json_output: bool = False,
) -> tuple[int, str, str]:
    """
    Execute SQL via docker compose exec.

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    compose_dir = find_compose_dir()

    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",  # Disable pseudo-TTY allocation
        "db",
        "mariadb",
        f"-u{user}",
        f"-p{password}",
        database,
        "-e",
        sql,
    ]

    if vertical:
        cmd.insert(-2, "-E")  # Vertical output format

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=compose_dir,
    )

    return result.returncode, result.stdout, result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute SQL queries against the Nextcloud MariaDB database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s "SELECT COUNT(*) FROM oc_notes"
    %(prog)s "SELECT id, name FROM oc_oidc_clients"
    %(prog)s -E "SELECT * FROM oc_users LIMIT 1"
    %(prog)s --user nextcloud --password nextcloud "SHOW TABLES"
        """,
    )
    parser.add_argument("sql", help="SQL statement to execute")
    parser.add_argument(
        "-u", "--user", default="root", help="Database user (default: root)"
    )
    parser.add_argument(
        "-p",
        "--password",
        default="password",
        help="Database password (default: password)",
    )
    parser.add_argument(
        "-d",
        "--database",
        default="nextcloud",
        help="Database name (default: nextcloud)",
    )
    parser.add_argument(
        "-E",
        "--vertical",
        action="store_true",
        help="Print output vertically (one column per line)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Request JSON output (if supported)",
    )

    args = parser.parse_args()

    returncode, stdout, stderr = run_query(
        sql=args.sql,
        user=args.user,
        password=args.password,
        database=args.database,
        vertical=args.vertical,
        json_output=args.json_output,
    )

    if stdout:
        print(stdout, end="")
    if stderr:
        # Filter out the password warning
        filtered_stderr = "\n".join(
            line
            for line in stderr.splitlines()
            if "Using a password on the command line interface can be insecure"
            not in line
        )
        if filtered_stderr:
            print(filtered_stderr, file=sys.stderr)

    return returncode


if __name__ == "__main__":
    sys.exit(main())
