#!/usr/bin/env python3
"""
OAuth Multi-User Load Testing for Nextcloud MCP Server.

Simulates realistic multi-user scenarios with coordinated workflows
like note sharing, collaborative editing, and file operations.

Usage:
    uv run python -m tests.load.oauth_benchmark --users 4 --duration 60
    uv run python -m tests.load.oauth_benchmark -u 10 -d 300 --workload sharing
"""

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import click
import httpx
from playwright.async_api import async_playwright

from nextcloud_mcp_server.auth.client_registration import load_or_register_client
from nextcloud_mcp_server.client import NextcloudClient
from tests.load.oauth_metrics import OAuthBenchmarkMetrics
from tests.load.oauth_pool import (
    OAuthUserPool,
    UserSessionWrapper,
    generate_secure_password,
)
from tests.load.oauth_workloads import MixedOAuthWorkload, WorkflowResult

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s [%(asctime)s] %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)


class OAuthCallbackServer:
    """
    Temporary HTTP server to capture OAuth authorization codes.

    Runs in a background thread, captures auth codes via state parameter
    correlation, and stores them in a shared dictionary.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8081):
        self.host = host
        self.port = port
        self.auth_states: dict[str, str] = {}
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        """Start the callback server in a background thread."""

        class CallbackHandler(BaseHTTPRequestHandler):
            auth_states = self.auth_states

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/callback":
                    params = parse_qs(parsed.query)
                    code = params.get("code", [None])[0]
                    state = params.get("state", [None])[0]

                    if code and state:
                        self.auth_states[state] = code
                        logger.info(f"Captured auth code for state {state[:16]}...")

                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h1>Authorization successful!</h1>"
                        b"<p>You can close this window.</p></body></html>"
                    )
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                # Suppress default logging
                pass

        self.server = HTTPServer((self.host, self.port), CallbackHandler)

        def run():
            logger.info(f"OAuth callback server listening on {self.host}:{self.port}")
            self.server.serve_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        logger.info("OAuth callback server started")

    def stop(self):
        """Stop the callback server."""
        if self.server:
            self.server.shutdown()
            logger.info("OAuth callback server stopped")

    def get_auth_code(self, state: str) -> str | None:
        """Get auth code for a given state parameter."""
        return self.auth_states.get(state)


async def discover_oidc_endpoints(nextcloud_host: str) -> dict[str, str]:
    """
    Discover OIDC endpoints from Nextcloud's .well-known configuration.

    Args:
        nextcloud_host: Nextcloud host URL (e.g., http://localhost:8080)

    Returns:
        Dict with authorization_endpoint, token_endpoint, and registration_endpoint
    """
    logger.info("Discovering OIDC endpoints...")
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        response = await client.get(
            f"{nextcloud_host}/.well-known/openid-configuration"
        )
        response.raise_for_status()
        config = response.json()

    endpoints = {
        "authorization_endpoint": config["authorization_endpoint"],
        "token_endpoint": config["token_endpoint"],
        "registration_endpoint": config["registration_endpoint"],
    }
    logger.info(f"Discovered endpoints: {endpoints}")
    return endpoints


async def setup_oauth_client(
    nextcloud_host: str, callback_url: str, registration_endpoint: str
) -> dict[str, str]:
    """
    Setup OAuth client using load_or_register_client.

    Args:
        nextcloud_host: Nextcloud host URL
        callback_url: OAuth callback URL
        registration_endpoint: OAuth registration endpoint URL

    Returns:
        Dict with client_id and client_secret
    """
    logger.info("Setting up OAuth client...")

    # Use the client registration utility
    client_info = await load_or_register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        storage_path=".nextcloud_oauth_benchmark_client.json",
        client_name="OAuth Benchmark Test Client",
        redirect_uris=[callback_url],
    )

    logger.info(f"OAuth client setup complete (client_id: {client_info.client_id})")
    return {
        "client_id": client_info.client_id,
        "client_secret": client_info.client_secret,
    }


async def create_and_authenticate_user(
    user_pool: OAuthUserPool,
    browser: Any,
    auth_states: dict[str, str],
    username: str,
    password: str,
    display_name: str | None = None,
) -> str:
    """
    Create Nextcloud user and acquire OAuth token via Playwright.

    Args:
        user_pool: OAuthUserPool instance
        browser: Playwright browser instance
        auth_states: Shared auth_states dict for callback server
        username: Username to create
        password: Password for the user
        display_name: Optional display name

    Returns:
        OAuth access token for the user
    """
    logger.info(f"Creating and authenticating user: {username}")

    # Create Nextcloud user
    await user_pool.create_nextcloud_user(
        username=username,
        password=password,
        display_name=display_name or username,
    )

    # Generate unique state for this OAuth flow
    state = secrets.token_urlsafe(32)

    # Acquire OAuth token via Playwright
    token = await user_pool.acquire_token_playwright(
        browser=browser,
        username=username,
        password=password,
        state=state,
        auth_states=auth_states,
    )

    logger.info(f"Successfully authenticated user: {username}")
    return token


async def oauth_benchmark_worker(
    user_wrapper: UserSessionWrapper,
    workload: MixedOAuthWorkload,
    duration: float,
    metrics: OAuthBenchmarkMetrics,
    stop_event: asyncio.Event,
):
    """
    Single worker executing operations for one user.

    Args:
        user_wrapper: UserSessionWrapper for this worker
        workload: MixedOAuthWorkload instance
        duration: Test duration in seconds
        metrics: Metrics collector
        stop_event: Event to signal stop
    """
    logger.info(f"Worker for {user_wrapper.username} starting...")

    start_time = time.time()
    operation_count = 0

    try:
        while not stop_event.is_set():
            if time.time() - start_time >= duration:
                break

            # Run an operation (might be baseline or workflow)
            result = await workload.run_operation()

            # Record metrics
            if isinstance(result, WorkflowResult):
                metrics.add_workflow_result(result)
            else:
                # Baseline operation
                metrics.add_baseline_operation(result)

            operation_count += 1

            # Small delay to prevent overwhelming the server
            await asyncio.sleep(0.05)

        logger.info(
            f"Worker for {user_wrapper.username} completed {operation_count} operations"
        )

    except asyncio.CancelledError:
        # Handle task cancellation gracefully (e.g., during benchmark shutdown)
        logger.info(
            f"Worker for {user_wrapper.username} was cancelled "
            f"(completed {operation_count} operations)"
        )
        raise  # Re-raise to allow proper cleanup
    except Exception as e:
        logger.error(f"Worker {user_wrapper.username} error: {e}", exc_info=True)


async def show_progress(
    duration: float,
    metrics: OAuthBenchmarkMetrics,
    stop_event: asyncio.Event,
):
    """Show real-time progress during benchmark."""
    start_time = time.time()

    while not stop_event.is_set():
        elapsed = time.time() - start_time
        if elapsed >= duration:
            break

        # Calculate progress
        progress = min(elapsed / duration * 100, 100)
        total_ops = len(metrics.baseline_operations) + len(metrics.workflows)
        workflows = len(metrics.workflows)

        # Print progress bar
        bar_length = 40
        filled = int(bar_length * progress / 100)
        bar = "█" * filled + "░" * (bar_length - filled)

        print(
            f"\r[{bar}] {progress:5.1f}% | "
            f"Total Ops: {total_ops:6d} | "
            f"Workflows: {workflows:4d}",
            end="",
            flush=True,
        )

        await asyncio.sleep(0.5)

    print()  # New line after progress


async def run_oauth_benchmark(
    num_users: int,
    duration: float,
    mcp_url: str,
    warmup: float = 5.0,
    user_prefix: str = "loadtest",
    cleanup: bool = True,
    browser_type: str = "firefox",
    headed: bool = False,
) -> OAuthBenchmarkMetrics:
    """
    Run the OAuth multi-user benchmark with dynamic user creation.

    Args:
        num_users: Number of concurrent users to create
        duration: Test duration in seconds
        mcp_url: MCP server URL
        warmup: Warmup period in seconds
        user_prefix: Prefix for generated usernames
        cleanup: Whether to delete users after benchmark
        browser_type: Playwright browser type (firefox, chromium, webkit)
        headed: Whether to run browser in headed mode

    Returns:
        OAuthBenchmarkMetrics with results
    """
    metrics = OAuthBenchmarkMetrics()
    stop_event = asyncio.Event()
    created_users: list[str] = []
    callback_server: OAuthCallbackServer | None = None
    user_pool: OAuthUserPool | None = None
    admin_client: NextcloudClient | None = None

    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        logger.warning("Received interrupt signal, stopping benchmark...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"\n{'=' * 80}")
    print("OAUTH MULTI-USER BENCHMARK")
    print(f"{'=' * 80}")
    print(f"Users: {num_users} | Duration: {duration}s | Warmup: {warmup}s")
    print(f"Target: {mcp_url}")
    print(f"User Prefix: {user_prefix} | Cleanup: {cleanup}")
    print(f"Browser: {browser_type} | Headed: {headed}")
    print(f"{'=' * 80}\n")

    try:
        # Get environment variables
        nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
        callback_url = "http://127.0.0.1:8081/callback"

        # Step 1: Start OAuth callback server
        print("Step 1/6: Starting OAuth callback server...")
        callback_server = OAuthCallbackServer(host="127.0.0.1", port=8081)
        callback_server.start()
        print("✓ Callback server listening on http://127.0.0.1:8081\n")

        # Step 2: Discover OIDC endpoints
        print("Step 2/6: Discovering OIDC endpoints...")
        endpoints = await discover_oidc_endpoints(nextcloud_host)
        print(f"✓ Authorization endpoint: {endpoints['authorization_endpoint']}")
        print(f"✓ Token endpoint: {endpoints['token_endpoint']}")
        print(f"✓ Registration endpoint: {endpoints['registration_endpoint']}\n")

        # Step 3: Setup OAuth client
        print("Step 3/6: Setting up OAuth client...")
        oauth_credentials = await setup_oauth_client(
            nextcloud_host, callback_url, endpoints["registration_endpoint"]
        )
        print(f"✓ OAuth client registered (ID: {oauth_credentials['client_id']})\n")

        # Step 4: Create admin client and user pool
        print("Step 4/6: Initializing admin client and user pool...")
        admin_client = NextcloudClient.from_env()
        user_pool = OAuthUserPool(
            admin_client=admin_client,
            client_id=oauth_credentials["client_id"],
            client_secret=oauth_credentials["client_secret"],
            callback_url=callback_url,
            token_endpoint=endpoints["token_endpoint"],
            authorization_endpoint=endpoints["authorization_endpoint"],
        )

        async with user_pool:
            print("✓ User pool initialized\n")

            # Step 5: Create users and acquire OAuth tokens (concurrently)
            print(f"Step 5/6: Creating {num_users} users and acquiring OAuth tokens...")
            print("(Running concurrently for faster setup)\n")

            async def create_user_task(
                i: int, browser, auth_states: dict
            ) -> tuple[str, str, str] | None:
                """Create and authenticate a single user. Returns (username, password, token) or None on failure."""
                username = f"{user_prefix}_user_{i + 1}"
                password = generate_secure_password(16)

                print(f"  [{i + 1}/{num_users}] Creating user '{username}'...")

                try:
                    token = await create_and_authenticate_user(
                        user_pool=user_pool,
                        browser=browser,
                        auth_states=auth_states,
                        username=username,
                        password=password,
                        display_name=f"Load Test User {i + 1}",
                    )

                    print(f"  ✓ User '{username}' authenticated\n")
                    return (username, password, token)

                except Exception as e:
                    logger.error(f"Failed to create/authenticate user {username}: {e}")
                    return None

            async with async_playwright() as p:
                # Launch browser
                browser_launcher = getattr(p, browser_type)
                browser = await browser_launcher.launch(headless=not headed)

                try:
                    # Create all users concurrently
                    tasks = [
                        create_user_task(i, browser, callback_server.auth_states)
                        for i in range(num_users)
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Process results
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error(f"User creation task failed: {result}")
                            continue
                        if result is None:
                            continue

                        username, password, token = result
                        await user_pool.add_user(
                            username=username, password=password, token=token
                        )
                        created_users.append(username)

                finally:
                    await browser.close()

            if not created_users:
                raise RuntimeError("Failed to create any users")

            print(
                f"✓ Successfully created and authenticated {len(created_users)} users\n"
            )

        # Step 6: Create MCP sessions for each user (concurrently)
        print("Step 6/6: Creating MCP sessions for users...")
        user_wrappers = []
        async with user_pool:

            async def create_session_task(username: str) -> UserSessionWrapper | None:
                """Create MCP session for a user. Returns wrapper or None on failure."""
                try:
                    session = await user_pool.create_user_session(username, mcp_url)
                    wrapper = UserSessionWrapper(username, session, user_pool)
                    print(f"  ✓ Session created for '{username}'")
                    return wrapper
                except Exception as e:
                    logger.error(f"Failed to create session for {username}: {e}")
                    return None

            # Create all sessions concurrently
            session_tasks = [
                create_session_task(username) for username in created_users
            ]
            session_results = await asyncio.gather(
                *session_tasks, return_exceptions=True
            )

            # Process results
            for result in session_results:
                if isinstance(result, Exception):
                    logger.error(f"Session creation task failed: {result}")
                    continue
                if result is not None:
                    user_wrappers.append(result)

            if not user_wrappers:
                raise RuntimeError("Failed to create any user sessions")

            print(f"✓ Created {len(user_wrappers)} MCP sessions\n")

            # Warmup period
            if warmup > 0:
                print(f"Warmup period: {warmup}s...")
                await asyncio.sleep(warmup)
                print()

            # Start benchmark
            print(f"{'=' * 80}")
            print("STARTING BENCHMARK")
            print(f"{'=' * 80}\n")

            metrics.start()

            # Create workload and workers
            workload = MixedOAuthWorkload(user_wrappers)
            workers = [
                oauth_benchmark_worker(wrapper, workload, duration, metrics, stop_event)
                for wrapper in user_wrappers
            ]

            # Run workers with progress display
            progress_task = asyncio.create_task(
                show_progress(duration, metrics, stop_event)
            )
            await asyncio.gather(*workers, return_exceptions=True)
            stop_event.set()
            await progress_task

            metrics.stop()

            print(f"\n{'=' * 80}")
            print("BENCHMARK COMPLETE")
            print(f"{'=' * 80}\n")

            # Cleanup user sessions
            print("Closing user sessions...")
            await user_pool.close_all_sessions()
            print("✓ All sessions closed\n")

    except Exception as e:
        logger.error(f"Benchmark error: {e}", exc_info=True)
        # Don't re-raise here - we want cleanup to run

    finally:
        # Cleanup callback server
        if callback_server:
            try:
                callback_server.stop()
                logger.info("OAuth callback server stopped")
            except Exception as e:
                logger.warning(f"Error stopping callback server: {e}")

        # Cleanup test users
        if cleanup and created_users:
            print(f"\nCleaning up {len(created_users)} test users...")
            # Create a new admin client for cleanup (don't rely on the existing one)
            try:
                cleanup_client = NextcloudClient.from_env()
                for username in created_users:
                    try:
                        await cleanup_client.users.delete_user(userid=username)
                        print(f"  ✓ Deleted user '{username}'")
                    except Exception as e:
                        logger.warning(f"Failed to delete user {username}: {e}")
                print("✓ Cleanup complete\n")
            except Exception as e:
                logger.error(f"Error during user cleanup: {e}")
                print(
                    "⚠️  Failed to cleanup users. Please run cleanup script manually.\n"
                )
        elif created_users:
            print(
                f"\n⚠️  {len(created_users)} test users were NOT deleted (cleanup=False)"
            )
            print(f"Users: {', '.join(created_users)}\n")

    return metrics


@click.command()
@click.option(
    "--users",
    "-u",
    type=int,
    default=2,
    show_default=True,
    help="Number of concurrent users to create dynamically",
)
@click.option(
    "--duration",
    "-d",
    type=float,
    default=30.0,
    show_default=True,
    help="Test duration in seconds",
)
@click.option(
    "--warmup",
    "-w",
    type=float,
    default=5.0,
    show_default=True,
    help="Warmup duration before collecting metrics (seconds)",
)
@click.option(
    "--url",
    default="http://127.0.0.1:8001/mcp",
    show_default=True,
    help="MCP OAuth server URL",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file for JSON results (optional)",
)
@click.option(
    "--workload",
    type=click.Choice(["mixed", "sharing", "collaboration", "baseline"]),
    default="mixed",
    show_default=True,
    help="Workload type to execute",
)
@click.option(
    "--user-prefix",
    default="loadtest",
    show_default=True,
    help="Prefix for dynamically created usernames",
)
@click.option(
    "--cleanup/--no-cleanup",
    default=True,
    show_default=True,
    help="Delete created users after benchmark",
)
@click.option(
    "--browser",
    type=click.Choice(["firefox", "chromium", "webkit"]),
    default="firefox",
    show_default=True,
    help="Playwright browser type for OAuth automation",
)
@click.option(
    "--headed",
    is_flag=True,
    help="Run browser in headed mode (visible window, useful for debugging)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
)
def main(
    users: int,
    duration: float,
    warmup: float,
    url: str,
    output: str | None,
    workload: str,
    user_prefix: str,
    cleanup: bool,
    browser: str,
    headed: bool,
    verbose: bool,
):
    """
    OAuth Multi-User Load Testing for Nextcloud MCP Server.

    Dynamically creates N users, authenticates them via OAuth using Playwright
    browser automation, and simulates realistic multi-user scenarios with
    coordinated workflows like note sharing, collaborative editing, and file operations.

    Examples:

        # 2 users, 30-second test (default settings)
        uv run python -m tests.load.oauth_benchmark

        # 4 users, 60-second test with mixed workload
        uv run python -m tests.load.oauth_benchmark --users 4 --duration 60

        # 10 users, 5-minute sharing-focused test
        uv run python -m tests.load.oauth_benchmark -u 10 -d 300 --workload sharing

        # Export results to JSON
        uv run python -m tests.load.oauth_benchmark -u 5 -d 120 --output results.json

        # Custom user prefix and keep users after benchmark
        uv run python -m tests.load.oauth_benchmark -u 3 --user-prefix mytest --no-cleanup

        # Debug with visible browser (headed mode)
        uv run python -m tests.load.oauth_benchmark -u 2 -d 10 --headed --verbose

    Requirements:
        - docker-compose up (mcp-oauth container running on port 8001)
        - NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD env vars set
        - Playwright browser installed: uv run playwright install firefox
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("tests.load").setLevel(logging.DEBUG)

    async def run():
        # Run benchmark
        metrics = await run_oauth_benchmark(
            num_users=users,
            duration=duration,
            mcp_url=url,
            warmup=warmup,
            user_prefix=user_prefix,
            cleanup=cleanup,
            browser_type=browser,
            headed=headed,
        )

        # Print report
        metrics.print_report()

        # Export to JSON if requested
        if output:
            with open(output, "w") as f:
                json.dump(metrics.to_dict(), f, indent=2)
            print(f"Results exported to: {output}")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if verbose:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()
