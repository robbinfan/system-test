"""
Test case index - structured catalog of all available Vespa system tests.

The index is built by scanning the tests/ directory and extracting metadata
from Ruby test files (class name, owner, description, test methods, etc.).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TestMethod:
    """A single test method within a test class."""
    name: str
    line_number: int = 0


@dataclass
class TestCaseEntry:
    """Metadata for a single test case file."""
    file_path: str
    class_name: str
    parent_class: str = ""
    owner: str = ""
    description: str = ""
    category: str = ""  # search, config, container, etc.
    area: str = ""  # basicsearch, ranking, deploy, etc.
    test_methods: list[TestMethod] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    num_hosts: int = 1
    timeout_seconds: Optional[int] = None

    @property
    def fqn(self) -> str:
        """Fully qualified name: category/area/class_name."""
        return f"{self.category}/{self.area}/{self.class_name}"

    def matches_area(self, category: str, area: str = "") -> bool:
        if self.category != category:
            return False
        if area and self.area != area:
            return False
        return True

    def matches_tags(self, tags: list[str]) -> bool:
        return bool(set(tags) & set(self.tags))


@dataclass
class TestIndex:
    """
    Complete index of all test cases.

    Supports efficient lookup by category, area, tags, and parent class.
    """
    entries: list[TestCaseEntry] = field(default_factory=list)
    _by_category: dict[str, list[TestCaseEntry]] = field(
        default_factory=dict, repr=False
    )
    _by_fqn: dict[str, TestCaseEntry] = field(
        default_factory=dict, repr=False
    )

    def build_indexes(self):
        """Build lookup indexes after entries are populated."""
        self._by_category.clear()
        self._by_fqn.clear()
        for entry in self.entries:
            self._by_category.setdefault(entry.category, []).append(entry)
            self._by_fqn[entry.fqn] = entry

    def find_by_category(self, category: str) -> list[TestCaseEntry]:
        return self._by_category.get(category, [])

    def find_by_area(self, category: str, area: str) -> list[TestCaseEntry]:
        return [
            e for e in self.find_by_category(category)
            if e.area == area
        ]

    def find_by_parent_class(self, parent_class: str) -> list[TestCaseEntry]:
        return [e for e in self.entries if e.parent_class == parent_class]

    def find_by_fqn(self, fqn: str) -> Optional[TestCaseEntry]:
        return self._by_fqn.get(fqn)

    def search(self, query: str) -> list[TestCaseEntry]:
        """Simple text search across class name, description, and area."""
        q = query.lower()
        return [
            e for e in self.entries
            if q in e.class_name.lower()
            or q in e.description.lower()
            or q in e.area.lower()
            or any(q in t.lower() for t in e.tags)
        ]

    def save(self, path: str | Path):
        data = {
            "version": 1,
            "count": len(self.entries),
            "entries": [
                {
                    "file_path": e.file_path,
                    "class_name": e.class_name,
                    "parent_class": e.parent_class,
                    "owner": e.owner,
                    "description": e.description,
                    "category": e.category,
                    "area": e.area,
                    "test_methods": [
                        {"name": m.name, "line_number": m.line_number}
                        for m in e.test_methods
                    ],
                    "tags": e.tags,
                    "num_hosts": e.num_hosts,
                    "timeout_seconds": e.timeout_seconds,
                }
                for e in self.entries
            ],
        }
        Path(path).write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: str | Path) -> TestIndex:
        data = json.loads(Path(path).read_text())
        index = cls()
        for e in data["entries"]:
            index.entries.append(
                TestCaseEntry(
                    file_path=e["file_path"],
                    class_name=e["class_name"],
                    parent_class=e.get("parent_class", ""),
                    owner=e.get("owner", ""),
                    description=e.get("description", ""),
                    category=e.get("category", ""),
                    area=e.get("area", ""),
                    test_methods=[
                        TestMethod(name=m["name"], line_number=m.get("line_number", 0))
                        for m in e.get("test_methods", [])
                    ],
                    tags=e.get("tags", []),
                    num_hosts=e.get("num_hosts", 1),
                    timeout_seconds=e.get("timeout_seconds"),
                )
            )
        index.build_indexes()
        return index
