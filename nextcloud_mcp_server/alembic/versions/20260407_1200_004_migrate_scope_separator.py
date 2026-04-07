"""Migrate scope separator from colon to dot

Many identity providers reject ':' in OAuth scope names. This migration
updates stored scope strings from the old 'resource:action' format to
'resource.action' (e.g., 'notes:read' -> 'notes.read').

See ADR-023 for rationale.

Revision ID: 004
Revises: 003
Create Date: 2026-04-07 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Replace colon separator with dot in stored scope strings."""

    # Update scopes in app_passwords (JSON array of scope strings)
    # Only ':' characters in a JSON array like '["notes:read","calendar:write"]'
    # are inside scope name strings, so REPLACE is safe here.
    op.execute(
        """
        UPDATE app_passwords
        SET scopes = REPLACE(scopes, ':', '.')
        WHERE scopes IS NOT NULL
        """
    )

    # Update requested_scopes in login_flow_sessions
    op.execute(
        """
        UPDATE login_flow_sessions
        SET requested_scopes = REPLACE(requested_scopes, ':', '.')
        WHERE requested_scopes IS NOT NULL
        """
    )


def downgrade() -> None:
    """Revert dot separator back to colon in stored scope strings."""

    op.execute(
        """
        UPDATE app_passwords
        SET scopes = REPLACE(scopes, '.', ':')
        WHERE scopes IS NOT NULL
        """
    )

    op.execute(
        """
        UPDATE login_flow_sessions
        SET requested_scopes = REPLACE(requested_scopes, '.', ':')
        WHERE requested_scopes IS NOT NULL
        """
    )
