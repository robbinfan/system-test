"""
Evaluation result types.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"  # infrastructure error, not test logic
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class TestResult:
    """Result of a single test case execution."""
    test_fqn: str
    test_method: str
    status: TestStatus = TestStatus.PENDING
    duration_seconds: float = 0.0
    error_message: str = ""
    stdout: str = ""
    stderr: str = ""
    pod_name: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    retries: int = 0

    def mark_running(self):
        self.status = TestStatus.RUNNING
        self.started_at = time.time()

    def mark_passed(self):
        self.status = TestStatus.PASSED
        self.finished_at = time.time()
        self.duration_seconds = self.finished_at - self.started_at

    def mark_failed(self, message: str, stdout: str = "", stderr: str = ""):
        self.status = TestStatus.FAILED
        self.error_message = message
        self.stdout = stdout
        self.stderr = stderr
        self.finished_at = time.time()
        self.duration_seconds = self.finished_at - self.started_at

    def mark_error(self, message: str):
        self.status = TestStatus.ERROR
        self.error_message = message
        self.finished_at = time.time()
        self.duration_seconds = self.finished_at - self.started_at


@dataclass
class EvaluationReport:
    """Aggregated report of all test executions for an evaluation run."""
    plan_summary: str = ""
    results: list[TestResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    total_pods_launched: int = 0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.FAILED)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.ERROR)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def duration_seconds(self) -> float:
        return self.finished_at - self.started_at if self.finished_at else 0.0

    def summary(self) -> str:
        return (
            f"Evaluation: {self.plan_summary}\n"
            f"  Total: {self.total} | "
            f"Passed: {self.passed} | "
            f"Failed: {self.failed} | "
            f"Errors: {self.errors} | "
            f"Pass rate: {self.pass_rate:.1%}\n"
            f"  Duration: {self.duration_seconds:.1f}s | "
            f"Pods launched: {self.total_pods_launched}"
        )

    def failed_results(self) -> list[TestResult]:
        return [r for r in self.results if r.status in (TestStatus.FAILED, TestStatus.ERROR)]
