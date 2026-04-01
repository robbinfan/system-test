"""
TestRunnerActor - Actor that executes a single test case in a K8s sandbox.

This is the workhorse actor. Each instance:
    1. Receives a "start" message with test case info
    2. Launches a K8s Pod via SandboxManager
    3. Waits for Pod completion
    4. Collects results and reports back
"""

from __future__ import annotations

import logging
from typing import Optional

from ..core.result import TestResult, TestStatus
from ..core.test_case_index import TestCaseEntry
from ..k8s.sandbox import PodPhase, PodResult, SandboxManager
from .actor import Actor, ActorState, Message

logger = logging.getLogger(__name__)


class TestRunnerActor(Actor):
    """
    Runs a single test case in an isolated K8s Pod.

    The actor manages the full lifecycle:
        launch pod → wait → collect logs → report result

    If the pod fails with an infrastructure error (not test failure),
    the Supervisor can restart this actor for a retry.
    """

    def __init__(
        self,
        test_entry: TestCaseEntry,
        test_method: str,
        sandbox_manager: SandboxManager,
        vespa_hosts: list[str] | None = None,
    ):
        super().__init__(actor_id=f"runner-{test_entry.area}-{test_method}")
        self.test_entry = test_entry
        self.test_method = test_method
        self.sandbox = sandbox_manager
        self.vespa_hosts = vespa_hosts or []
        self.result = TestResult(
            test_fqn=test_entry.fqn,
            test_method=test_method,
        )

    async def handle_message(self, msg: Message) -> Optional[Message]:
        if msg.type == "start":
            return await self._run_test()
        elif msg.type == "status":
            return Message(
                type="status_response",
                payload={
                    "actor_id": self.actor_id,
                    "state": self.state.value,
                    "result_status": self.result.status.value,
                },
            )
        return None

    async def _run_test(self) -> Message:
        """Execute the test in a K8s Pod and return the result."""
        self.result.mark_running()
        logger.info(
            f"Actor {self.actor_id}: Running {self.test_entry.fqn}::{self.test_method}"
        )

        try:
            # Launch pod
            pod_name = await self.sandbox.launch(
                test_file=self.test_entry.file_path,
                test_method=self.test_method,
                vespa_hosts=self.vespa_hosts,
            )
            self.result.pod_name = pod_name

            # Wait for completion
            pod_result: PodResult = await self.sandbox.wait_for_completion(pod_name)

            # Map pod result to test result
            if pod_result.phase == PodPhase.SUCCEEDED and pod_result.exit_code == 0:
                self.result.mark_passed()
                logger.info(f"Actor {self.actor_id}: PASSED")
            elif pod_result.phase == PodPhase.FAILED:
                self.result.mark_failed(
                    message=f"Test failed with exit code {pod_result.exit_code}",
                    stdout=pod_result.stdout,
                    stderr=pod_result.stderr,
                )
                logger.info(f"Actor {self.actor_id}: FAILED")
            else:
                # Infrastructure error or timeout → actor state = FAILED
                # so Supervisor can restart it
                self.result.mark_error(
                    message=pod_result.stderr or f"Pod ended in {pod_result.phase.value}"
                )
                self.state = ActorState.FAILED
                logger.warning(f"Actor {self.actor_id}: ERROR - {pod_result.phase.value}")

            # Cleanup pod
            await self.sandbox.cleanup(pod_name)

        except Exception as e:
            self.result.mark_error(str(e))
            self.state = ActorState.FAILED
            logger.error(f"Actor {self.actor_id}: Exception - {e}")

        # Stop the actor after completing its single task
        self.state = ActorState.DONE

        return Message(type="test_result", payload=self.result)
