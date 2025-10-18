#!/usr/bin/env python3
"""
Load testing benchmark for Nextcloud MCP Server.

Usage:
    uv run python -m tests.load.benchmark --concurrency 10 --duration 30
    uv run python -m tests.load.benchmark -c 50 -d 300 --output results.json
"""

import asyncio
import json
import logging
import signal
import statistics
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any

import anyio
import click
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.load.workloads import MixedWorkload, OperationResult, WorkloadOperations

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s [%(asctime)s] %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)


class BenchmarkMetrics:
    """Collect and analyze benchmark metrics."""

    def __init__(self):
        self.results: list[OperationResult] = []
        self.start_time: float | None = None
        self.end_time: float | None = None
        self._operation_counts: Counter = Counter()
        self._operation_errors: Counter = Counter()

    def add_result(self, result: OperationResult):
        """Add a single operation result."""
        self.results.append(result)
        self._operation_counts[result.operation] += 1
        if not result.success:
            self._operation_errors[result.operation] += 1

    def start(self):
        """Mark the start of the benchmark."""
        self.start_time = time.time()

    def stop(self):
        """Mark the end of the benchmark."""
        self.end_time = time.time()

    @property
    def duration(self) -> float:
        """Total benchmark duration in seconds."""
        if self.start_time is None or self.end_time is None:
            return 0.0
        return self.end_time - self.start_time

    @property
    def total_requests(self) -> int:
        """Total number of requests made."""
        return len(self.results)

    @property
    def successful_requests(self) -> int:
        """Number of successful requests."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed_requests(self) -> int:
        """Number of failed requests."""
        return sum(1 for r in self.results if not r.success)

    @property
    def error_rate(self) -> float:
        """Error rate as a percentage."""
        if self.total_requests == 0:
            return 0.0
        return (self.failed_requests / self.total_requests) * 100

    @property
    def requests_per_second(self) -> float:
        """Average requests per second."""
        if self.duration == 0:
            return 0.0
        return self.total_requests / self.duration

    def latency_stats(self) -> dict[str, float]:
        """Calculate latency statistics."""
        if not self.results:
            return {
                "min": 0.0,
                "max": 0.0,
                "mean": 0.0,
                "median": 0.0,
                "p90": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            }

        durations = [r.duration for r in self.results]
        sorted_durations = sorted(durations)

        def percentile(data: list[float], p: float) -> float:
            k = (len(data) - 1) * p
            f = int(k)
            c = f + 1
            if c >= len(data):
                return data[-1]
            return data[f] + (k - f) * (data[c] - data[f])

        return {
            "min": min(durations),
            "max": max(durations),
            "mean": statistics.mean(durations),
            "median": statistics.median(durations),
            "p90": percentile(sorted_durations, 0.90),
            "p95": percentile(sorted_durations, 0.95),
            "p99": percentile(sorted_durations, 0.99),
        }

    def operation_breakdown(self) -> dict[str, dict[str, Any]]:
        """Get per-operation statistics."""
        breakdown = {}
        for op_name in self._operation_counts:
            op_results = [r for r in self.results if r.operation == op_name]
            op_durations = [r.duration for r in op_results if r.success]

            if op_durations:
                sorted_durations = sorted(op_durations)
                p50 = statistics.median(sorted_durations)
                p95_idx = int(len(sorted_durations) * 0.95)
                p95 = sorted_durations[min(p95_idx, len(sorted_durations) - 1)]
            else:
                p50 = p95 = 0.0

            breakdown[op_name] = {
                "count": self._operation_counts[op_name],
                "errors": self._operation_errors[op_name],
                "success_rate": (
                    (self._operation_counts[op_name] - self._operation_errors[op_name])
                    / self._operation_counts[op_name]
                    * 100
                ),
                "p50_latency": p50,
                "p95_latency": p95,
            }

        return breakdown

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary for JSON export."""
        return {
            "summary": {
                "duration": self.duration,
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "error_rate": self.error_rate,
                "requests_per_second": self.requests_per_second,
            },
            "latency": self.latency_stats(),
            "operations": self.operation_breakdown(),
        }

    def print_report(self):
        """Print human-readable benchmark report."""
        print("\n" + "=" * 80)
        print("BENCHMARK RESULTS")
        print("=" * 80)

        print(f"\nDuration: {self.duration:.2f}s")
        print(f"Total Requests: {self.total_requests}")
        print(f"Successful: {self.successful_requests}")
        print(f"Failed: {self.failed_requests}")
        print(f"Error Rate: {self.error_rate:.2f}%")
        print(f"Requests/Second: {self.requests_per_second:.2f}")

        print("\n" + "-" * 80)
        print("LATENCY (seconds)")
        print("-" * 80)
        latency = self.latency_stats()
        print(f"Min:    {latency['min']:.4f}s")
        print(f"Mean:   {latency['mean']:.4f}s")
        print(f"Median: {latency['median']:.4f}s")
        print(f"P90:    {latency['p90']:.4f}s")
        print(f"P95:    {latency['p95']:.4f}s")
        print(f"P99:    {latency['p99']:.4f}s")
        print(f"Max:    {latency['max']:.4f}s")

        print("\n" + "-" * 80)
        print("OPERATION BREAKDOWN")
        print("-" * 80)
        print(
            f"{'Operation':<25} {'Count':>8} {'Errors':>8} {'Success':>9} {'P50':>10} {'P95':>10}"
        )
        print("-" * 80)

        breakdown = self.operation_breakdown()
        for op_name, stats in sorted(breakdown.items()):
            print(
                f"{op_name:<25} {stats['count']:>8} {stats['errors']:>8} "
                f"{stats['success_rate']:>8.1f}% {stats['p50_latency']:>9.4f}s {stats['p95_latency']:>9.4f}s"
            )

        print("=" * 80 + "\n")


@asynccontextmanager
async def create_mcp_session(url: str):
    """Create an MCP client session with proper cleanup."""
    logger.info(f"Creating MCP client session for {url}")
    streamable_context = streamablehttp_client(url)
    session_context = None

    try:
        read_stream, write_stream, _ = await streamable_context.__aenter__()
        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()
        await session.initialize()
        logger.info("MCP client session initialized")
        yield session
    finally:
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing session: {e}")

        try:
            await streamable_context.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"Error closing streamable context: {e}")


async def wait_for_mcp_server(url: str, max_attempts: int = 10) -> bool:
    """Wait for MCP server to be ready."""
    logger.info(f"Waiting for MCP server at {url}...")

    for attempt in range(1, max_attempts + 1):
        try:
            async with create_mcp_session(url) as session:
                # Try to get capabilities
                await session.read_resource("nc://capabilities")
                logger.info("MCP server is ready")
                return True
        except Exception as e:
            if attempt < max_attempts:
                logger.debug(f"Attempt {attempt}/{max_attempts}: {e}")
                await asyncio.sleep(2)
            else:
                logger.error(f"MCP server not ready after {max_attempts} attempts")
                return False

    return False


async def benchmark_worker(
    worker_id: int,
    url: str,
    duration: float,
    metrics: BenchmarkMetrics,
    stop_event: asyncio.Event,
):
    """Single worker that runs operations for the specified duration."""
    logger.info(f"Worker {worker_id} starting...")

    try:
        async with create_mcp_session(url) as session:
            ops = WorkloadOperations(session)
            workload = MixedWorkload(ops)

            # Warmup
            await workload.warmup(count=5)

            # Run operations until duration expires or stop event is set
            start_time = time.time()
            operation_count = 0

            while not stop_event.is_set():
                if time.time() - start_time >= duration:
                    break

                result = await workload.run_operation()
                metrics.add_result(result)
                operation_count += 1

                # Small delay to prevent overwhelming the server
                await asyncio.sleep(0.01)

            # Cleanup
            await ops.cleanup()

            logger.info(f"Worker {worker_id} completed {operation_count} operations")

    except Exception as e:
        logger.error(f"Worker {worker_id} error: {e}", exc_info=True)


async def run_benchmark(
    url: str,
    concurrency: int,
    duration: float,
    warmup: float = 5.0,
) -> BenchmarkMetrics:
    """Run the benchmark with specified parameters."""
    metrics = BenchmarkMetrics()
    stop_event = asyncio.Event()

    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        logger.warning("Received interrupt signal, stopping benchmark...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(
        f"\nStarting benchmark with {concurrency} concurrent workers for {duration}s..."
    )
    print(f"Target: {url}")
    print(f"Warmup period: {warmup}s\n")

    # Warmup period
    if warmup > 0:
        print("Warming up...")
        await asyncio.sleep(warmup)

    # Start metrics collection
    metrics.start()

    # Create and run workers
    workers = [
        benchmark_worker(i, url, duration, metrics, stop_event)
        for i in range(concurrency)
    ]

    # Show progress
    progress_task = asyncio.create_task(show_progress(duration, metrics, stop_event))

    # Wait for all workers to complete
    await asyncio.gather(*workers, return_exceptions=True)

    # Stop metrics and progress
    metrics.stop()
    stop_event.set()
    await progress_task

    return metrics


async def show_progress(
    duration: float,
    metrics: BenchmarkMetrics,
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
        rps = metrics.total_requests / max(elapsed, 0.1)

        # Print progress bar
        bar_length = 40
        filled = int(bar_length * progress / 100)
        bar = "█" * filled + "░" * (bar_length - filled)

        print(
            f"\r[{bar}] {progress:5.1f}% | "
            f"Requests: {metrics.total_requests:6d} | "
            f"RPS: {rps:6.1f} | "
            f"Errors: {metrics.failed_requests:4d}",
            end="",
            flush=True,
        )

        await asyncio.sleep(0.5)

    print()  # New line after progress


@click.command()
@click.option(
    "--concurrency",
    "-c",
    type=int,
    default=10,
    show_default=True,
    help="Number of concurrent workers",
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
    "-u",
    default="http://127.0.0.1:8000/mcp",
    show_default=True,
    help="MCP server URL",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file for JSON results (optional)",
)
@click.option(
    "--wait-for-server/--no-wait",
    default=True,
    show_default=True,
    help="Wait for MCP server to be ready before starting",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
)
def main(
    concurrency: int,
    duration: float,
    warmup: float,
    url: str,
    output: str | None,
    wait_for_server: bool,
    verbose: bool,
):
    """
    Load testing benchmark for Nextcloud MCP Server.

    Runs a mixed workload of realistic MCP operations against the server
    and reports detailed performance metrics.

    Examples:

        # Quick 30-second test with 10 workers
        uv run python -m tests.load.benchmark --concurrency 10 --duration 30

        # Extended test with 50 workers for 5 minutes
        uv run python -m tests.load.benchmark -c 50 -d 300

        # Export results to JSON
        uv run python -m tests.load.benchmark -c 20 -d 60 --output results.json

        # Test OAuth server on port 8001
        uv run python -m tests.load.benchmark --url http://127.0.0.1:8001/mcp
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("tests.load").setLevel(logging.DEBUG)

    async def run():
        # Wait for server if requested
        if wait_for_server:
            if not await wait_for_mcp_server(url):
                print("ERROR: MCP server is not ready", file=sys.stderr)
                sys.exit(1)

        # Run benchmark
        metrics = await run_benchmark(url, concurrency, duration, warmup)

        # Print report
        metrics.print_report()

        # Export to JSON if requested
        if output:
            with open(output, "w") as f:
                json.dump(metrics.to_dict(), f, indent=2)
            print(f"Results exported to: {output}")

    try:
        anyio.run(run)
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
