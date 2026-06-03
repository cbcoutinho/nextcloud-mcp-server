"""Unit tests for the procrastinate ingest producer + task (Deck #183).

Uses procrastinate's in-memory connector so no live Postgres is required.
"""

from typing import cast
from unittest.mock import AsyncMock

import pytest
from procrastinate import App, JobContext, testing

import nextcloud_mcp_server.vector.queue.procrastinate as pq
from nextcloud_mcp_server.vector.scanner import DocumentTask

pytestmark = pytest.mark.unit


@pytest.fixture
def app():
    """An App bound to the in-memory connector with the ingest tasks."""
    return pq.build_app(testing.InMemoryConnector())


def _task(doc_id="42", doc_type="note", operation="index"):
    return DocumentTask(
        user_id="alice",
        doc_id=doc_id,
        doc_type=doc_type,
        operation=operation,
        modified_at=100,
        etag="etag-abc",
    )


class TestProcrastinateTaskProducer:
    async def test_send_defers_with_correct_job_shape(self, app):
        async with app.open_async():
            producer = pq.ProcrastinateTaskProducer(app)
            await producer.send(_task())

        jobs = list(app.connector.jobs.values())
        assert len(jobs) == 1
        job = jobs[0]
        assert job["task_name"] == pq.INGEST_TASK_NAME
        assert job["queue_name"] == pq.INGEST_QUEUE_NAME
        assert job["queueing_lock"] == "alice:note:42"
        assert job["lock"] is None  # no execution lock (crash-deadlock guard)
        assert job["args"]["doc_id"] == "42"
        assert job["args"]["etag"] == "etag-abc"

    async def test_duplicate_send_is_deduped(self, app):
        async with app.open_async():
            producer = pq.ProcrastinateTaskProducer(app)
            await producer.send(_task())
            # Same doc again → AlreadyEnqueued, swallowed; still one job.
            await producer.send(_task())
        assert len(app.connector.jobs) == 1

    async def test_distinct_docs_create_separate_jobs(self, app):
        async with app.open_async():
            producer = pq.ProcrastinateTaskProducer(app)
            await producer.send(_task(doc_id="1"))
            await producer.send(_task(doc_id="2"))
        assert len(app.connector.jobs) == 2

    def test_clone_returns_self(self, app):
        producer = pq.ProcrastinateTaskProducer(app)
        assert producer.clone() is producer

    async def test_connect_opens_pool_and_drain_closes(self, app, monkeypatch):
        # connect() resolves the process-wide app; point it at our in-memory one.
        monkeypatch.setattr(pq, "get_procrastinate_app", lambda: app)

        producer = await pq.ProcrastinateTaskProducer.connect()
        # `await app.open_async()` must actually open the connector (regression
        # guard for the await-vs-`async with` form on the long-lived pool).
        assert app.connector.states == ["open_async"]

        # An open pool means send() works end-to-end.
        await producer.send(_task())
        assert len(app.connector.jobs) == 1

        await producer.drain()
        assert "closed_async" in app.connector.states


class TestProcessDocumentTask:
    async def test_runs_pipeline_and_closes_client(self, monkeypatch):
        captured = {}

        fake_client = AsyncMock()

        async def fake_resolve(user_id):
            captured["user_id"] = user_id
            return fake_client

        async def fake_process(task, nc_client, *, max_retries):
            captured["task"] = task
            captured["nc_client"] = nc_client
            captured["max_retries"] = max_retries

        monkeypatch.setattr(pq, "_resolve_client", fake_resolve)
        monkeypatch.setattr(
            "nextcloud_mcp_server.vector.processor.process_document", fake_process
        )

        # Calling the Task runs its wrapped function in-process.
        await pq.process_document_task(
            user_id="alice",
            doc_id="42",
            doc_type="note",
            operation="index",
            modified_at=100,
            etag="e1",
        )

        assert captured["user_id"] == "alice"
        assert isinstance(captured["task"], DocumentTask)
        assert captured["task"].doc_id == "42"
        assert captured["task"].etag == "e1"
        # Worker disables the in-process retry loop; durable retry is the queue's.
        assert captured["max_retries"] == 1
        fake_client.close.assert_awaited_once()

    async def test_pipeline_error_propagates_and_closes_client(self, monkeypatch):
        # A non-credential failure must propagate (so procrastinate's
        # RetryStrategy picks it up) and still close the client via finally.
        fake_client = AsyncMock()

        async def fake_resolve(user_id):
            return fake_client

        async def fake_process(task, nc_client, *, max_retries):
            raise RuntimeError("transient qdrant failure")

        monkeypatch.setattr(pq, "_resolve_client", fake_resolve)
        monkeypatch.setattr(
            "nextcloud_mcp_server.vector.processor.process_document", fake_process
        )

        with pytest.raises(RuntimeError, match="transient qdrant failure"):
            await pq.process_document_task(
                user_id="alice",
                doc_id="42",
                doc_type="note",
                operation="index",
                modified_at=100,
            )
        fake_client.close.assert_awaited_once()

    async def test_skips_on_missing_credentials(self, monkeypatch):
        from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

        async def fake_resolve(user_id):
            raise NotProvisionedError("no app password")

        called = False

        async def fake_process(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(pq, "_resolve_client", fake_resolve)
        monkeypatch.setattr(
            "nextcloud_mcp_server.vector.processor.process_document", fake_process
        )

        # Returns cleanly (job succeeds as a no-op); pipeline never runs.
        await pq.process_document_task(
            user_id="ghost",
            doc_id="9",
            doc_type="note",
            operation="index",
            modified_at=0,
        )
        assert called is False


class TestReclaimStalledJobs:
    async def test_reclaims_each_stalled_job(self):
        from datetime import datetime

        retried: list[int] = []

        class Job:
            def __init__(self, id):
                self.id = id

        class FakeManager:
            async def get_stalled_jobs(self, queue=None, seconds_since_heartbeat=0):
                assert queue == pq.INGEST_QUEUE_NAME
                return [Job(1), Job(2), Job(None)]  # None id is skipped

            async def retry_job_by_id_async(self, job_id, retry_at):
                assert isinstance(retry_at, datetime)
                retried.append(job_id)

        class FakeApp:
            job_manager = FakeManager()

        class Ctx:
            app = FakeApp()

        await pq.reclaim_stalled_ingest_jobs(cast(JobContext, Ctx()), timestamp=0)
        assert retried == [1, 2]


class TestGetIngestJobCounts:
    async def test_aggregates_stats_rows(self):
        class FakeManager:
            async def list_queues_async(self, queue=None):
                assert queue == pq.INGEST_QUEUE_NAME
                # procrastinate flattens per-status stats into top-level keys.
                return [
                    {
                        "name": "ingest",
                        "jobs_count": 6,
                        "todo": 3,
                        "doing": 1,
                        "succeeded": 0,
                        "failed": 2,
                        "cancelled": 0,
                        "aborted": 0,
                    }
                ]

        class FakeApp:
            job_manager = FakeManager()

        counts = await pq.get_ingest_job_counts(cast(App, FakeApp()))
        assert counts["todo"] == 3
        assert counts["doing"] == 1
        assert counts["failed"] == 2
        assert counts["succeeded"] == 0
