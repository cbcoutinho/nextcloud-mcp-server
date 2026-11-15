"""Apply procrastinate 3.11's schema migration to an existing ingest queue.

We create procrastinate's tables only when they are missing (see
``vector/queue/procrastinate.py``), so a database created on an older release
never receives procrastinate's own upgrade scripts. This revision carries the
one migration procrastinate 3.11.0 ships,
``03.11.00_01_pre_fetch_job_skip_null_lock.sql``, inlined verbatim.
Revisions are snapshots, so the SQL is not read from the installed package.

The migration replaces ``procrastinate_fetch_job_v2`` with a version that
accepts lock-less jobs before evaluating the lock subquery. The signature and
semantics are unchanged; only the query plan improves. It is a ``pre``
migration, so 3.10 workers still running during a rolling update keep working.

The upgrade is skipped on SQLite (procrastinate is Postgres-only) and on a
Postgres database without the ingest queue: a fresh install gets the new
function from 3.11's ``schema.sql`` when the queue schema is applied after
Alembic.

When we adopt procrastinate's own Alembic branch (procrastinate PR 1584,
Deck #1494), stamp that branch at the revision matching 3.11: this revision
has already applied it.

Revision ID: 011
Revises: 010
Create Date: 2026-11-15 20:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None

# procrastinate 3.11.0: sql/migrations/03.11.00_01_pre_fetch_job_skip_null_lock.sql
_FETCH_JOB_V2_3_11 = """
CREATE OR REPLACE FUNCTION procrastinate_fetch_job_v2(
    target_queue_names character varying[],
    p_worker_id bigint
)
    RETURNS procrastinate_jobs
    LANGUAGE plpgsql
AS $$
DECLARE
	found_jobs procrastinate_jobs;
BEGIN
    WITH candidate AS (
        SELECT jobs.*
            FROM procrastinate_jobs AS jobs
            WHERE
                -- reject the job if its lock has earlier or higher priority jobs.
                -- Jobs without a lock are accepted before the subquery is considered:
                -- keeping "jobs.lock IS NOT NULL" inside the subquery lets Postgres
                -- plan a nested loop that scans every other job for each NULL-lock job.
                (
                    jobs.lock IS NULL
                    OR NOT EXISTS (
                        SELECT 1
                            FROM procrastinate_jobs AS other_jobs
                            WHERE
                                other_jobs.lock = jobs.lock
                                AND (
                                    -- job with same lock is already running
                                    other_jobs.status = 'doing'
                                    OR
                                    -- job with same lock is waiting and has higher priority (or same priority but was queued first)
                                    (
                                        other_jobs.status = 'todo'
                                        AND (
                                            other_jobs.priority > jobs.priority
                                            OR (
                                            other_jobs.priority = jobs.priority
                                            AND other_jobs.id < jobs.id
                                            )
                                        )
                                    )
                                )
                    )
                )
                AND jobs.status = 'todo'
                AND (target_queue_names IS NULL OR jobs.queue_name = ANY( target_queue_names ))
                AND (jobs.scheduled_at IS NULL OR jobs.scheduled_at <= now())
            ORDER BY jobs.priority DESC, jobs.id ASC LIMIT 1
            FOR UPDATE OF jobs SKIP LOCKED
    )
    UPDATE procrastinate_jobs
        SET status = 'doing', worker_id = p_worker_id
        FROM candidate
        WHERE procrastinate_jobs.id = candidate.id
        RETURNING procrastinate_jobs.* INTO found_jobs;

 RETURN found_jobs;
END;
$$;
"""

# procrastinate 3.3.0-3.10.x: sql/migrations/03.03.00_01_pre_priority_lock_fetch_job.sql
# Same signature, so CREATE OR REPLACE restores it without the original DROP.
_FETCH_JOB_V2_3_3 = """
CREATE OR REPLACE FUNCTION procrastinate_fetch_job_v2(
    target_queue_names character varying[],
    p_worker_id bigint
)
    RETURNS procrastinate_jobs
    LANGUAGE plpgsql
AS $$
DECLARE
	found_jobs procrastinate_jobs;
BEGIN
    WITH candidate AS (
        SELECT jobs.*
            FROM procrastinate_jobs AS jobs
            WHERE
                -- reject the job if its lock has earlier or higher priority jobs
                NOT EXISTS (
                    SELECT 1
                        FROM procrastinate_jobs AS other_jobs
                        WHERE
                            jobs.lock IS NOT NULL
                            AND other_jobs.lock = jobs.lock
                            AND (
                                -- job with same lock is already running
                                other_jobs.status = 'doing'
                                OR
                                -- job with same lock is waiting and has higher priority (or same priority but was queued first)
                                (
                                    other_jobs.status = 'todo'
                                    AND (
                                        other_jobs.priority > jobs.priority
                                        OR (
                                        other_jobs.priority = jobs.priority
                                        AND other_jobs.id < jobs.id
                                        )
                                    )
                                )
                            )
                )
                AND jobs.status = 'todo'
                AND (target_queue_names IS NULL OR jobs.queue_name = ANY( target_queue_names ))
                AND (jobs.scheduled_at IS NULL OR jobs.scheduled_at <= now())
            ORDER BY jobs.priority DESC, jobs.id ASC LIMIT 1
            FOR UPDATE OF jobs SKIP LOCKED
    )
    UPDATE procrastinate_jobs
        SET status = 'doing', worker_id = p_worker_id
        FROM candidate
        WHERE procrastinate_jobs.id = candidate.id
        RETURNING procrastinate_jobs.* INTO found_jobs;

 RETURN found_jobs;
END;
$$;
"""


def _has_ingest_queue() -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    return bool(
        bind.execute(
            sa.text("SELECT to_regclass('procrastinate_jobs') IS NOT NULL")
        ).scalar()
    )


def upgrade() -> None:
    if _has_ingest_queue():
        op.execute(_FETCH_JOB_V2_3_11)


def downgrade() -> None:
    if _has_ingest_queue():
        op.execute(_FETCH_JOB_V2_3_3)
