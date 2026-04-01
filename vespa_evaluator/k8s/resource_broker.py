"""
Resource Broker — bridges CMDB and K8s for dynamic resource allocation.

Instead of hardcoded resource limits, the broker:
    1. Queries CMDB (via external skill) for available nodes/capacity
    2. Matches test requirements to available resources
    3. Produces optimized SandboxConfig per test case
    4. Tracks resource usage and releases on completion

This module defines the interface. The actual CMDB queries are delegated
to external skills (e.g., a CMDB skill that wraps your internal API).

Integration points:
    - CMDB skill: provides node inventory, CPU/memory availability
    - K8s API: for real-time cluster capacity
    - Test metadata: num_hosts, timeout, resource intensity
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..core.test_case_index import TestCaseEntry

logger = logging.getLogger(__name__)


class NodeTier(str, Enum):
    """Resource tier for test execution nodes."""
    SMALL = "small"      # 2 CPU, 4Gi — config tests, smoke tests
    MEDIUM = "medium"    # 4 CPU, 8Gi — standard search tests
    LARGE = "large"      # 8 CPU, 16Gi — performance tests, multi-node
    XLARGE = "xlarge"    # 16 CPU, 32Gi — large-scale stress tests


# Resource specs per tier
TIER_SPECS = {
    NodeTier.SMALL:  {"cpu_request": "500m",  "cpu_limit": "2",  "memory_request": "1Gi",  "memory_limit": "4Gi"},
    NodeTier.MEDIUM: {"cpu_request": "2",     "cpu_limit": "4",  "memory_request": "4Gi",  "memory_limit": "8Gi"},
    NodeTier.LARGE:  {"cpu_request": "4",     "cpu_limit": "8",  "memory_request": "8Gi",  "memory_limit": "16Gi"},
    NodeTier.XLARGE: {"cpu_request": "8",     "cpu_limit": "16", "memory_request": "16Gi", "memory_limit": "32Gi"},
}


@dataclass
class CmdbNode:
    """A node from CMDB inventory."""
    hostname: str
    ip: str
    cpu_total: int
    cpu_available: int
    memory_total_gi: int
    memory_available_gi: int
    labels: dict = field(default_factory=dict)
    zone: str = ""
    status: str = "idle"  # idle, allocated, maintenance


@dataclass
class ResourceAllocation:
    """An allocated set of resources for a test execution."""
    nodes: list[CmdbNode]
    tier: NodeTier
    sandbox_overrides: dict = field(default_factory=dict)
    vespa_hosts: list[str] = field(default_factory=list)
    config_server: str = ""

    @property
    def total_cpu(self) -> int:
        return sum(n.cpu_available for n in self.nodes)

    @property
    def total_memory_gi(self) -> int:
        return sum(n.memory_available_gi for n in self.nodes)


class CmdbProvider(ABC):
    """
    Abstract interface for CMDB integration.

    Implement this to connect to your actual CMDB. The implementation
    can shell out to an external skill or call an internal API.
    """

    @abstractmethod
    async def list_available_nodes(
        self,
        min_cpu: int = 0,
        min_memory_gi: int = 0,
        zone: str = "",
        labels: dict | None = None,
    ) -> list[CmdbNode]:
        """Query CMDB for nodes matching the given criteria."""
        ...

    @abstractmethod
    async def allocate_nodes(
        self,
        nodes: list[CmdbNode],
        purpose: str = "",
        ttl_seconds: int = 3600,
    ) -> bool:
        """Mark nodes as allocated in CMDB. Returns True on success."""
        ...

    @abstractmethod
    async def release_nodes(self, nodes: list[CmdbNode]) -> bool:
        """Release previously allocated nodes back to the pool."""
        ...


class SkillBasedCmdbProvider(CmdbProvider):
    """
    CMDB provider that delegates to an external Claude Code skill.

    The skill is expected to:
        - Accept JSON commands on stdin
        - Return JSON results on stdout
        - Handle: list_nodes, allocate, release

    This is a bridge — the actual CMDB logic lives in your skill.
    """

    def __init__(self, skill_command: str = "your-cmdb-skill"):
        self.skill_command = skill_command

    async def list_available_nodes(
        self,
        min_cpu: int = 0,
        min_memory_gi: int = 0,
        zone: str = "",
        labels: dict | None = None,
    ) -> list[CmdbNode]:
        """
        In production, this would invoke the CMDB skill:
            /cmdb-query --status idle --min-cpu {min_cpu} --min-memory {min_memory_gi}

        The skill returns JSON list of nodes. We parse into CmdbNode objects.
        """
        # Placeholder — replace with actual skill invocation
        logger.info(
            f"[CMDB] Would query: min_cpu={min_cpu}, "
            f"min_memory={min_memory_gi}Gi, zone={zone}"
        )
        return []

    async def allocate_nodes(
        self,
        nodes: list[CmdbNode],
        purpose: str = "",
        ttl_seconds: int = 3600,
    ) -> bool:
        hostnames = [n.hostname for n in nodes]
        logger.info(f"[CMDB] Would allocate: {hostnames} for {purpose}")
        return True

    async def release_nodes(self, nodes: list[CmdbNode]) -> bool:
        hostnames = [n.hostname for n in nodes]
        logger.info(f"[CMDB] Would release: {hostnames}")
        return True


class ResourceBroker:
    """
    Decides what resources each test case needs and allocates them.

    The broker examines test case metadata (category, num_hosts, timeout,
    parent class) and determines:
        1. Which tier to use (SMALL/MEDIUM/LARGE/XLARGE)
        2. How many nodes to allocate
        3. Specific sandbox config overrides

    Usage:
        cmdb = SkillBasedCmdbProvider()
        broker = ResourceBroker(cmdb)

        allocation = await broker.allocate_for_test(test_entry)
        # Use allocation.vespa_hosts, allocation.sandbox_overrides, etc.

        await broker.release(allocation)
    """

    def __init__(self, cmdb_provider: CmdbProvider):
        self.cmdb = cmdb_provider
        self._active_allocations: list[ResourceAllocation] = []

    def classify_tier(self, entry: TestCaseEntry) -> NodeTier:
        """Determine resource tier based on test case metadata."""
        # Performance tests → LARGE or XLARGE
        if entry.category == "performance":
            return NodeTier.XLARGE if entry.num_hosts > 2 else NodeTier.LARGE

        # Multi-node tests → LARGE
        if entry.num_hosts > 1:
            return NodeTier.LARGE

        # Specific heavy test areas
        heavy_areas = {
            "bigdocument", "bigindexclosure", "stress",
            "redistribution", "splitjoin",
        }
        if entry.area in heavy_areas:
            return NodeTier.LARGE

        # Config and smoke tests → SMALL
        if entry.category in ("config",):
            return NodeTier.SMALL

        # Default search/container/docproc → MEDIUM
        return NodeTier.MEDIUM

    async def allocate_for_test(
        self, entry: TestCaseEntry
    ) -> ResourceAllocation:
        """Allocate resources for a specific test case."""
        tier = self.classify_tier(entry)
        specs = TIER_SPECS[tier]
        num_nodes = max(entry.num_hosts, 1)

        # Query CMDB for available nodes
        min_cpu = int(specs["cpu_limit"])
        min_mem = int(specs["memory_limit"].replace("Gi", ""))

        available = await self.cmdb.list_available_nodes(
            min_cpu=min_cpu,
            min_memory_gi=min_mem,
        )

        # Select best-fit nodes
        selected = available[:num_nodes] if available else []

        if selected:
            await self.cmdb.allocate_nodes(
                selected,
                purpose=f"vespa-eval: {entry.fqn}",
                ttl_seconds=(entry.timeout_seconds or 1200) + 300,
            )

        allocation = ResourceAllocation(
            nodes=selected,
            tier=tier,
            sandbox_overrides=specs,
            vespa_hosts=[n.hostname for n in selected],
            config_server=selected[0].hostname if selected else "",
        )

        self._active_allocations.append(allocation)
        logger.info(
            f"Allocated {tier.value} tier ({num_nodes} nodes) for {entry.fqn}"
        )
        return allocation

    async def release(self, allocation: ResourceAllocation):
        """Release resources back to the pool."""
        if allocation.nodes:
            await self.cmdb.release_nodes(allocation.nodes)
        if allocation in self._active_allocations:
            self._active_allocations.remove(allocation)

    async def release_all(self):
        """Release all active allocations."""
        for alloc in list(self._active_allocations):
            await self.release(alloc)
