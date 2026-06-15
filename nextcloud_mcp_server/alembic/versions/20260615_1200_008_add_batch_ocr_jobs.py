"""Add batch_ocr_jobs table for async batch OCR job tracking.

Deck #332 / embedding-gateway batch OCR (astrolabe-cloud-website#372). When
``DOCUMENT_OCR_MODE=batch`` the OCR tier submits a document to the gateway's
async ``POST /v1/ocr/batch`` and must re-poll ``GET /v1/ocr/batch/{job_id}``
across procrastinate retries. procrastinate job args are immutable, so the
gateway ``job_id`` (and submit time, for the poll deadline) are persisted here,
keyed on the document + its content version (``etag``).

One row per in-flight job; the row is deleted once the job reaches a terminal
state. Empty + unused unless batch mode is enabled (gateway-only), so OSS/SQLite
self-hosters get an idle table and zero overhead.

Portable types only (Text + unix-epoch BigInteger timestamps, like the rest of
this schema except the CP-queried usage_events) so the same migration runs on
both self-host SQLite and cloud Postgres.

Revision ID: 008
Revises: 007
Create Date: 2026-06-15 12:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batch_ocr_jobs",
        # Document identity (the same keys the OCR tier receives via the
        # processor ``options``). ``etag`` is the content-version key: a changed
        # document (new etag) is a new job, so a stale row never serves results
        # for the wrong content. The four-column natural key IS the primary key:
        # one in-flight job per (document, content version), and the PK doubles as
        # the unique index ``insert_pending``'s ON CONFLICT target relies on. A
        # resubmit for a new etag inserts a new row; the superseded row is swept on
        # resubmit (delete_stale_for_doc).
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        # The gateway's namespaced batch job id ("<provider>/<batch_job_id>") —
        # the only handle for polling (the gateway is stateless).
        sa.Column("job_id", sa.Text(), nullable=False),
        # Unix-epoch seconds. ``submitted_at`` anchors the poll deadline
        # (DOCUMENT_OCR_BATCH_MAX_WAIT_SECONDS). No status/updated_at column: a row
        # only ever exists in the pending state (terminal jobs are deleted), and
        # the live status always comes from a fresh poll — a stored mirror would
        # be permanently "pending" and carry no information.
        sa.Column("submitted_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id", "doc_id", "doc_type", "etag", name="pk_batch_ocr_jobs"
        ),
    )


def downgrade() -> None:
    op.drop_table("batch_ocr_jobs")
