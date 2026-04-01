"""
Supervisor - manages a pool of actors, handles failures and restarts.

Inspired by Erlang/OTP Supervisor with restart strategies:
    - one_for_one: Restart only the failed actor
    - one_for_all: Restart all actors if one fails
    - rest_for_one: Restart the failed actor and all actors started after it

For the Vespa evaluator, we primarily use one_for_one since each test case
is independent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .actor import Actor, ActorState, Message

logger = logging.getLogger(__name__)


class RestartStrategy(str, Enum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"


@dataclass
class ActorSpec:
    """Specification for creating an actor."""
    factory: Callable[[], Actor]
    max_restarts: int = 3
    restart_window_seconds: float = 60.0


@dataclass
class ActorRecord:
    """Runtime record of a managed actor."""
    spec: ActorSpec
    actor: Actor
    task: Optional[asyncio.Task] = None
    restart_count: int = 0
    restart_timestamps: list[float] = field(default_factory=list)
    started_at: float = 0.0


class Supervisor:
    """
    Manages a pool of actors with automatic restart on failure.

    Usage:
        supervisor = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ONE,
            max_concurrency=10,  # max 10 actors running simultaneously
        )

        # Register actor specs
        for test_case in selected_cases:
            supervisor.add_child(ActorSpec(
                factory=lambda tc=test_case: TestRunnerActor(tc),
                max_restarts=2,
            ))

        # Run all actors and collect results
        results = await supervisor.run_all()
    """

    def __init__(
        self,
        strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE,
        max_concurrency: int = 10,
    ):
        self.strategy = strategy
        self.max_concurrency = max_concurrency
        self._children: list[ActorRecord] = []
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._results: list[Message] = []
        self._result_queue: asyncio.Queue[Message] = asyncio.Queue()

    def add_child(self, spec: ActorSpec):
        """Register an actor specification."""
        actor = spec.factory()
        self._children.append(ActorRecord(spec=spec, actor=actor))

    async def _run_actor(self, record: ActorRecord) -> Optional[Message]:
        """Run a single actor with concurrency control and restart logic."""
        async with self._semaphore:
            record.started_at = time.time()

            while True:
                record.actor = record.spec.factory() if record.restart_count > 0 else record.actor
                record.task = record.actor.start()

                # Send a "start" message to kick off the actor's work
                await record.actor.mailbox.put(
                    Message(type="start", sender_id="supervisor")
                )

                try:
                    await record.task
                except Exception as e:
                    record.actor.state = ActorState.FAILED
                    logger.error(
                        f"Actor {record.actor.actor_id} crashed: {e}"
                    )

                if record.actor.state == ActorState.FAILED:
                    if self._should_restart(record):
                        record.restart_count += 1
                        record.restart_timestamps.append(time.time())
                        logger.info(
                            f"Restarting actor {record.actor.actor_id} "
                            f"(attempt {record.restart_count}/{record.spec.max_restarts})"
                        )
                        continue
                    else:
                        logger.warning(
                            f"Actor {record.actor.actor_id} exceeded max restarts"
                        )

                break

        return None

    def _should_restart(self, record: ActorRecord) -> bool:
        """Check if an actor should be restarted based on its restart policy."""
        if record.restart_count >= record.spec.max_restarts:
            return False

        # Check restart frequency within the window
        now = time.time()
        window_start = now - record.spec.restart_window_seconds
        recent_restarts = [
            t for t in record.restart_timestamps if t > window_start
        ]
        return len(recent_restarts) < record.spec.max_restarts

    async def run_all(self) -> list[ActorRecord]:
        """
        Run all registered actors with concurrency control.

        Returns list of ActorRecords with final states.
        """
        tasks = [
            asyncio.create_task(self._run_actor(record))
            for record in self._children
        ]

        await asyncio.gather(*tasks, return_exceptions=True)
        return self._children

    async def stop_all(self):
        """Stop all running actors."""
        for record in self._children:
            if record.actor.state == ActorState.RUNNING:
                await record.actor.stop()

    @property
    def summary(self) -> dict:
        states = {}
        for record in self._children:
            state = record.actor.state.value
            states[state] = states.get(state, 0) + 1
        return {
            "total": len(self._children),
            "states": states,
            "total_restarts": sum(r.restart_count for r in self._children),
        }
