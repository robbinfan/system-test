"""
Topology Profiles — predefined cluster shapes for different test levels.

Models real Vespa production topologies at reduced scale:
    - ConfigServer quorum (always 3, can't shrink — ZK majority)
    - Container/QRS layer (stateless, 2+ for load balancing tests)
    - Content/Proton layer (stateful, 3-6 for distribution/redistribution)

Production reference (from user):
    - ConfigServer × 3
    - Content × 28 per replica (single replica)

These profiles define how to map that to test-scale clusters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TopologyLevel(str, Enum):
    """Test topology levels — increasing fidelity and cost."""
    SINGLE = "single"           # Level 1: all-in-one pod (fast, cheap)
    SPLIT = "split"             # Level 2: separate processes, shared node
    PRODUCTION_LIKE = "production-like"  # Level 3: separate pods per role
    FULL_SCALE = "full-scale"   # Level 4: production replica count (rare)


@dataclass
class RoleSpec:
    """Spec for a single Vespa role in the topology."""
    count: int
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    # Storage for content nodes
    storage_gi: int = 0


@dataclass
class TopologyProfile:
    """Complete cluster topology definition."""
    name: str
    level: TopologyLevel
    description: str
    configserver: RoleSpec
    container: RoleSpec
    content: RoleSpec
    # Additional roles (e.g., feed-container, admin)
    extras: dict[str, RoleSpec] = field(default_factory=dict)

    @property
    def total_pods(self) -> int:
        total = self.configserver.count + self.container.count + self.content.count
        total += sum(r.count for r in self.extras.values())
        return total

    @property
    def total_cpu_requests(self) -> float:
        """Rough CPU request total (parses "500m" and "4" formats)."""
        def parse_cpu(s: str) -> float:
            return int(s.replace("m", "")) / 1000 if "m" in s else float(s)

        total = 0.0
        for role in [self.configserver, self.container, self.content]:
            total += parse_cpu(role.cpu_request) * role.count
        for role in self.extras.values():
            total += parse_cpu(role.cpu_request) * role.count
        return total

    def summary(self) -> str:
        parts = [
            f"ConfigServer × {self.configserver.count}",
            f"Container × {self.container.count}",
            f"Content × {self.content.count}",
        ]
        for name, spec in self.extras.items():
            parts.append(f"{name} × {spec.count}")
        return (
            f"[{self.level.value}] {self.name}: "
            + ", ".join(parts)
            + f" = {self.total_pods} pods"
        )


# === Predefined Profiles ===

SINGLE_NODE = TopologyProfile(
    name="single-node",
    level=TopologyLevel.SINGLE,
    description="All Vespa roles in a single pod. Fastest, for functional tests.",
    configserver=RoleSpec(count=1, cpu_request="500m", cpu_limit="2", memory_request="1Gi", memory_limit="4Gi"),
    container=RoleSpec(count=0, cpu_request="0", cpu_limit="0", memory_request="0", memory_limit="0"),
    content=RoleSpec(count=0, cpu_request="0", cpu_limit="0", memory_request="0", memory_limit="0"),
)

MINIMAL_SPLIT = TopologyProfile(
    name="minimal-split",
    level=TopologyLevel.SPLIT,
    description="Minimal split: 1 configserver + 1 container + 1 content. Tests basic distribution.",
    configserver=RoleSpec(count=1, cpu_request="500m", cpu_limit="2", memory_request="1Gi", memory_limit="2Gi"),
    container=RoleSpec(count=1, cpu_request="500m", cpu_limit="2", memory_request="1Gi", memory_limit="2Gi"),
    content=RoleSpec(count=1, cpu_request="1", cpu_limit="2", memory_request="2Gi", memory_limit="4Gi"),
)

PRODUCTION_LIKE = TopologyProfile(
    name="production-like",
    level=TopologyLevel.PRODUCTION_LIKE,
    description=(
        "Production-like topology: 3 configservers (ZK quorum), "
        "2 containers, 6 content nodes. Tests distribution, failover, "
        "and redistribution at meaningful scale."
    ),
    configserver=RoleSpec(
        count=3,  # ZK quorum — minimum for leader election
        cpu_request="1", cpu_limit="2",
        memory_request="2Gi", memory_limit="4Gi",
    ),
    container=RoleSpec(
        count=2,
        cpu_request="1", cpu_limit="4",
        memory_request="2Gi", memory_limit="8Gi",
    ),
    content=RoleSpec(
        count=6,  # 28 production → 6 test (enough for redistribution)
        cpu_request="2", cpu_limit="4",
        memory_request="4Gi", memory_limit="8Gi",
        storage_gi=10,
    ),
)

FULL_SCALE = TopologyProfile(
    name="full-scale",
    level=TopologyLevel.FULL_SCALE,
    description=(
        "Full production replica: 3 configservers, 2+ containers, "
        "28 content nodes. Only for production-shadow validation."
    ),
    configserver=RoleSpec(
        count=3,
        cpu_request="2", cpu_limit="4",
        memory_request="4Gi", memory_limit="8Gi",
    ),
    container=RoleSpec(
        count=2,
        cpu_request="2", cpu_limit="4",
        memory_request="4Gi", memory_limit="8Gi",
    ),
    content=RoleSpec(
        count=28,
        cpu_request="2", cpu_limit="4",
        memory_request="4Gi", memory_limit="8Gi",
        storage_gi=50,
    ),
)


# Profile registry
PROFILES: dict[str, TopologyProfile] = {
    "single-node": SINGLE_NODE,
    "minimal-split": MINIMAL_SPLIT,
    "production-like": PRODUCTION_LIKE,
    "full-scale": FULL_SCALE,
}


def get_profile(name: str) -> TopologyProfile:
    if name not in PROFILES:
        available = ", ".join(PROFILES.keys())
        raise ValueError(f"Unknown topology profile: {name}. Available: {available}")
    return PROFILES[name]


def recommend_profile(risk_level: str, num_changed_components: int) -> TopologyProfile:
    """Recommend a topology profile based on change risk."""
    if risk_level == "critical" or num_changed_components > 5:
        return PRODUCTION_LIKE
    elif risk_level == "high" or num_changed_components > 2:
        return MINIMAL_SPLIT
    else:
        return SINGLE_NODE
