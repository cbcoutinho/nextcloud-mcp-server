"""Which databases get procrastinate's Alembic revisions (Deck #1494)."""

import pytest

from nextcloud_mcp_server import migrations

pytestmark = pytest.mark.unit

POSTGRES = "postgresql+psycopg://u:p@db/mcp"


def test_postgres_gets_procrastinate_revisions():
    versions = migrations._procrastinate_versions(POSTGRES)
    assert versions is not None
    assert versions.endswith("versions")


def test_sqlite_never_gets_them():
    assert migrations._procrastinate_versions("sqlite+aiosqlite:///x.db") is None


def test_postgres_without_procrastinate_installed(mocker):
    """Postgres can back token storage alone, without the queue's extras."""
    mocker.patch.object(
        migrations.resources, "files", side_effect=ModuleNotFoundError("procrastinate")
    )
    assert migrations._procrastinate_versions(POSTGRES) is None
