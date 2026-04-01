"""
CLI entry point for the Vespa Evaluator.

Usage:
    # Scan tests and build index
    python -m vespa_evaluator scan --tests-dir tests --output test_index.json

    # Run evaluation from a plan file
    python -m vespa_evaluator evaluate --plan plan.json --tests-dir tests

    # Show index stats
    python -m vespa_evaluator stats --index test_index.json
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path


def cmd_scan(args):
    """Scan test directory and build index."""
    from .scanner.ruby_scanner import scan_tests_directory

    index = scan_tests_directory(args.tests_dir)
    index.save(args.output)

    print(f"Scanned {len(index.entries)} test cases")
    print(f"Index saved to {args.output}")

    # Print category breakdown
    categories = {}
    for entry in index.entries:
        categories[entry.category] = categories.get(entry.category, 0) + 1
    print("\nBy category:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")


def cmd_stats(args):
    """Show index statistics."""
    from .core.test_case_index import TestIndex

    index = TestIndex.load(args.index)
    print(f"Total test cases: {len(index.entries)}")

    # Category breakdown
    categories = {}
    owners = {}
    parent_classes = {}
    total_methods = 0
    for entry in index.entries:
        categories[entry.category] = categories.get(entry.category, 0) + 1
        if entry.owner:
            owners[entry.owner] = owners.get(entry.owner, 0) + 1
        parent_classes[entry.parent_class] = parent_classes.get(entry.parent_class, 0) + 1
        total_methods += len(entry.test_methods)

    print(f"Total test methods: {total_methods}")
    print(f"\nBy category:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    print(f"\nTop parent classes:")
    for cls, count in sorted(parent_classes.items(), key=lambda x: -x[1])[:10]:
        print(f"  {cls}: {count}")

    print(f"\nTop owners:")
    for owner, count in sorted(owners.items(), key=lambda x: -x[1])[:10]:
        print(f"  {owner}: {count}")


def cmd_evaluate(args):
    """Run evaluation from a plan file."""
    from .orchestrator import EvaluatorConfig, run_evaluation

    plan_json = Path(args.plan).read_text()

    config = EvaluatorConfig(
        tests_dir=args.tests_dir,
        max_concurrency=args.concurrency,
        max_cases=args.max_cases,
    )
    if args.vespa_hosts:
        config.vespa_hosts = args.vespa_hosts.split(",")

    report = asyncio.run(run_evaluation(plan_json, config=config))
    print(report.summary())

    if args.output:
        results_data = {
            "plan_summary": report.plan_summary,
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "errors": report.errors,
            "pass_rate": report.pass_rate,
            "duration_seconds": report.duration_seconds,
            "results": [
                {
                    "test_fqn": r.test_fqn,
                    "test_method": r.test_method,
                    "status": r.status.value,
                    "duration_seconds": r.duration_seconds,
                    "error_message": r.error_message,
                    "pod_name": r.pod_name,
                }
                for r in report.results
            ],
        }
        Path(args.output).write_text(json.dumps(results_data, indent=2))
        print(f"\nDetailed results saved to {args.output}")


def cmd_select(args):
    """Preview case selection for a plan without executing."""
    from .core.plan import Plan
    from .core.test_case_index import TestIndex
    from .selector.case_selector import CaseSelector

    plan = Plan.from_json(Path(args.plan).read_text())
    index = TestIndex.load(args.index)
    selector = CaseSelector(index, max_cases=args.max_cases)
    result = selector.select(plan)

    print(f"Plan: {plan.summary}")
    print(f"Risk level: {plan.risk_level.value}")
    print(f"\nSelected {result.total_selected} existing cases:")
    for entry in result.selected:
        reason = result.selection_reasons.get(entry.fqn, "")
        methods = ", ".join(m.name for m in entry.test_methods)
        print(f"  [{entry.category}/{entry.area}] {entry.class_name}")
        print(f"    Methods: {methods}")
        print(f"    Reason: {reason}")

    if result.generation_requests:
        print(f"\n{result.total_to_generate} cases to generate:")
        for req in result.generation_requests:
            print(f"  [{req.category}/{req.area}] {req.generation_hint}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Vespa Evaluator - Actor-based test evaluation framework"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan tests and build index")
    p_scan.add_argument("--tests-dir", default="tests", help="Path to tests directory")
    p_scan.add_argument("--output", "-o", default="test_index.json", help="Output index file")

    # stats
    p_stats = subparsers.add_parser("stats", help="Show index statistics")
    p_stats.add_argument("--index", default="test_index.json", help="Path to index file")

    # evaluate
    p_eval = subparsers.add_parser("evaluate", help="Run evaluation from a plan")
    p_eval.add_argument("--plan", required=True, help="Path to plan JSON file")
    p_eval.add_argument("--tests-dir", default="tests", help="Path to tests directory")
    p_eval.add_argument("--concurrency", type=int, default=10, help="Max concurrent pods")
    p_eval.add_argument("--max-cases", type=int, default=50, help="Max cases to select")
    p_eval.add_argument("--vespa-hosts", default="", help="Comma-separated Vespa hosts")
    p_eval.add_argument("--output", "-o", help="Output results JSON file")

    # select (preview)
    p_sel = subparsers.add_parser("select", help="Preview case selection")
    p_sel.add_argument("--plan", required=True, help="Path to plan JSON file")
    p_sel.add_argument("--index", default="test_index.json", help="Path to index file")
    p_sel.add_argument("--max-cases", type=int, default=50, help="Max cases to select")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "scan": cmd_scan,
        "stats": cmd_stats,
        "evaluate": cmd_evaluate,
        "select": cmd_select,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
