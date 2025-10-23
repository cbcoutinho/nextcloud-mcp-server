# Testing Client Sessions Architecture

## Overview

This document compares different approaches to managing MCP client sessions in integration tests, addressing the fundamental incompatibility between pytest-asyncio's fixture management and anyio's structured concurrency requirements.

## The Problem

When using pytest-asyncio with anyio-based libraries (like the MCP Python SDK), session-scoped async generator fixtures encounter a fundamental issue:

1. **pytest-asyncio** runs fixture teardown in a **new asyncio task** using `runner.run()`
2. **anyio** requires that cancel scopes be entered and exited in the **same task**
3. This causes `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`

This is a **known limitation** documented in the anyio project and is not a bug in either pytest-asyncio or anyio, but rather an inherent incompatibility between their design philosophies.

## Solution Comparison

### Solution 1: Native Async Context Managers with Surgical Exception Handling ✅ **IMPLEMENTED**

**Approach**: Use native `async with` statements for clean code structure, but add targeted exception handling at the pytest fixture level to handle the expected teardown errors.

**Implementation**:

```python
async def create_mcp_client_session(
    url: str,
    token: str | None = None,
    client_name: str = "MCP",
) -> AsyncGenerator[ClientSession, Any]:
    """Uses native async context managers for clean LIFO cleanup."""
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session

@pytest.fixture(scope="session")
async def nc_mcp_client() -> AsyncGenerator[ClientSession, Any]:
    """Fixture with surgical exception handling for pytest-asyncio incompatibility."""
    try:
        async for session in create_mcp_client_session(
            url="http://localhost:8000/mcp", client_name="Basic MCP"
        ):
            yield session
    except RuntimeError as e:
        # Only catch the specific expected error during pytest teardown
        if "cancel scope" in str(e) and "different task" in str(e):
            logger.debug(f"Ignoring expected pytest-asyncio teardown issue: {e}")
        else:
            # Unexpected RuntimeError - re-raise to fail the test
            raise
```

**Pros**:
- ✅ Clean, idiomatic code using native Python context managers
- ✅ Exception handling is surgical - only catches the specific expected error
- ✅ Unexpected errors still propagate and fail tests
- ✅ Can use session-scoped fixtures for performance
- ✅ Easy to understand and maintain
- ✅ Minimal code changes from original implementation
- ✅ No external dependencies required

**Cons**:
- ⚠️ Still requires exception suppression (though targeted)
- ⚠️ String-based exception matching is somewhat fragile
- ⚠️ Must apply the pattern to each session-scoped fixture
- ⚠️ Doesn't solve the root cause

**Verdict**: **Recommended** - Best balance of code clarity, maintainability, and pragmatism.

---

### Solution 2: Task-Isolated Fixtures

**Approach**: Run each fixture's client session in an isolated anyio task group, allowing independent cleanup without cross-fixture interference.

**Implementation**:

```python
@pytest.fixture(scope="session")
async def nc_mcp_client() -> AsyncGenerator[ClientSession, Any]:
    """Fixture with task isolation for clean teardown."""
    import anyio

    session_holder = {"session": None}

    async def create_and_hold_session():
        """Runs in isolated task - creates session and keeps it alive."""
        async with streamablehttp_client("http://localhost:8000/mcp") as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                session_holder["session"] = session

                # Keep session alive until cancelled
                try:
                    await anyio.sleep_forever()
                except anyio.get_cancelled_exc_class():
                    pass  # Expected cancellation

    async with anyio.create_task_group() as tg:
        tg.start_soon(create_and_hold_session)

        # Wait for session to be ready
        while session_holder["session"] is None:
            await anyio.sleep(0.1)

        yield session_holder["session"]

        # Task group cancellation ensures clean LIFO cleanup
        tg.cancel_scope.cancel()
```

**Pros**:
- ✅ No exception suppression needed
- ✅ Each fixture has its own isolated task scope
- ✅ More theoretically correct approach
- ✅ Can use session-scoped fixtures

**Cons**:
- ❌ Significantly more complex code
- ❌ Harder to understand for developers unfamiliar with anyio
- ❌ Requires understanding of task groups and cancel scopes
- ❌ More boilerplate per fixture
- ❌ Still doesn't solve the fundamental pytest-asyncio incompatibility
- ❌ Polling for session readiness is inelegant
- ❌ Higher cognitive overhead for maintenance

**Verdict**: **Not Recommended** - Complexity outweighs benefits. Consider only if exception handling is completely unacceptable.

---

### Solution 3: Function-Scoped Fixtures with Nested Context Managers

**Approach**: Change fixtures to function scope and rely on Python's context manager nesting for guaranteed LIFO cleanup.

**Implementation**:

```python
@pytest.fixture(scope="function")  # Changed from session
async def nc_mcp_client() -> AsyncGenerator[ClientSession, Any]:
    """Function-scoped fixture with natural LIFO cleanup."""
    async with streamablehttp_client("http://localhost:8000/mcp") as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session

# For tests needing multiple clients:
@pytest.fixture(scope="function")
async def multi_mcp_clients() -> AsyncGenerator[tuple[ClientSession, ClientSession], Any]:
    """Multiple clients with guaranteed LIFO cleanup through nesting."""
    async with streamablehttp_client("http://localhost:8000/mcp") as (read1, write1, _):
        async with ClientSession(read1, write1) as session1:
            await session1.initialize()

            async with streamablehttp_client("http://localhost:8001/mcp") as (read2, write2, _):
                async with ClientSession(read2, write2) as session2:
                    await session2.initialize()
                    yield session1, session2
    # Cleanup: session2 -> stream2 -> session1 -> stream1 (LIFO guaranteed)
```

**Pros**:
- ✅ No exception handling needed
- ✅ Simplest to understand
- ✅ Natural LIFO cleanup through Python's context managers
- ✅ Each test gets fresh clients (better isolation)
- ✅ No workarounds or hacks required

**Cons**:
- ❌ Significantly slower tests (new clients per test)
- ❌ Cannot share client state across tests
- ❌ More resource intensive
- ❌ Higher overhead for test suite execution
- ❌ May not be practical for expensive fixtures (e.g., OAuth tokens)
- ❌ Nested context managers become unwieldy with many clients

**Verdict**: **Good Alternative** - Consider for specific fixtures where session scope isn't critical, or for new test files where performance isn't a concern.

---

### Solution 4: Use pytest-trio Instead of pytest-asyncio (Future)

**Approach**: Replace pytest-asyncio with pytest-trio, which was designed with structured concurrency in mind.

**Implementation**:

```python
# pyproject.toml
[tool.pytest.ini_options]
# Remove: asyncio_mode = "auto"
# Add: trio_mode = "auto"

# Fixtures work naturally with trio
@pytest.fixture(scope="session")
async def nc_mcp_client() -> AsyncGenerator[ClientSession, Any]:
    async with streamablehttp_client("http://localhost:8000/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
```

**Pros**:
- ✅ No workarounds needed
- ✅ Designed for structured concurrency
- ✅ Theoretically cleanest solution
- ✅ Can use session-scoped fixtures naturally

**Cons**:
- ❌ Requires switching from asyncio to trio backend
- ❌ Major refactoring required
- ❌ May break existing code that assumes asyncio
- ❌ Dependency changes throughout project
- ❌ Team needs to learn trio ecosystem
- ❌ Less ecosystem support than asyncio

**Verdict**: **Not Practical** - Too disruptive for existing projects. Consider only for greenfield projects or major rewrites.

---

## Decision Matrix

| Solution | Code Clarity | Maintenance | Performance | Safety | Effort |
|----------|--------------|-------------|-------------|--------|--------|
| **Solution 1** (Implemented) | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Solution 2 (Task-Isolated) | ⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| Solution 3 (Function-Scoped) | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Solution 4 (pytest-trio) | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ |

## Implementation Details

### What Changed in Solution 1

1. **`create_mcp_client_session` function** (conftest.py:61-110):
   - Replaced manual `__aenter__`/`__aexit__` calls with native `async with` statements
   - Removed blanket exception suppression from cleanup logic
   - Added clear documentation about LIFO cleanup order
   - Simplified from ~60 lines to ~40 lines

2. **Session-scoped MCP client fixtures** (conftest.py:148-1269):
   - Added targeted exception handling wrapper
   - Only catches specific "cancel scope" + "different task" RuntimeError
   - All other exceptions propagate normally
   - Applied to: `nc_mcp_client`, `nc_mcp_oauth_client`, `alice_mcp_client`, `bob_mcp_client`, `charlie_mcp_client`, `diana_mcp_client`

3. **Documentation**:
   - Added comprehensive docstrings explaining the workaround
   - Referenced MCP SDK issue #577 for context
   - Documented why this is necessary and not a bug

### Benefits of This Implementation

1. **Clean Core Logic**: The `create_mcp_client_session` function is now clean, idiomatic Python with no workarounds
2. **Isolated Workaround**: Exception handling is confined to pytest fixture level where the issue actually occurs
3. **Surgical Exception Handling**: Only catches the specific expected error, not all RuntimeErrors
4. **Performance**: Maintains session-scoped fixtures for fast test execution
5. **Maintainability**: Easy to understand and modify
6. **Safety**: Real errors still cause test failures

## Testing Results

All tests pass cleanly with the implementation:

```bash
$ uv run pytest tests/server/test_mcp.py -v
============================================= test session starts ==============================================
tests/server/test_mcp.py::test_mcp_connectivity PASSED                                            [ 16%]
tests/server/test_mcp.py::test_mcp_notes_crud_workflow PASSED                                     [ 33%]
tests/server/test_mcp.py::test_mcp_notes_etag_conflict PASSED                                     [ 50%]
tests/server/test_mcp.py::test_mcp_webdav_workflow PASSED                                         [ 66%]
tests/server/test_mcp.py::test_mcp_resources_access PASSED                                        [ 83%]
tests/server/test_mcp.py::test_mcp_calendar_workflow PASSED                                       [100%]
============================================== 6 passed in 39.52s ==============================================
```

## Recommendations

### For This Project: Solution 1 ✅

The implemented solution (Solution 1) is the best fit because:
- Minimal disruption to existing tests
- Clean, maintainable code
- Good performance with session-scoped fixtures
- Targeted exception handling that doesn't hide real errors

### For New Test Files: Consider Solution 3

For new test files where performance isn't critical, consider using function-scoped fixtures (Solution 3):
- No workarounds needed
- Perfect code clarity
- Better test isolation

### For Greenfield Projects: Consider Solution 4

For new projects starting from scratch, consider pytest-trio instead of pytest-asyncio:
- Native structured concurrency support
- No workarounds needed
- Better alignment with modern async Python patterns

## Related Resources

- [MCP Python SDK Issue #577](https://github.com/modelcontextprotocol/python-sdk/issues/577) - Original issue report
- [Anyio Issue #345](https://github.com/agronholm/anyio/issues/345) - Discussion of fixture limitations
- [Nextcloud MCP Note 378555](nextcloud://notes/378555) - Detailed investigation notes
- pytest-asyncio documentation: https://pytest-asyncio.readthedocs.io/
- anyio structured concurrency guide: https://anyio.readthedocs.io/en/stable/basics.html

## Appendix: Why Can't This Be Fixed Upstream?

The incompatibility cannot be "fixed" in either pytest-asyncio or anyio without breaking their core design:

1. **pytest-asyncio** needs to manage fixture lifecycle across different scopes, requiring separate task creation for cleanup
2. **anyio** enforces structured concurrency guarantees by requiring same-task cancel scope entry/exit
3. These requirements are fundamentally incompatible

The maintainers of both projects are aware of this issue, and it's considered an acceptable trade-off given their respective design goals. The recommended approach is to handle it at the application level, as we've done here.
