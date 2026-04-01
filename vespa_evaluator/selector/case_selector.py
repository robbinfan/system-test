"""
Case selector - matches a Plan to existing test cases in the index.

Selection strategies:
    1. Direct match: Plan specifies category/area → find matching cases
    2. Component mapping: Map changed components to relevant test categories
    3. Risk-based expansion: Higher risk → include more related cases
    4. Tag matching: Match by tags (e.g., "ranking", "tensor", "streaming")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..core.plan import ChangeType, Plan, RiskLevel, TestSuggestion
from ..core.test_case_index import TestCaseEntry, TestIndex

logger = logging.getLogger(__name__)

# Maps Vespa component change types to test categories/areas that should be tested
COMPONENT_TEST_MAP: dict[ChangeType, list[tuple[str, str]]] = {
    ChangeType.RANKING: [
        ("search", "ranking"),
        ("search", "tensor"),
        ("search", "bm25"),
        ("search", "significance"),
    ],
    ChangeType.SCHEMA: [
        ("search", "basicsearch"),
        ("search", "schemachanges"),
        ("search", "struct_and_map_types"),
    ],
    ChangeType.FEEDING: [
        ("search", "feeding"),
        ("search", "basicsearch"),
        ("docproc", ""),
    ],
    ChangeType.CONFIG: [
        ("config", "deploy"),
        ("config", "configserver"),
        ("config", "config_proxy"),
    ],
    ChangeType.CONTAINER: [
        ("container", ""),
        ("search", "basicsearch"),
    ],
    ChangeType.STORAGE: [
        ("vds", ""),
        ("search", "basicsearch"),
    ],
    ChangeType.PERFORMANCE: [
        ("performance", ""),
    ],
}

# How many extra related cases to include per risk level
RISK_EXPANSION: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 5,
    RiskLevel.CRITICAL: 10,
}


@dataclass
class SelectionResult:
    """Result of case selection."""
    selected: list[TestCaseEntry] = field(default_factory=list)
    generation_requests: list[TestSuggestion] = field(default_factory=list)
    selection_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def total_selected(self) -> int:
        return len(self.selected)

    @property
    def total_to_generate(self) -> int:
        return len(self.generation_requests)


class CaseSelector:
    """
    Selects test cases from the index based on a Plan.

    Usage:
        index = TestIndex.load("test_index.json")
        selector = CaseSelector(index)
        result = selector.select(plan)

        # result.selected: existing cases to run
        # result.generation_requests: cases to dynamically generate
    """

    def __init__(self, index: TestIndex, max_cases: int = 50):
        self.index = index
        self.max_cases = max_cases

    def select(self, plan: Plan) -> SelectionResult:
        result = SelectionResult()
        seen_fqns: set[str] = set()

        # 1. Direct matches from plan suggestions
        for suggestion in plan.test_suggestions:
            if suggestion.generate_new:
                result.generation_requests.append(suggestion)
                continue

            matches = self.index.find_by_area(suggestion.category, suggestion.area)
            if not matches:
                # Fallback: text search by area keyword (more precise than full category)
                matches = self.index.search(suggestion.area)
            if not matches:
                # Last resort: search by category only, but limit to avoid flooding
                matches = self.index.find_by_category(suggestion.category)[:5]

            for entry in matches:
                if entry.fqn not in seen_fqns:
                    result.selected.append(entry)
                    result.selection_reasons[entry.fqn] = (
                        f"Direct match: {suggestion.reason or suggestion.area}"
                    )
                    seen_fqns.add(entry.fqn)

        # 2. Component-based expansion
        for component in plan.changed_components:
            test_areas = COMPONENT_TEST_MAP.get(component.change_type, [])
            for category, area in test_areas:
                if area:
                    matches = self.index.find_by_area(category, area)
                else:
                    matches = self.index.find_by_category(category)
                for entry in matches:
                    if entry.fqn not in seen_fqns:
                        result.selected.append(entry)
                        result.selection_reasons[entry.fqn] = (
                            f"Component mapping: {component.name} → {category}/{area}"
                        )
                        seen_fqns.add(entry.fqn)

        # 3. Risk-based expansion: add more related cases for higher risk
        extra_budget = RISK_EXPANSION.get(plan.risk_level, 0)
        if extra_budget > 0 and plan.affected_areas:
            for area_keyword in plan.affected_areas:
                extra_matches = self.index.search(area_keyword)
                added = 0
                for entry in extra_matches:
                    if entry.fqn not in seen_fqns and added < extra_budget:
                        result.selected.append(entry)
                        result.selection_reasons[entry.fqn] = (
                            f"Risk expansion ({plan.risk_level.value}): {area_keyword}"
                        )
                        seen_fqns.add(entry.fqn)
                        added += 1

        # 4. Always include basicsearch as smoke test for non-trivial changes
        if plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            smoke_tests = self.index.find_by_area("search", "basicsearch")
            for entry in smoke_tests:
                if entry.fqn not in seen_fqns:
                    result.selected.append(entry)
                    result.selection_reasons[entry.fqn] = "Smoke test (high risk)"
                    seen_fqns.add(entry.fqn)

        # 5. Sort by priority (direct matches first) and cap
        result.selected = result.selected[: self.max_cases]

        logger.info(
            f"Selected {result.total_selected} existing cases, "
            f"{result.total_to_generate} to generate"
        )
        return result
