"""Add usage_events table for per-tenant usage metering.

Deck #67 / control-plane usage-metering.md (pull model). Each tenant Pod
records billable usage (embedding queries, pages/chunks embedded) into its own
app DB; the control plane later pulls this table read-only into the billing
ledger and syncs to Stripe Meter Events. Writes are gated by
``USAGE_METERING_ENABLED`` (default off) — the recording hook is a no-op when
the flag is off, so an OSS self-hoster gets an empty table and zero write
overhead.

Unlike the rest of this schema (unix-epoch ``BigInteger`` timestamps, JSON as
``Text``), this table uses real Postgres ``TIMESTAMPTZ``/``JSONB``/``UUID``
because the control-plane rollup runs ``date_trunc('day', occurred_at AT TIME
ZONE 'UTC')`` and ``GROUP BY day, metric`` directly against Postgres, which
requires a genuine timestamptz column. SQLite (OSS/tests) uses portable
fallbacks (``TEXT``/``TIMESTAMP``); the control plane never queries SQLite.

Revision ID: 007
Revises: 006
Create Date: 2026-06-07 12:00:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"

    # Retention: this table has no TTL by design — the control-plane rollup
    # owns the lifecycle (it pulls rows read-only into usage_daily, then
    # prunes once a day is reconciled). The data plane only appends.
    op.create_table(
        "usage_events",
        # Pod-generated idempotency key. UUID on Postgres; TEXT on SQLite,
        # which has no native UUID type. Stored/bound as a plain string in
        # both backends (see usage/store.py).
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=False) if is_pg else sa.Text(),
            primary_key=True,
        ),
        # Operation completion time (UTC). Real TIMESTAMPTZ on Postgres so the
        # CP rollup's date_trunc(... AT TIME ZONE 'UTC') works; portable
        # TIMESTAMP on SQLite (stored as ISO text, queryable in tests).
        sa.Column(
            "occurred_at",
            postgresql.TIMESTAMP(timezone=True) if is_pg else sa.TIMESTAMP(),
            nullable=False,
        ),
        # Catalog metric: 'tokens_embedded' or 'pages_embedded'. Deliberately
        # an unconstrained Text (no CHECK/enum) — the metric catalog lives in
        # control-plane config, not the app-DB schema. If a third metric is
        # ever added, the CP-side catalog must learn it too, or its rollup will
        # silently ignore the new rows; keep the two in sync.
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        # Rawest unit per request (provider, model, tokens, doc_type, ...).
        # JSONB on Postgres so the CP can slice on dimensions later; TEXT
        # (json.dumps) on SQLite.
        sa.Column(
            "metadata",
            postgresql.JSONB() if is_pg else sa.Text(),
            nullable=True,
        ),
    )
    # Serves the CP rollup's per-day range scan (occurred_at >= / <) plus the
    # GROUP BY metric; leading occurred_at makes the range filter index-usable.
    op.create_index(
        "idx_usage_events_occurred_metric",
        "usage_events",
        ["occurred_at", "metric"],
    )


def downgrade() -> None:
    op.drop_index("idx_usage_events_occurred_metric", table_name="usage_events")
    op.drop_table("usage_events")
