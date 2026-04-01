"""
Ruby test file scanner - extracts metadata from Vespa system test files.

Parses Ruby test files to extract:
    - Class name and parent class
    - Owner (set_owner)
    - Description (set_description)
    - Test methods (def test_*)
    - Number of hosts required
    - Timeout settings

This builds the TestIndex used by the case selector.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..core.test_case_index import TestCaseEntry, TestIndex, TestMethod

logger = logging.getLogger(__name__)

# Regex patterns for Ruby test file parsing
RE_CLASS = re.compile(
    r"class\s+(\w+)\s*<\s*(\w+)"
)
RE_OWNER = re.compile(
    r'set_owner\s*\(\s*["\'](\w+)["\']\s*\)'
)
RE_DESCRIPTION = re.compile(
    r'set_description\s*\(\s*["\'](.+?)["\']\s*\)'
)
RE_TEST_METHOD = re.compile(
    r"def\s+(test_\w+)"
)
RE_NUM_HOSTS = re.compile(
    r"@num_hosts\s*=\s*(\d+)"
)
RE_TIMEOUT = re.compile(
    r"def\s+timeout_seconds\s*\n\s*(?:return\s+)?(\d+)"
)

# Map parent class names to tags for better searchability
PARENT_CLASS_TAGS = {
    "SearchTest": ["search"],
    "IndexedOnlySearchTest": ["search", "indexed"],
    "IndexedStreamingSearchTest": ["search", "indexed", "streaming"],
    "StreamingOnlySearchTest": ["search", "streaming"],
    "ContainerTest": ["container"],
    "ConfigTest": ["config"],
    "VdsTest": ["vds", "storage"],
    "DocprocTest": ["docproc"],
    "PerformanceTest": ["performance"],
}


def scan_test_file(file_path: Path, tests_root: Path) -> TestCaseEntry | None:
    """
    Parse a single Ruby test file and extract metadata.

    Returns None if the file is not a valid test case.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Failed to read {file_path}: {e}")
        return None

    # Extract class definition
    class_match = RE_CLASS.search(content)
    if not class_match:
        return None

    class_name = class_match.group(1)
    parent_class = class_match.group(2)

    # Extract test methods
    test_methods = []
    for i, line in enumerate(content.splitlines(), 1):
        m = RE_TEST_METHOD.search(line)
        if m:
            test_methods.append(TestMethod(name=m.group(1), line_number=i))

    if not test_methods:
        return None

    # Extract metadata
    owner_match = RE_OWNER.search(content)
    desc_match = RE_DESCRIPTION.search(content)
    hosts_match = RE_NUM_HOSTS.search(content)
    timeout_match = RE_TIMEOUT.search(content)

    # Derive category and area from path
    # e.g., tests/search/basicsearch/basic_search.rb → category=search, area=basicsearch
    rel_path = file_path.relative_to(tests_root)
    parts = rel_path.parts
    category = parts[0] if len(parts) > 1 else "unknown"
    area = parts[1] if len(parts) > 2 else parts[0]

    # Build tags from parent class
    tags = list(PARENT_CLASS_TAGS.get(parent_class, []))
    # Add category as tag if not already present
    if category not in tags:
        tags.append(category)

    return TestCaseEntry(
        file_path=str(rel_path),
        class_name=class_name,
        parent_class=parent_class,
        owner=owner_match.group(1) if owner_match else "",
        description=desc_match.group(1) if desc_match else "",
        category=category,
        area=area,
        test_methods=test_methods,
        tags=tags,
        num_hosts=int(hosts_match.group(1)) if hosts_match else 1,
        timeout_seconds=int(timeout_match.group(1)) if timeout_match else None,
    )


def scan_tests_directory(tests_root: str | Path) -> TestIndex:
    """
    Scan the entire tests/ directory and build a TestIndex.

    Usage:
        index = scan_tests_directory("/path/to/system-test/tests")
        index.save("test_index.json")
    """
    tests_root = Path(tests_root)
    if not tests_root.is_dir():
        raise ValueError(f"Tests directory not found: {tests_root}")

    index = TestIndex()
    rb_files = sorted(tests_root.rglob("*.rb"))
    scanned = 0
    skipped = 0

    for rb_file in rb_files:
        entry = scan_test_file(rb_file, tests_root)
        if entry:
            index.entries.append(entry)
            scanned += 1
        else:
            skipped += 1

    index.build_indexes()
    logger.info(
        f"Scanned {scanned} test cases ({skipped} non-test files skipped) "
        f"from {tests_root}"
    )
    return index
