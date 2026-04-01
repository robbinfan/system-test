"""
Orchestrator - the main pipeline that drives the evaluation process.

Pipeline:
    1. Receive a Plan (from LLM or manual input)
    2. Build/load test index
    3. Select existing cases + identify cases to generate
    4. Generate new cases (template or LLM)
    5. Create actors for all selected cases
    6. Launch actors via Supervisor (backed by K8s Pods)
    7. Collect results and produce EvaluationReport
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .actors.actor import Message
from .actors.supervisor import ActorSpec, RestartStrategy, Supervisor
from .actors.test_runner_actor import TestRunnerActor
from .core.plan import Plan
from .core.result import EvaluationReport, TestResult, TestStatus
from .core.test_case_index import TestCaseEntry, TestIndex
from .generator.case_generator import CaseGenerator, GeneratedCase
from .k8s.sandbox import SandboxConfig, SandboxManager
from .scanner.ruby_scanner import scan_tests_directory
from .selector.case_selector import CaseSelector, SelectionResult

logger = logging.getLogger(__name__)


@dataclass
class EvaluatorConfig:
    """Configuration for the evaluator pipeline."""
    # Path to system-test/tests directory
    tests_dir: str = "tests"
    # Path to cached test index (built if not exists)
    index_path: str = "test_index.json"
    # K8s sandbox config
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    # Max concurrent test pods
    max_concurrency: int = 10
    # Max test cases to select
    max_cases: int = 50
    # Max restarts per actor on infrastructure failure
    max_restarts: int = 2
    # Vespa hosts for test execution
    vespa_hosts: list[str] = field(default_factory=list)
    # LLM client for case generation (optional)
    llm_client: object = None
    # Directory for generated test files
    generated_dir: str = "/tmp/vespa-eval-generated"


class Evaluator:
    """
    Main entry point for the Vespa Evaluator.

    Usage:
        config = EvaluatorConfig(
            tests_dir="/path/to/system-test/tests",
            sandbox=SandboxConfig(
                namespace="vespa-eval",
                vespa_config_server="configserver.vespa.svc:19071",
            ),
            vespa_hosts=["node-0.vespa.svc", "node-1.vespa.svc"],
            max_concurrency=5,
        )

        evaluator = Evaluator(config)
        report = await evaluator.evaluate(plan)
        print(report.summary())
    """

    def __init__(self, config: EvaluatorConfig):
        self.config = config
        self.index: Optional[TestIndex] = None
        self.sandbox_manager = SandboxManager(config.sandbox)
        self.case_generator = CaseGenerator(
            index=TestIndex(),  # Will be replaced after index is built
            output_dir=config.generated_dir,
            llm_client=config.llm_client,
        )

    async def evaluate(self, plan: Plan) -> EvaluationReport:
        """
        Run the full evaluation pipeline for a given Plan.

        Steps:
            1. Build/load test index
            2. Select cases
            3. Generate new cases
            4. Execute all cases via actor supervisor
            5. Return evaluation report
        """
        report = EvaluationReport(
            plan_summary=plan.summary,
            started_at=time.time(),
        )

        # Step 1: Build or load test index
        logger.info("Step 1: Building test index...")
        self.index = self._build_or_load_index()
        self.case_generator.index = self.index

        # Step 2: Select test cases
        logger.info("Step 2: Selecting test cases...")
        selector = CaseSelector(self.index, max_cases=self.config.max_cases)
        selection = selector.select(plan)
        logger.info(
            f"Selected {selection.total_selected} existing cases, "
            f"{selection.total_to_generate} to generate"
        )

        # Step 3: Generate new cases
        generated_cases: list[GeneratedCase] = []
        if selection.generation_requests:
            logger.info("Step 3: Generating new test cases...")
            generated_cases = await self._generate_cases(selection)
            logger.info(f"Generated {len(generated_cases)} new cases")

        # Step 4: Build actor pool and execute
        logger.info("Step 4: Executing test cases via actor supervisor...")
        all_entries = list(selection.selected)
        for gc in generated_cases:
            all_entries.append(gc.entry)

        results = await self._execute_cases(all_entries)
        report.results = results
        report.total_pods_launched = len(results)
        report.finished_at = time.time()

        # Log summary
        logger.info(f"\n{report.summary()}")
        for fail in report.failed_results():
            logger.warning(
                f"  FAILED: {fail.test_fqn}::{fail.test_method} - {fail.error_message}"
            )

        return report

    def _build_or_load_index(self) -> TestIndex:
        """Build test index from filesystem or load cached version."""
        index_path = Path(self.config.index_path)
        if index_path.exists():
            logger.info(f"Loading cached index from {index_path}")
            return TestIndex.load(index_path)

        logger.info(f"Scanning {self.config.tests_dir} for test cases...")
        index = scan_tests_directory(self.config.tests_dir)
        index.save(index_path)
        logger.info(f"Saved index ({len(index.entries)} entries) to {index_path}")
        return index

    async def _generate_cases(
        self, selection: SelectionResult
    ) -> list[GeneratedCase]:
        """Generate new test cases for suggestions that require generation."""
        generated = []
        for suggestion in selection.generation_requests:
            if self.config.llm_client:
                case = await self.case_generator.from_llm(suggestion)
            else:
                case = self.case_generator.from_template(suggestion)

            if case:
                self.case_generator.write_generated(case)
                generated.append(case)
        return generated

    async def _execute_cases(
        self, entries: list[TestCaseEntry]
    ) -> list[TestResult]:
        """Execute all test cases using the actor supervisor."""
        supervisor = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ONE,
            max_concurrency=self.config.max_concurrency,
        )

        # Create an actor for each test method in each test case
        for entry in entries:
            for method in entry.test_methods:
                supervisor.add_child(
                    ActorSpec(
                        factory=lambda e=entry, m=method.name: TestRunnerActor(
                            test_entry=e,
                            test_method=m,
                            sandbox_manager=self.sandbox_manager,
                            vespa_hosts=self.config.vespa_hosts,
                        ),
                        max_restarts=self.config.max_restarts,
                    )
                )

        # Run all actors
        records = await supervisor.run_all()

        # Collect results
        results = []
        for record in records:
            if isinstance(record.actor, TestRunnerActor):
                results.append(record.actor.result)

        return results


async def run_evaluation(
    plan_json: str,
    tests_dir: str = "tests",
    config: EvaluatorConfig | None = None,
) -> EvaluationReport:
    """
    Convenience function to run an evaluation from a Plan JSON string.

    Usage:
        import asyncio
        from vespa_evaluator.orchestrator import run_evaluation

        plan_json = '''
        {
            "summary": "Modified ranking expression evaluation",
            "changed_components": [{
                "name": "searchcore/ranking",
                "change_type": "ranking",
                "description": "Fixed tensor dot product edge case"
            }],
            "risk_level": "high",
            "test_suggestions": [
                {"category": "search", "area": "ranking", "priority": 1},
                {"category": "search", "area": "tensor",
                 "generate_new": true,
                 "generation_hint": "Test tensor dot product with zero vectors"}
            ]
        }
        '''

        report = asyncio.run(run_evaluation(plan_json, tests_dir="tests"))
        print(report.summary())
    """
    plan = Plan.from_json(plan_json)
    if config is None:
        config = EvaluatorConfig(tests_dir=tests_dir)
    evaluator = Evaluator(config)
    return await evaluator.evaluate(plan)
