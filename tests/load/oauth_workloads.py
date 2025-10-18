"""
Multi-User Workflow Definitions for OAuth Load Testing.

Defines coordinated workflows that span multiple users, simulating realistic
collaborative scenarios like note sharing, file collaboration, and permission management.
"""

import asyncio
import json
import logging
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from tests.load.oauth_pool import UserSessionWrapper

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStepResult:
    """Result of a single workflow step."""

    step_name: str
    user: str
    success: bool
    duration: float
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Result of a complete workflow execution."""

    workflow_name: str
    success: bool
    total_duration: float
    steps: list[WorkflowStepResult]
    participants: list[str]
    error: str | None = None

    @property
    def steps_completed(self) -> int:
        """Count of successfully completed steps."""
        return sum(1 for step in self.steps if step.success)

    @property
    def step_latencies(self) -> dict[str, float]:
        """Map of step names to their durations."""
        return {step.step_name: step.duration for step in self.steps}


class Workflow(ABC):
    """
    Base class for multi-user workflows.

    A workflow represents a coordinated sequence of operations across multiple users,
    such as creating and sharing a note, collaborative editing, or permission management.
    """

    def __init__(self, name: str):
        self.name = name
        self.steps: list[WorkflowStepResult] = []
        self.start_time: float | None = None

    @abstractmethod
    async def execute(self, users: list[UserSessionWrapper]) -> WorkflowResult:
        """
        Execute the workflow with the given users.

        Args:
            users: List of UserSessionWrapper instances to use in the workflow

        Returns:
            WorkflowResult with execution details
        """
        pass

    async def _execute_step(
        self,
        step_name: str,
        user: UserSessionWrapper,
        operation: Callable[..., Awaitable[Any]],
        **kwargs,
    ) -> WorkflowStepResult:
        """
        Execute a single workflow step with timing and error handling.

        Args:
            step_name: Name of the step for reporting
            user: User executing the step
            operation: Async callable to execute
            **kwargs: Arguments to pass to the operation

        Returns:
            WorkflowStepResult
        """
        start = time.time()
        try:
            result = await operation(**kwargs)
            duration = time.time() - start
            step_result = WorkflowStepResult(
                step_name=step_name,
                user=user.username,
                success=True,
                duration=duration,
                data={"result": result} if result else {},
            )
            self.steps.append(step_result)
            return step_result
        except Exception as e:
            duration = time.time() - start
            logger.error(f"Step {step_name} failed for user {user.username}: {e}")
            step_result = WorkflowStepResult(
                step_name=step_name,
                user=user.username,
                success=False,
                duration=duration,
                error=str(e),
            )
            self.steps.append(step_result)
            return step_result

    def _finish(self, success: bool, error: str | None = None) -> WorkflowResult:
        """
        Finalize workflow and create result.

        Args:
            success: Whether the overall workflow succeeded
            error: Optional error message

        Returns:
            WorkflowResult
        """
        duration = time.time() - self.start_time if self.start_time else 0.0
        participants = list(set(step.user for step in self.steps))

        return WorkflowResult(
            workflow_name=self.name,
            success=success,
            total_duration=duration,
            steps=self.steps,
            participants=participants,
            error=error,
        )


class NoteShareWorkflow(Workflow):
    """
    Workflow: User A creates a note and shares it with User B, who then reads it.

    Steps:
    1. User A creates a note
    2. User A shares the note with User B (read-only)
    3. User B lists their shared notes (verify propagation)
    4. User B reads the shared note
    """

    def __init__(self):
        super().__init__("note_share")

    async def execute(self, users: list[UserSessionWrapper]) -> WorkflowResult:
        """Execute note sharing workflow."""
        self.start_time = time.time()

        if len(users) < 2:
            return self._finish(False, error="Requires at least 2 users")

        user_a, user_b = users[0], users[1]
        unique_id = uuid.uuid4().hex[:8]

        try:
            # Step 1: User A creates note
            create_result = await self._execute_step(
                "create_note",
                user_a,
                lambda: user_a.call_tool(
                    "nc_notes_create_note",
                    {
                        "title": f"Shared Note {unique_id}",
                        "content": f"Content for workflow test {unique_id}",
                        "category": "Workflows",
                    },
                ),
            )

            if not create_result.success:
                return self._finish(False, error="Failed to create note")

            # Extract note ID
            note_data = json.loads(create_result.data["result"].content[0].text)
            note_id = note_data["id"]

            # Step 2: User A shares note with User B
            # Note: Sharing files/notes requires using WebDAV path
            # Create a file first, then share it
            share_result = await self._execute_step(
                "share_note",
                user_a,
                lambda: user_a.call_tool(
                    "nc_share_create",
                    {
                        "path": f"/Notes/{note_data['category']}/{note_data['title']}.txt",
                        "share_with": user_b.username,
                        "share_type": 0,  # User share
                        "permissions": 1,  # Read-only
                    },
                ),
            )

            if not share_result.success:
                logger.warning("Share creation failed, continuing anyway")

            # Step 3: User B lists shares (measure propagation)
            await self._execute_step(
                "list_shared_with_me",
                user_b,
                lambda: user_b.call_tool("nc_share_list", {"shared_with_me": True}),
            )

            # Step 4: User B reads the note
            await self._execute_step(
                "read_shared_note",
                user_b,
                lambda: user_b.call_tool("nc_notes_get_note", {"note_id": note_id}),
            )

            # Cleanup: Delete the note
            await user_a.call_tool("nc_notes_delete_note", {"note_id": note_id})

            return self._finish(success=True)

        except Exception as e:
            logger.error(f"Note share workflow failed: {e}")
            return self._finish(False, error=str(e))


class CollaborativeEditWorkflow(Workflow):
    """
    Workflow: Multiple users edit the same note concurrently.

    Steps:
    1. User A creates a note
    2. User A shares note with Users B, C (edit permissions)
    3. All users read the note simultaneously
    4. All users update the note simultaneously (test concurrent edits)
    5. User A verifies final state
    """

    def __init__(self):
        super().__init__("collaborative_edit")

    async def execute(self, users: list[UserSessionWrapper]) -> WorkflowResult:
        """Execute collaborative editing workflow."""
        self.start_time = time.time()

        if len(users) < 2:
            return self._finish(False, error="Requires at least 2 users")

        owner = users[0]
        collaborators = users[1:]
        unique_id = uuid.uuid4().hex[:8]

        try:
            # Step 1: Owner creates note
            create_result = await self._execute_step(
                "create_note",
                owner,
                lambda: owner.call_tool(
                    "nc_notes_create_note",
                    {
                        "title": f"Collab Note {unique_id}",
                        "content": f"Initial content {unique_id}",
                        "category": "Collaboration",
                    },
                ),
            )

            if not create_result.success:
                return self._finish(False, error="Failed to create note")

            note_data = json.loads(create_result.data["result"].content[0].text)
            note_id = note_data["id"]

            # Step 2: Read note concurrently by all users
            read_tasks = []
            for i, user in enumerate(users):
                read_tasks.append(
                    self._execute_step(
                        f"concurrent_read_{i}",
                        user,
                        lambda uid=note_id: user.call_tool(
                            "nc_notes_get_note", {"note_id": uid}
                        ),
                    )
                )

            await asyncio.gather(*read_tasks)

            # Step 3: Append content concurrently by all collaborators
            append_tasks = []
            for i, user in enumerate(collaborators):
                append_tasks.append(
                    self._execute_step(
                        f"concurrent_append_{i}",
                        user,
                        lambda _=i, u=user: u.call_tool(
                            "nc_notes_append_content",
                            {
                                "note_id": note_id,
                                "content": f"Addition from {u.username} at {time.time()}",
                            },
                        ),
                    )
                )

            await asyncio.gather(*append_tasks)

            # Step 4: Owner verifies final state
            await self._execute_step(
                "verify_final_state",
                owner,
                lambda: owner.call_tool("nc_notes_get_note", {"note_id": note_id}),
            )

            # Cleanup
            await owner.call_tool("nc_notes_delete_note", {"note_id": note_id})

            return self._finish(success=True)

        except Exception as e:
            logger.error(f"Collaborative edit workflow failed: {e}")
            return self._finish(False, error=str(e))


class FileShareAndDownloadWorkflow(Workflow):
    """
    Workflow: User A uploads a file, shares it with User B, who then downloads it.

    Steps:
    1. User A creates a file via WebDAV
    2. User A shares the file with User B (read-only)
    3. User B lists their shares
    4. User B reads/downloads the file
    """

    def __init__(self):
        super().__init__("file_share_download")

    async def execute(self, users: list[UserSessionWrapper]) -> WorkflowResult:
        """Execute file sharing workflow."""
        self.start_time = time.time()

        if len(users) < 2:
            return self._finish(False, error="Requires at least 2 users")

        user_a, user_b = users[0], users[1]
        unique_id = uuid.uuid4().hex[:8]
        file_path = f"/LoadTest_{unique_id}.txt"

        try:
            # Step 1: User A creates a file
            content = f"Test file content {unique_id}\nCreated for workflow testing"
            create_result = await self._execute_step(
                "create_file",
                user_a,
                lambda: user_a.call_tool(
                    "nc_webdav_put_file",
                    {
                        "path": file_path,
                        "content": content,
                        "content_type": "text/plain",
                    },
                ),
            )

            if not create_result.success:
                return self._finish(False, error="Failed to create file")

            # Step 2: User A shares file with User B
            share_result = await self._execute_step(
                "share_file",
                user_a,
                lambda: user_a.call_tool(
                    "nc_share_create",
                    {
                        "path": file_path,
                        "share_with": user_b.username,
                        "share_type": 0,
                        "permissions": 1,  # Read-only
                    },
                ),
            )

            if not share_result.success:
                logger.warning("File share failed, continuing")

            # Step 3: User B lists shared files
            _ = await self._execute_step(
                "list_shares",
                user_b,
                lambda: user_b.call_tool("nc_share_list", {"shared_with_me": True}),
            )

            # Step 4: User B downloads the file
            _ = await self._execute_step(
                "download_file",
                user_b,
                lambda: user_b.call_tool("nc_webdav_get_file", {"path": file_path}),
            )

            # Cleanup
            await user_a.call_tool("nc_webdav_delete", {"path": file_path})

            return self._finish(success=True)

        except Exception as e:
            logger.error(f"File share workflow failed: {e}")
            return self._finish(False, error=str(e))


class MixedOAuthWorkload:
    """
    Mixed workload combining baseline operations and coordinated workflows.

    Distribution:
    - 50% Baseline operations (individual user CRUD)
    - 30% Note sharing workflows
    - 15% Collaborative editing workflows
    - 5% File sharing workflows
    """

    def __init__(self, users: list[UserSessionWrapper]):
        self.users = users
        self.workflows = {
            "note_share": NoteShareWorkflow(),
            "collaborative_edit": CollaborativeEditWorkflow(),
            "file_share": FileShareAndDownloadWorkflow(),
        }

    async def run_operation(self) -> WorkflowResult | dict[str, Any]:
        """
        Execute one random operation (baseline or workflow).

        Returns:
            WorkflowResult for workflows, dict for baseline operations
        """
        rand = random.random()

        # 50% baseline operations (single-user)
        if rand < 0.50:
            return await self._run_baseline_operation()

        # 30% note sharing
        elif rand < 0.80:
            users = random.sample(self.users, min(2, len(self.users)))
            return await self.workflows["note_share"].execute(users)

        # 15% collaborative editing
        elif rand < 0.95:
            users = random.sample(self.users, min(len(self.users), 3))
            return await self.workflows["collaborative_edit"].execute(users)

        # 5% file sharing
        else:
            users = random.sample(self.users, min(2, len(self.users)))
            return await self.workflows["file_share"].execute(users)

    async def _run_baseline_operation(self) -> dict[str, Any]:
        """Run a baseline single-user operation."""
        user = random.choice(self.users)
        operations = [
            (
                "search_notes",
                lambda: user.call_tool("nc_notes_search_notes", {"query": ""}),
            ),
            ("list_files", lambda: user.call_tool("nc_webdav_list", {"path": "/"})),
            ("get_capabilities", lambda: user.read_resource("nc://capabilities")),
        ]

        op_name, operation = random.choice(operations)
        start = time.time()
        try:
            await operation()
            duration = time.time() - start
            return {
                "type": "baseline",
                "operation": op_name,
                "user": user.username,
                "success": True,
                "duration": duration,
            }
        except Exception as e:
            duration = time.time() - start
            return {
                "type": "baseline",
                "operation": op_name,
                "user": user.username,
                "success": False,
                "duration": duration,
                "error": str(e),
            }
