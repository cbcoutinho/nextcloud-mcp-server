"""
Workload definitions for load testing the MCP server.

Defines realistic operation mixes and individual operation functions.
"""

import json
import logging
import random
import time
import uuid

from mcp import ClientSession

logger = logging.getLogger(__name__)


class OperationResult:
    """Result of a single operation execution."""

    def __init__(
        self,
        operation: str,
        success: bool,
        duration: float,
        error: str | None = None,
    ):
        self.operation = operation
        self.success = success
        self.duration = duration
        self.error = error
        self.timestamp = time.time()


class WorkloadOperations:
    """Collection of MCP operations for load testing."""

    def __init__(self, session: ClientSession):
        self.session = session
        self._created_notes: list[int] = []
        self._created_boards: list[int] = []

    async def get_capabilities(self) -> OperationResult:
        """Fetch server capabilities (lightweight operation)."""
        start = time.time()
        try:
            await self.session.read_resource("nc://capabilities")
            duration = time.time() - start
            return OperationResult("get_capabilities", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("get_capabilities", False, duration, str(e))

    async def list_notes(self) -> OperationResult:
        """List all notes (read operation)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_notes_search_notes", {"query": ""})
            duration = time.time() - start
            return OperationResult("list_notes", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("list_notes", False, duration, str(e))

    async def search_notes(self, query: str = "test") -> OperationResult:
        """Search notes by query (read operation with filtering)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_notes_search_notes", {"query": query})
            duration = time.time() - start
            return OperationResult("search_notes", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("search_notes", False, duration, str(e))

    async def create_note(self) -> OperationResult:
        """Create a new note (write operation)."""
        start = time.time()
        unique_id = uuid.uuid4().hex[:8]
        try:
            result = await self.session.call_tool(
                "nc_notes_create_note",
                {
                    "title": f"Load Test Note {unique_id}",
                    "content": f"Content for load test note {unique_id}",
                    "category": "LoadTesting",
                },
            )
            duration = time.time() - start

            # Track created note ID for cleanup
            if result and len(result.content) > 0:
                content = result.content[0]
                if hasattr(content, "text"):
                    note_data = json.loads(content.text)
                    note_id = note_data.get("id")
                    if note_id:
                        self._created_notes.append(note_id)

            return OperationResult("create_note", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("create_note", False, duration, str(e))

    async def get_note(self, note_id: int) -> OperationResult:
        """Get a specific note by ID (read operation)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_notes_get_note", {"note_id": note_id})
            duration = time.time() - start
            return OperationResult("get_note", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("get_note", False, duration, str(e))

    async def update_note(self, note_id: int, etag: str) -> OperationResult:
        """Update an existing note (write operation)."""
        start = time.time()
        try:
            await self.session.call_tool(
                "nc_notes_update_note",
                {
                    "note_id": note_id,
                    "etag": etag,
                    "title": f"Updated Note {note_id}",
                    "content": f"Updated content at {time.time()}",
                    "category": "LoadTesting",
                },
            )
            duration = time.time() - start
            return OperationResult("update_note", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("update_note", False, duration, str(e))

    async def delete_note(self, note_id: int) -> OperationResult:
        """Delete a note (write operation)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_notes_delete_note", {"note_id": note_id})
            duration = time.time() - start
            # Remove from tracking
            if note_id in self._created_notes:
                self._created_notes.remove(note_id)
            return OperationResult("delete_note", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("delete_note", False, duration, str(e))

    async def list_webdav_files(self, path: str = "/") -> OperationResult:
        """List files via WebDAV (read operation)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_webdav_list", {"path": path})
            duration = time.time() - start
            return OperationResult("list_webdav_files", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("list_webdav_files", False, duration, str(e))

    async def list_calendars(self) -> OperationResult:
        """List calendars (read operation)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_calendar_list_calendars", {})
            duration = time.time() - start
            return OperationResult("list_calendars", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("list_calendars", False, duration, str(e))

    async def list_deck_boards(self) -> OperationResult:
        """List deck boards (read operation)."""
        start = time.time()
        try:
            await self.session.call_tool("nc_deck_list_boards", {})
            duration = time.time() - start
            return OperationResult("list_deck_boards", True, duration)
        except Exception as e:
            duration = time.time() - start
            return OperationResult("list_deck_boards", False, duration, str(e))

    async def cleanup(self):
        """Clean up any resources created during testing."""
        logger.info(f"Cleaning up {len(self._created_notes)} test notes...")
        for note_id in self._created_notes[:]:
            try:
                await self.delete_note(note_id)
            except Exception as e:
                logger.warning(f"Failed to delete note {note_id}: {e}")


class MixedWorkload:
    """
    Realistic mixed workload simulating typical MCP server usage.

    Operation distribution:
    - 40% Notes read (list/get/search)
    - 20% Notes write (create/update/delete)
    - 15% Notes search
    - 10% WebDAV operations
    - 10% Calendar operations
    - 5% Other (capabilities, deck)
    """

    def __init__(self, operations: WorkloadOperations):
        self.ops = operations
        # Pre-create some notes for read/update operations
        self._warmup_note_ids: list[tuple[int, str]] = []

    async def warmup(self, count: int = 10):
        """Create initial notes for read/update operations."""
        logger.info(f"Warming up with {count} test notes...")
        for _ in range(count):
            result = await self.ops.create_note()
            if result.success and self.ops._created_notes:
                note_id = self.ops._created_notes[-1]
                # Get the note to fetch its etag
                try:
                    get_result = await self.ops.session.call_tool(
                        "nc_notes_get_note", {"note_id": note_id}
                    )
                    if get_result and len(get_result.content) > 0:
                        note_data = json.loads(get_result.content[0].text)
                        etag = note_data.get("etag", "")
                        self._warmup_note_ids.append((note_id, etag))
                except Exception as e:
                    logger.warning(f"Failed to get etag for note {note_id}: {e}")

    async def run_operation(self) -> OperationResult:
        """Execute one random operation based on the workload distribution."""
        rand = random.random()

        # 40% reads (list/get/search)
        if rand < 0.40:
            op_rand = random.random()
            if op_rand < 0.5:
                return await self.ops.list_notes()
            elif op_rand < 0.8 and self._warmup_note_ids:
                note_id, _ = random.choice(self._warmup_note_ids)
                return await self.ops.get_note(note_id)
            else:
                return await self.ops.search_notes()

        # 20% writes (create/update/delete)
        elif rand < 0.60:
            op_rand = random.random()
            if op_rand < 0.5:
                return await self.ops.create_note()
            elif op_rand < 0.8 and self._warmup_note_ids:
                note_id, etag = random.choice(self._warmup_note_ids)
                return await self.ops.update_note(note_id, etag)
            elif self.ops._created_notes and len(self.ops._created_notes) > 5:
                # Only delete if we have enough notes
                note_id = random.choice(self.ops._created_notes)
                return await self.ops.delete_note(note_id)
            else:
                return await self.ops.create_note()

        # 15% search
        elif rand < 0.75:
            queries = ["test", "load", "note", "content", ""]
            return await self.ops.search_notes(random.choice(queries))

        # 10% WebDAV
        elif rand < 0.85:
            return await self.ops.list_webdav_files()

        # 10% Calendar
        elif rand < 0.95:
            return await self.ops.list_calendars()

        # 5% Other
        else:
            op_rand = random.random()
            if op_rand < 0.5:
                return await self.ops.get_capabilities()
            else:
                return await self.ops.list_deck_boards()
