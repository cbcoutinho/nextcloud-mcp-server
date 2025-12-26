"""
Enhanced metrics collection for OAuth multi-user load testing.

Extends the base BenchmarkMetrics to track per-user statistics,
workflow completion rates, and cross-user operation latencies.
"""

import statistics
import time
from collections import Counter, defaultdict
from typing import Any

from tests.load.oauth_workloads import WorkflowResult


class OAuthBenchmarkMetrics:
    """
    Enhanced metrics for OAuth multi-user load testing.

    Tracks:
    - Per-user operation counts and latencies
    - Workflow completion rates and timings
    - Cross-user operation metrics
    - Step-by-step workflow breakdowns
    """

    def __init__(self):
        # Base metrics
        self.start_time: float | None = None
        self.end_time: float | None = None

        # Per-user tracking
        self.user_operations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.user_operation_counts: dict[str, Counter] = defaultdict(Counter)
        self.user_errors: dict[str, Counter] = defaultdict(Counter)

        # Workflow tracking
        self.workflows: list[WorkflowResult] = []
        self.workflow_counts: Counter = Counter()
        self.workflow_successes: Counter = Counter()
        self.workflow_durations: dict[str, list[float]] = defaultdict(list)

        # Baseline operations (non-workflow)
        self.baseline_operations: list[dict[str, Any]] = []

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

    def add_workflow_result(self, result: WorkflowResult):
        """
        Add a workflow execution result.

        Args:
            result: WorkflowResult from workflow execution
        """
        self.workflows.append(result)
        self.workflow_counts[result.workflow_name] += 1
        if result.success:
            self.workflow_successes[result.workflow_name] += 1
        self.workflow_durations[result.workflow_name].append(result.total_duration)

        # Track per-user operations from workflow steps
        for step in result.steps:
            self.user_operation_counts[step.user][step.step_name] += 1
            if not step.success:
                self.user_errors[step.user][step.step_name] += 1

            self.user_operations[step.user].append(
                {
                    "type": "workflow_step",
                    "workflow": result.workflow_name,
                    "step": step.step_name,
                    "success": step.success,
                    "duration": step.duration,
                    "error": step.error,
                }
            )

    def add_baseline_operation(self, operation: dict[str, Any]):
        """
        Add a baseline (non-workflow) operation result.

        Args:
            operation: Dict with keys: type, operation, user, success, duration, error (optional)
        """
        self.baseline_operations.append(operation)

        user = operation.get("user", "unknown")
        op_name = operation.get("operation", "unknown")
        success = operation.get("success", False)

        self.user_operation_counts[user][op_name] += 1
        if not success:
            self.user_errors[user][op_name] += 1

        self.user_operations[user].append(operation)

    def get_user_stats(self) -> dict[str, dict[str, Any]]:
        """
        Get per-user statistics.

        Returns:
            Dict mapping username to their stats
        """
        stats = {}
        for user, operations in self.user_operations.items():
            total_ops = len(operations)
            successful_ops = sum(1 for op in operations if op.get("success", False))
            durations = [op["duration"] for op in operations if "duration" in op]

            stats[user] = {
                "total_operations": total_ops,
                "successful_operations": successful_ops,
                "failed_operations": total_ops - successful_ops,
                "success_rate": (successful_ops / total_ops * 100)
                if total_ops > 0
                else 0.0,
                "latency": self._calculate_latency_stats(durations),
                "operations_breakdown": dict(self.user_operation_counts[user]),
                "errors_breakdown": dict(self.user_errors[user]),
            }
        return stats

    def get_workflow_stats(self) -> dict[str, dict[str, Any]]:
        """
        Get workflow execution statistics.

        Returns:
            Dict mapping workflow name to its stats
        """
        stats = {}
        for workflow_name in self.workflow_counts:
            total = self.workflow_counts[workflow_name]
            successes = self.workflow_successes[workflow_name]
            durations = self.workflow_durations[workflow_name]

            # Calculate per-step latencies
            step_latencies = defaultdict(list)
            for workflow in self.workflows:
                if workflow.workflow_name == workflow_name:
                    for step in workflow.steps:
                        if step.success:
                            step_latencies[step.step_name].append(step.duration)

            step_stats = {}
            for step_name, latencies in step_latencies.items():
                if latencies:
                    step_stats[step_name] = self._calculate_latency_stats(latencies)

            stats[workflow_name] = {
                "total_executions": total,
                "successful_executions": successes,
                "failed_executions": total - successes,
                "success_rate": (successes / total * 100) if total > 0 else 0.0,
                "latency": self._calculate_latency_stats(durations),
                "step_latencies": step_stats,
            }
        return stats

    def get_baseline_stats(self) -> dict[str, Any]:
        """
        Get statistics for baseline operations.

        Returns:
            Dict with baseline operation stats
        """
        if not self.baseline_operations:
            return {
                "total_operations": 0,
                "success_rate": 0.0,
                "latency": self._calculate_latency_stats([]),
            }

        total = len(self.baseline_operations)
        successes = sum(
            1 for op in self.baseline_operations if op.get("success", False)
        )
        durations = [
            op["duration"] for op in self.baseline_operations if "duration" in op
        ]

        # Per-operation breakdown
        operation_counts = Counter()
        operation_errors = Counter()
        for op in self.baseline_operations:
            op_name = op.get("operation", "unknown")
            operation_counts[op_name] += 1
            if not op.get("success", False):
                operation_errors[op_name] += 1

        return {
            "total_operations": total,
            "successful_operations": successes,
            "failed_operations": total - successes,
            "success_rate": (successes / total * 100) if total > 0 else 0.0,
            "latency": self._calculate_latency_stats(durations),
            "operations_breakdown": dict(operation_counts),
            "errors_breakdown": dict(operation_errors),
        }

    def _calculate_latency_stats(self, durations: list[float]) -> dict[str, float]:
        """Calculate latency statistics from a list of durations."""
        if not durations:
            return {
                "min": 0.0,
                "max": 0.0,
                "mean": 0.0,
                "median": 0.0,
                "p90": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            }

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

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary for JSON export."""
        return {
            "summary": {
                "duration": self.duration,
                "total_workflows": len(self.workflows),
                "total_baseline_ops": len(self.baseline_operations),
                "total_users": len(self.user_operations),
            },
            "workflows": self.get_workflow_stats(),
            "baseline": self.get_baseline_stats(),
            "users": self.get_user_stats(),
        }

    def print_report(self):
        """Print human-readable benchmark report."""
        print("\n" + "=" * 80)
        print("OAUTH MULTI-USER BENCHMARK RESULTS")
        print("=" * 80)

        # Summary
        print(f"\nDuration: {self.duration:.2f}s")
        print(f"Total Users: {len(self.user_operations)}")
        print(f"Total Workflows Executed: {len(self.workflows)}")
        print(f"Total Baseline Operations: {len(self.baseline_operations)}")

        # Workflow Stats
        if self.workflows:
            print("\n" + "-" * 80)
            print("WORKFLOW STATISTICS")
            print("-" * 80)
            print(
                f"{'Workflow':<30} {'Total':>8} {'Success':>8} {'Rate':>8} {'P50':>10} {'P95':>10}"
            )
            print("-" * 80)

            workflow_stats = self.get_workflow_stats()
            for name, stats in sorted(workflow_stats.items()):
                latency = stats["latency"]
                print(
                    f"{name:<30} {stats['total_executions']:>8} "
                    f"{stats['successful_executions']:>8} "
                    f"{stats['success_rate']:>7.1f}% "
                    f"{latency['median']:>9.4f}s {latency['p95']:>9.4f}s"
                )

        # Per-User Stats
        print("\n" + "-" * 80)
        print("PER-USER STATISTICS")
        print("-" * 80)
        print(
            f"{'User':<20} {'Total Ops':>10} {'Success':>10} {'Errors':>8} {'Rate':>8} {'P50':>10}"
        )
        print("-" * 80)

        user_stats = self.get_user_stats()
        for username, stats in sorted(user_stats.items()):
            latency = stats["latency"]
            print(
                f"{username:<20} {stats['total_operations']:>10} "
                f"{stats['successful_operations']:>10} "
                f"{stats['failed_operations']:>8} "
                f"{stats['success_rate']:>7.1f}% "
                f"{latency['median']:>9.4f}s"
            )

        # Baseline Stats
        if self.baseline_operations:
            print("\n" + "-" * 80)
            print("BASELINE OPERATIONS")
            print("-" * 80)
            baseline = self.get_baseline_stats()
            print(f"Total Operations: {baseline['total_operations']}")
            print(f"Success Rate: {baseline['success_rate']:.1f}%")
            latency = baseline["latency"]
            print(
                f"Latency: min={latency['min']:.4f}s, p50={latency['median']:.4f}s, "
                f"p95={latency['p95']:.4f}s, max={latency['max']:.4f}s"
            )

        print("=" * 80 + "\n")
