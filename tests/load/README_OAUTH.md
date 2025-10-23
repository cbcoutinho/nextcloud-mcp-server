# OAuth Multi-User Load Testing Framework

Comprehensive multi-user benchmarking system for testing OAuth-authenticated Nextcloud MCP server with realistic collaborative workflows.

## Quick Start

```bash
# 1. Ensure docker-compose is running
docker-compose up -d

# 2. Run a benchmark with 2 users for 30 seconds
uv run python -m tests.load.oauth_benchmark --users 2 --duration 30

# 3. Clean up test users (IMPORTANT - always run after benchmark)
uv run python -m tests.load.cleanup_loadtest_users

# Optional: Verify cleanup
uv run python -m tests.load.cleanup_loadtest_users --dry-run
```

## Overview

This framework extends the basic load testing infrastructure to support:
- **Multiple OAuth-authenticated users** running concurrently
- **Coordinated workflows** spanning multiple users (sharing, collaboration, permissions)
- **Per-user metrics** tracking individual user performance
- **Workflow-specific metrics** measuring cross-user operation latencies
- **Realistic scenarios** mimicking actual user collaboration patterns
- **Concurrent user creation** - all users created and authenticated in parallel for fast setup

## Architecture

### Components

```
tests/load/
├── oauth_pool.py          # OAuth user pool management
├── oauth_workloads.py     # Multi-user workflow definitions
├── oauth_metrics.py       # Enhanced metrics collection
├── oauth_benchmark.py     # Main CLI entry point
└── README_OAUTH.md        # This file
```

### Key Classes

**OAuthUserPool** (`oauth_pool.py`)
- Manages N OAuth-authenticated users
- Handles token acquisition and storage
- Creates and manages MCP sessions per user
- Tracks per-user operation statistics

**UserSessionWrapper** (`oauth_pool.py`)
- Wraps MCP ClientSession for a specific user
- Automatic operation tracking
- Convenient tool/resource access methods

**Workflow** (`oauth_workloads.py`)
- Base class for multi-user coordinated workflows
- Step-by-step execution with timing
- Comprehensive error handling and reporting

**OAuthBenchmarkMetrics** (`oauth_metrics.py`)
- Per-user operation counts and latencies
- Workflow completion rates and timings
- Baseline operation statistics
- Detailed reporting and JSON export

## Available Workflows

### 1. NoteShareWorkflow
**Scenario**: Alice creates a note and shares it with Bob, who then reads it.

**Steps**:
1. User A creates a note
2. User A shares note with User B (read-only permissions)
3. User B lists their shared notes (measures propagation delay)
4. User B reads the shared note

**Metrics**: Creation latency, share propagation time, read latency

### 2. CollaborativeEditWorkflow
**Scenario**: Multiple users concurrently edit the same note.

**Steps**:
1. Owner creates a note
2. All users read the note simultaneously
3. All users append content concurrently
4. Owner verifies final state

**Metrics**: Concurrent read latency, concurrent write conflicts, final state consistency

### 3. FileShareAndDownloadWorkflow
**Scenario**: Alice uploads a file, shares it with Bob, who then downloads it.

**Steps**:
1. User A creates a file via WebDAV
2. User A shares file with User B (read-only)
3. User B lists their shares
4. User B downloads the file

**Metrics**: Upload latency, share creation, download latency

### 4. MixedOAuthWorkload
**Distribution**:
- 50% Baseline operations (individual user CRUD)
- 30% Note sharing workflows
- 15% Collaborative editing workflows
- 5% File sharing workflows

## Usage

### Basic Usage

```bash
# 4 users, 60-second test with mixed workload
uv run python -m tests.load.oauth_benchmark --users 4 --duration 60

# 10 users, 5-minute test
uv run python -m tests.load.oauth_benchmark -u 10 -d 300

# Export results to JSON
uv run python -m tests.load.oauth_benchmark -u 5 -d 120 --output results.json
```

### Advanced Options

```bash
# Sharing-focused workload
uv run python -m tests.load.oauth_benchmark --workload sharing -u 8 -d 180

# Collaborative editing workload
uv run python -m tests.load.oauth_benchmark --workload collaboration -u 6 -d 120

# Baseline operations only (no workflows)
uv run python -m tests.load.oauth_benchmark --workload baseline -u 10 -d 60

# Verbose logging for debugging
uv run python -m tests.load.oauth_benchmark -u 2 -d 30 --verbose
```

### CLI Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--users` | `-u` | 2 | Number of concurrent users (dynamically created) |
| `--duration` | `-d` | 30.0 | Test duration in seconds |
| `--warmup` | `-w` | 5.0 | Warmup period before metrics collection (seconds) |
| `--url` | | `http://localhost:8001/mcp` | MCP OAuth server URL |
| `--output` | `-o` | None | JSON output file path |
| `--workload` | | `mixed` | Workload type: mixed, sharing, collaboration, baseline |
| `--user-prefix` | | `loadtest` | Prefix for dynamically created usernames |
| `--cleanup/--no-cleanup` | | `cleanup` | Delete created users after benchmark |
| `--browser` | | `chromium` | Playwright browser: firefox, chromium, webkit |
| `--headed` | | False | Run browser in headed mode (visible window) |
| `--verbose` | `-v` | False | Enable verbose logging |

## Test User Creation

The framework **dynamically creates test users** on-demand with OAuth authentication:

- **Naming**: Users are created with the pattern `{prefix}_user_{n}` (default: `loadtest_user_1`, `loadtest_user_2`, etc.)
- **Customization**: Use `--user-prefix` to change the prefix (e.g., `--user-prefix mytest` → `mytest_user_1`)
- **Scalability**: No limit on user count - create as many concurrent users as your system can handle
- **Credentials**: Each user gets a randomly generated secure password
- **OAuth Tokens**: All users authenticate via automated OAuth flow using Playwright
- **Cleanup**: Users are automatically deleted after the benchmark (disable with `--no-cleanup`)

**Example**: Running `--users 5` creates:
- `loadtest_user_1` (Display: Load Test User 1, Email: loadtest_user_1@benchmark.local)
- `loadtest_user_2` (Display: Load Test User 2, Email: loadtest_user_2@benchmark.local)
- `loadtest_user_3` (Display: Load Test User 3, Email: loadtest_user_3@benchmark.local)
- `loadtest_user_4` (Display: Load Test User 4, Email: loadtest_user_4@benchmark.local)
- `loadtest_user_5` (Display: Load Test User 5, Email: loadtest_user_5@benchmark.local)

## Metrics Output

### Console Report

```
================================================================================
OAUTH MULTI-USER BENCHMARK RESULTS
================================================================================

Duration: 120.45s
Total Users: 5
Total Workflows Executed: 312
Total Baseline Operations: 678

--------------------------------------------------------------------------------
WORKFLOW STATISTICS
--------------------------------------------------------------------------------
Workflow                         Total  Success     Rate        P50        P95
--------------------------------------------------------------------------------
note_share                         112      109    97.3%   0.2341s   0.4782s
collaborative_edit                  65       61    93.8%   0.5123s   0.9234s
file_share                          29       29   100.0%   0.3456s   0.6123s

--------------------------------------------------------------------------------
PER-USER STATISTICS
--------------------------------------------------------------------------------
User                  Total Ops    Success   Errors     Rate        P50
--------------------------------------------------------------------------------
loadtest_user_1              289        283        6    97.9%   0.2456s
loadtest_user_2              245        241        4    98.4%   0.2123s
loadtest_user_3              231        226        5    97.8%   0.2345s
loadtest_user_4              198        195        3    98.5%   0.2234s
loadtest_user_5              187        184        3    98.4%   0.2189s

--------------------------------------------------------------------------------
BASELINE OPERATIONS
--------------------------------------------------------------------------------
Total Operations: 678
Success Rate: 98.2%
Latency: min=0.0234s, p50=0.1234s, p95=0.3456s, max=0.8123s
================================================================================
```

### JSON Export

```json
{
  "summary": {
    "duration": 120.45,
    "total_workflows": 312,
    "total_baseline_ops": 678,
    "total_users": 5
  },
  "workflows": {
    "note_share": {
      "total_executions": 112,
      "successful_executions": 109,
      "failed_executions": 3,
      "success_rate": 97.3,
      "latency": {
        "min": 0.1234,
        "max": 0.8765,
        "mean": 0.2891,
        "median": 0.2341,
        "p90": 0.4123,
        "p95": 0.4782,
        "p99": 0.7234
      },
      "step_latencies": {
        "create_note": {...},
        "share_note": {...},
        "list_shared_with_me": {...},
        "read_shared_note": {...}
      }
    }
  },
  "users": {
    "loadtest_user_1": {
      "total_operations": 289,
      "successful_operations": 283,
      "failed_operations": 6,
      "success_rate": 97.9,
      "latency": {...},
      "operations_breakdown": {...},
      "errors_breakdown": {...}
    },
    "loadtest_user_2": {...},
    "loadtest_user_3": {...},
    "loadtest_user_4": {...},
    "loadtest_user_5": {...}
  },
  "baseline": {...}
}
```

## Implementation Status

### ✅ Completed Components

**Framework:**
- OAuth user pool management with dynamic user creation
- User session wrappers with automatic tracking
- Workflow base classes and framework
- 3 example workflows (note share, collaborative edit, file share)
- Enhanced metrics with per-user and workflow tracking
- CLI interface with multiple workload options
- Comprehensive reporting (console + JSON)

**OAuth Integration:**
- ✅ Playwright browser automation for OAuth login
- ✅ OAuth callback server for auth code capture
- ✅ Token exchange with OIDC provider
- ✅ OAuth token injection into MCP sessions via Authorization headers
- ✅ Cancel scope error handling for reliable cleanup
- ✅ Dynamic user creation and deletion via Nextcloud Users API

**Implementation Details:**
The benchmark now successfully:
1. Creates Nextcloud users dynamically with unique passwords
2. Acquires OAuth tokens via automated Playwright browser flows
3. Creates MCP client sessions with proper `Authorization: Bearer {token}` headers
4. Executes coordinated multi-user workflows
5. Tracks per-user and per-workflow metrics
6. Provides standalone cleanup utility for test users

**Key Fix (oauth_pool.py:163-164)**:
```python
# Pass OAuth token as Authorization header
headers = {"Authorization": f"Bearer {profile.token}"}
streamable_context = streamablehttp_client(mcp_url, headers=headers)
```

## Creating Custom Workflows

### Example: Permission Escalation Workflow

```python
class PermissionEscalationWorkflow(Workflow):
    """Test sharing permission changes."""

    def __init__(self):
        super().__init__("permission_escalation")

    async def execute(self, users: list[UserSessionWrapper]) -> WorkflowResult:
        self.start_time = time.time()

        if len(users) < 2:
            return self._finish(False, error="Requires 2+ users")

        owner, collaborator = users[0], users[1]

        # Step 1: Owner creates note
        create_result = await self._execute_step(
            "create_note",
            owner,
            lambda: owner.call_tool("nc_notes_create_note", {...})
        )

        # Step 2: Share read-only
        await self._execute_step(
            "share_readonly",
            owner,
            lambda: owner.call_tool("nc_share_create", {
                "permissions": 1  # Read-only
            })
        )

        # Step 3: Upgrade to edit permissions
        await self._execute_step(
            "upgrade_permissions",
            owner,
            lambda: owner.call_tool("nc_share_update", {
                "permissions": 15  # Read+update+create+delete
            })
        )

        # Step 4: Collaborator edits
        await self._execute_step(
            "collaborator_edit",
            collaborator,
            lambda: collaborator.call_tool("nc_notes_update_note", {...})
        )

        return self._finish(success=True)
```

### Registering Custom Workflows

```python
# In oauth_workloads.py
class MixedOAuthWorkload:
    def __init__(self, users: list[UserSessionWrapper]):
        self.users = users
        self.workflows = {
            "note_share": NoteShareWorkflow(),
            "collaborative_edit": CollaborativeEditWorkflow(),
            "file_share": FileShareAndDownloadWorkflow(),
            "permission_escalation": PermissionEscalationWorkflow(),  # Add your workflow
        }
```

## Performance Expectations

### Baseline Performance (basic auth, from existing benchmarks)
- **Throughput**: 50-200 RPS for mixed workload
- **Latency**: p50 <100ms, p95 <500ms, p99 <1000ms

### OAuth Multi-User Expectations
- **Lower throughput**: ~30-60% of baseline due to:
  - OAuth token validation overhead
  - Cross-user synchronization delays
  - Workflow coordination overhead
- **Higher p99 latency**: Due to workflow step dependencies
- **Focus**: End-to-end workflow completion time more important than raw RPS

### Common Bottlenecks
1. **OAuth token validation**: Per-request overhead
2. **Share propagation**: Time for shares to become visible to recipients
3. **Concurrent edit conflicts**: ETags and conflict resolution
4. **Permission checks**: Cross-user access validation

## Best Practices

1. **Start Small**: Begin with 2-3 users to validate workflows
2. **Monitor Errors**: Watch for permission errors and conflicts
3. **Adjust Delays**: Tune sleep delays between operations based on server response
4. **Profile Workflows**: Use step latencies to identify bottlenecks
5. **Export Results**: Always export to JSON for historical comparison

## Performance Optimizations

### Concurrent User Creation

The benchmark creates and authenticates users **concurrently** for maximum performance:

**Step 5: User Creation & OAuth Authentication**
- All N users are created in parallel using `asyncio.gather()`
- Each user runs through the full OAuth flow simultaneously
- Multiple Playwright browser contexts operate independently

**Step 6: MCP Session Creation**
- All user sessions are created concurrently
- OAuth tokens passed as Authorization headers to each session

**Performance Impact:**
- **Sequential** (old): ~10-12s per user → 40-48s for 4 users
- **Concurrent** (new): ~12-15s total for 4 users (3-4x speedup!)

Example output showing concurrent execution:
```
Step 5/6: Creating 4 users and acquiring OAuth tokens...
(Running concurrently for faster setup)

  [1/4] Creating user 'loadtest_user_1'...
  [2/4] Creating user 'loadtest_user_2'...
  [3/4] Creating user 'loadtest_user_3'...
  [4/4] Creating user 'loadtest_user_4'...
  ✓ User 'loadtest_user_4' authenticated
  ✓ User 'loadtest_user_2' authenticated
  ✓ User 'loadtest_user_1' authenticated
  ✓ User 'loadtest_user_3' authenticated

✓ Successfully created and authenticated 4 users
```

**Implementation** (oauth_benchmark.py:402-437):
```python
# Create tasks for all users
tasks = [
    create_user_task(i, browser, callback_server.auth_states)
    for i in range(num_users)
]
# Run all concurrently
results = await asyncio.gather(*tasks, return_exceptions=True)
```

## Cleanup

**Important**: Due to asyncio scoping issues with the MCP client library, automatic cleanup in the benchmark's finally block may not execute reliably. Always use the cleanup utility after running benchmarks.

### Cleanup Utility (Recommended)

Use the cleanup utility to remove test users:

```bash
# Dry run - see what would be deleted
uv run python -m tests.load.cleanup_loadtest_users --dry-run

# Delete all loadtest users
uv run python -m tests.load.cleanup_loadtest_users

# Delete users with custom prefix
uv run python -m tests.load.cleanup_loadtest_users --prefix mytest
```

### Disable Automatic Cleanup

To keep test users after the benchmark for inspection:

```bash
uv run python -m tests.load.oauth_benchmark --users 2 --no-cleanup
```

## Troubleshooting

### Leftover Test Users
**Symptom**: Test users remain in Nextcloud after benchmark crashes

**Solution**: Run the cleanup utility:
```bash
uv run python -m tests.load.cleanup_loadtest_users
```

### "User X not in pool" Error
- Ensure user count doesn't exceed configured limits
- Check that user creation succeeded in previous steps

### CancelledError During Benchmark
**Symptom**: Error message like `'CancelledError' object has no attribute 'username'` appears in logs

**Cause**: Async task cancellation during benchmark shutdown or errors can cause race conditions in error handling

**Solution**: This has been mitigated with defensive error handling. The worker now:
- Catches `asyncio.CancelledError` specifically before general exceptions
- Logs cancellation gracefully without attempting to access potentially invalid state
- Re-raises the exception to allow proper cleanup chain

If you still see this error, it's likely harmless and occurs during shutdown. The benchmark results should still be valid.

### High Error Rates
- Increase delay between operations (`await asyncio.sleep()` in worker)
- Check OAuth token validity
- Verify MCP OAuth server is running and accessible (port 8001)
- Rebuild mcp-oauth container after code changes: `docker-compose up --build -d mcp-oauth`

### Workflows Failing
- Check step-by-step latencies to identify failing steps
- Verify users have correct permissions
- Review server logs for errors

### MCP Session Creation Fails (401 Unauthorized)
**Solution**: This issue has been fixed! OAuth tokens are now properly passed as Authorization headers when creating MCP sessions.

If you still see 401 errors:
- Rebuild the mcp-oauth container: `docker-compose up --build -d mcp-oauth`
- Verify OAuth tokens are being acquired successfully in verbose mode
- Check that the token hasn't expired (use shorter test durations during troubleshooting)

## Future Enhancements

- [x] Dynamic user creation (beyond 4 default users) - **COMPLETED**
- [x] OAuth token injection for MCP sessions - **COMPLETED**
- [x] Cancel scope error handling - **COMPLETED**
- [x] Concurrent user creation and authentication - **COMPLETED** (3-4x speedup!)
- [ ] Workflow templates for common patterns
- [ ] Real-time dashboard for live monitoring
- [ ] Historical comparison and regression detection
- [ ] Load ramping (gradual user increase)
- [ ] Geographic distribution simulation (latency injection)
- [ ] Improve cleanup reliability in finally block
