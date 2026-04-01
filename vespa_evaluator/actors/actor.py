"""
Actor model implementation inspired by Erlang/OTP.

Key concepts:
    - Actor: Lightweight process that receives messages and performs work
    - Supervisor: Manages actor lifecycle, restarts on failure
    - Message: Typed communication between actors
    - Mailbox: Async queue for message delivery

Unlike Erlang, we use Python asyncio instead of OS processes.
Each Actor is a coroutine with its own mailbox (asyncio.Queue).
For heavy isolation, actors can be backed by K8s Pods.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ActorState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class Message:
    """Message passed between actors."""
    type: str
    payload: Any = None
    sender_id: str = ""
    correlation_id: str = ""

    def __post_init__(self):
        if not self.correlation_id:
            self.correlation_id = uuid.uuid4().hex[:8]


class Actor(ABC):
    """
    Base actor - an isolated unit of computation with a mailbox.

    Subclass and implement `handle_message` to define behavior.
    Actors communicate exclusively through message passing.

    Example:
        class TestRunnerActor(Actor):
            async def handle_message(self, msg: Message) -> Optional[Message]:
                if msg.type == "run_test":
                    result = await self.run_test(msg.payload)
                    return Message(type="test_result", payload=result)
    """

    def __init__(self, actor_id: str = "", max_mailbox_size: int = 100):
        self.actor_id = actor_id or uuid.uuid4().hex[:12]
        self.state = ActorState.IDLE
        self.mailbox: asyncio.Queue[Message] = asyncio.Queue(
            maxsize=max_mailbox_size
        )
        self._task: Optional[asyncio.Task] = None
        self._reply_futures: dict[str, asyncio.Future] = {}

    @abstractmethod
    async def handle_message(self, msg: Message) -> Optional[Message]:
        """Process a message. Return a reply message or None."""
        ...

    async def on_start(self):
        """Called when actor starts. Override for initialization."""
        pass

    async def on_stop(self):
        """Called when actor stops. Override for cleanup."""
        pass

    async def on_error(self, error: Exception, msg: Message):
        """Called when handle_message raises. Override for custom error handling."""
        logger.error(f"Actor {self.actor_id} error processing {msg.type}: {error}")

    async def send(self, target: Actor, msg: Message):
        """Send a message to another actor."""
        msg.sender_id = self.actor_id
        await target.mailbox.put(msg)

    async def ask(self, target: Actor, msg: Message, timeout: float = 300) -> Message:
        """Send a message and wait for a reply (request-response pattern)."""
        msg.sender_id = self.actor_id
        future: asyncio.Future[Message] = asyncio.get_event_loop().create_future()
        target._reply_futures[msg.correlation_id] = future
        await target.mailbox.put(msg)
        return await asyncio.wait_for(future, timeout=timeout)

    async def _run_loop(self):
        """Main actor loop - process messages from mailbox."""
        self.state = ActorState.RUNNING
        await self.on_start()
        try:
            while self.state == ActorState.RUNNING:
                try:
                    msg = await asyncio.wait_for(
                        self.mailbox.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if msg.type == "__stop__":
                    break

                try:
                    reply = await self.handle_message(msg)
                    # If someone is waiting for a reply via ask(), deliver it
                    if reply and msg.correlation_id in self._reply_futures:
                        future = self._reply_futures.pop(msg.correlation_id)
                        if not future.done():
                            future.set_result(reply)
                except Exception as e:
                    await self.on_error(e, msg)
                    if msg.correlation_id in self._reply_futures:
                        future = self._reply_futures.pop(msg.correlation_id)
                        if not future.done():
                            future.set_exception(e)

        except asyncio.CancelledError:
            pass
        finally:
            await self.on_stop()
            if self.state != ActorState.FAILED:
                self.state = ActorState.DONE

    def start(self) -> asyncio.Task:
        """Start the actor's message processing loop."""
        self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def stop(self):
        """Gracefully stop the actor."""
        self.state = ActorState.STOPPED
        await self.mailbox.put(Message(type="__stop__"))
        if self._task:
            await self._task
