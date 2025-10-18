#!/usr/bin/env python3
"""
Cleanup utility for loadtest users.

Searches for and deletes all users with 'loadtest' prefix in their username.
Useful for cleaning up after failed benchmark runs.

Usage:
    uv run python -m tests.load.cleanup_loadtest_users
    uv run python -m tests.load.cleanup_loadtest_users --prefix mytest
    uv run python -m tests.load.cleanup_loadtest_users --dry-run
"""

import sys

import anyio
import click

from nextcloud_mcp_server.client import NextcloudClient


async def cleanup_users(prefix: str = "loadtest", dry_run: bool = False):
    """
    Search for and delete users with the specified prefix.

    Args:
        prefix: Username prefix to search for
        dry_run: If True, only list users without deleting them
    """
    print(f"Searching for users with prefix '{prefix}'...")

    try:
        client = NextcloudClient.from_env()
        users = await client.users.search_users(search=prefix)

        if not users:
            print(f"✓ No users found with prefix '{prefix}'")
            return

        print(f"Found {len(users)} user(s): {', '.join(users)}\n")

        if dry_run:
            print("DRY RUN - No users will be deleted")
            for user in users:
                print(f"  Would delete: {user}")
            print("\nTo actually delete these users, run without --dry-run flag")
            return

        # Delete users
        deleted = []
        failed = []

        for user in users:
            try:
                print(f"  Deleting {user}...")
                await client.users.delete_user(userid=user)
                deleted.append(user)
                print(f"  ✓ Deleted {user}")
            except Exception as e:
                failed.append((user, str(e)))
                print(f"  ✗ Failed to delete {user}: {e}")

        # Summary
        print(f"\n{'=' * 60}")
        print("Cleanup Summary")
        print(f"{'=' * 60}")
        print(f"Successfully deleted: {len(deleted)}")
        print(f"Failed to delete: {len(failed)}")

        if failed:
            print("\nFailed deletions:")
            for user, error in failed:
                print(f"  - {user}: {error}")
            sys.exit(1)
        else:
            print("\n✓ All users cleaned up successfully")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


@click.command()
@click.option(
    "--prefix",
    default="loadtest",
    show_default=True,
    help="Username prefix to search for",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List users without deleting them",
)
def main(prefix: str, dry_run: bool):
    """
    Cleanup loadtest users from Nextcloud.

    Searches for all users with the specified prefix and deletes them.
    Useful for cleaning up after failed benchmark runs.

    Examples:

        # Dry run to see what would be deleted
        uv run python -m tests.load.cleanup_loadtest_users --dry-run

        # Delete all loadtest users
        uv run python -m tests.load.cleanup_loadtest_users

        # Delete users with custom prefix
        uv run python -m tests.load.cleanup_loadtest_users --prefix mytest
    """
    anyio.run(cleanup_users, prefix, dry_run)


if __name__ == "__main__":
    main()
