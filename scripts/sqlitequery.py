#!/usr/bin/env python3
"""
SQLite database query helper for MCP service development.

Wraps `docker compose exec <service> sqlite3` to execute SQL statements
against the token storage database in any MCP service container.

Usage:
    ./scripts/sqlitequery.py ".tables"
    ./scripts/sqlitequery.py -s oauth "SELECT * FROM refresh_tokens"
    ./scripts/sqlitequery.py -s keycloak --headers "SELECT * FROM oauth_clients"
    ./scripts/sqlitequery.py --json "SELECT * FROM audit_logs LIMIT 5"
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Service name aliases for convenience
SERVICE_ALIASES = {
    "mcp": "mcp",
    "oauth": "mcp-oauth",
    "mcp-oauth": "mcp-oauth",
    "keycloak": "mcp-keycloak",
    "mcp-keycloak": "mcp-keycloak",
    "basic": "mcp-multi-user-basic",
    "multi-user-basic": "mcp-multi-user-basic",
    "mcp-multi-user-basic": "mcp-multi-user-basic",
}


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


def resolve_service(service: str) -> str:
    """Resolve service alias to container name."""
    resolved = SERVICE_ALIASES.get(service.lower())
    if resolved is None:
        # Not a known alias, use as-is (might be a custom service)
        return service
    return resolved


def run_query(
    sql: str,
    service: str = "mcp",
    database: str = "/app/data/tokens.db",
    headers: bool = False,
    json_output: bool = False,
    column_mode: bool = False,
) -> tuple[int, str, str]:
    """
    Execute SQL via docker compose exec.

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    compose_dir = find_compose_dir()
    container = resolve_service(service)

    # Build sqlite3 command with options
    sqlite_args = []

    # Set output mode
    if json_output:
        sqlite_args.extend(["-json"])
    elif column_mode:
        sqlite_args.extend(["-column"])

    # Enable headers
    if headers or column_mode:
        sqlite_args.extend(["-header"])

    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",  # Disable pseudo-TTY allocation
        container,
        "sqlite3",
        *sqlite_args,
        database,
        sql,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=compose_dir,
    )

    return result.returncode, result.stdout, result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute SQL queries against SQLite databases in MCP service containers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Services:
    mcp              Single-user BasicAuth mode (default)
    oauth            Nextcloud OAuth mode (mcp-oauth)
    keycloak         Keycloak OAuth mode (mcp-keycloak)
    basic            Multi-user BasicAuth mode (mcp-multi-user-basic)

Examples:
    %(prog)s ".tables"
    %(prog)s -s oauth "SELECT user_id FROM refresh_tokens"
    %(prog)s -s keycloak ".schema oauth_clients"
    %(prog)s --headers "SELECT * FROM audit_logs LIMIT 5"
    %(prog)s --json "SELECT * FROM oauth_sessions"
        """,
    )
    parser.add_argument("sql", help="SQL statement or SQLite command to execute")
    parser.add_argument(
        "-s",
        "--service",
        default="mcp",
        help="Target service (mcp, oauth, keycloak, basic) (default: mcp)",
    )
    parser.add_argument(
        "-d",
        "--database",
        default="/app/data/tokens.db",
        help="Database path inside container (default: /app/data/tokens.db)",
    )
    parser.add_argument(
        "--headers",
        action="store_true",
        help="Show column headers",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output in JSON format",
    )
    parser.add_argument(
        "--column",
        action="store_true",
        dest="column_mode",
        help="Output in column format with headers",
    )

    args = parser.parse_args()

    returncode, stdout, stderr = run_query(
        sql=args.sql,
        service=args.service,
        database=args.database,
        headers=args.headers,
        json_output=args.json_output,
        column_mode=args.column_mode,
    )

    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, file=sys.stderr)

    return returncode


if __name__ == "__main__":
    sys.exit(main())
