"""Add document_paths table for per-user shared-document display paths.

ADR-033 Phase 2 (Deck #737). A file visible to more than one user is mounted at
a *different* path per user, but the tenant-wide dedup stores one point set per
``doc_id`` with a single scalar ``file_path`` (pinned to the owner in Phase 1).
This table holds each reader's own mount path, keyed on
``(doc_type, doc_id, user_id)``, joined onto the ~10 returned search results
*after* retrieval so every reader sees their own path. It is a derived,
non-security cache — Qdrant remains the system of record, and a missing/stale row
degrades a displayed path, never a permission or a retrieval result.

One row per (document, reader). The scanner upserts a user's observed path on
every scan (dedup hit or fresh index); a single relational UPDATE replaces the
per-chunk Qdrant ``set_payload`` path-thrash.

Portable types only (Text + unix-epoch BigInteger), like the rest of this schema
(cf. ``batch_ocr_jobs``, migration 008), so the same migration runs on both
self-host SQLite and cloud Postgres.

Write-volume note (accepted trade-off): the scanner currently upserts one row per
tagged file **per scanning user, on every scan pass** — not only for
multi-reader documents — so the table holds ~Σ(files × readers) rows, each
rewritten every scan interval, even on a single-owner corpus. This is a
deliberate swap of a small, idempotent relational upsert for the per-chunk Qdrant
``set_payload`` path-thrash it replaces (a shared doc's path used to be rewritten
across *every chunk* on every reader's pass). Scoping the upsert to actual
cross-user readers (owner content needs no row — the owner's path is the Qdrant
scalar the reader falls back to) would make the table genuinely sparse; it is a
tracked follow-up in ADR-033 rather than part of this change.

Revision ID: 009
Revises: 008
Create Date: 2026-07-21 12:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_paths",
        # Natural key: one path per (document, reader). The three-column key IS
        # the primary key and doubles as the ON CONFLICT target the scanner's
        # upsert relies on.
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        # This reader's mount path for the document. Overwritten in place when the
        # reader observes a new path (rename/move/re-share) — no history kept.
        sa.Column("file_path", sa.Text(), nullable=False),
        # Unix-epoch seconds of the last upsert (observability / staleness only;
        # never used as a filter).
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "doc_type", "doc_id", "user_id", name="pk_document_paths"
        ),
    )
    # Lookup index for the post-retrieval join: given a querying user and a set of
    # returned doc_ids, fetch their paths. The PK is (doc_type, doc_id, user_id),
    # so a user-leading index serves the ``user_id = ? AND doc_id IN (...)`` shape
    # the search join issues.
    op.create_index(
        "ix_document_paths_user_doc",
        "document_paths",
        ["user_id", "doc_type", "doc_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_paths_user_doc", table_name="document_paths")
    op.drop_table("document_paths")
