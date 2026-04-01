# Vespa System Test Repository

## Overview

This is the system test framework for the [Vespa](https://vespa.ai) search engine.
Tests are written in Ruby and execute against live Vespa clusters.

## Project Structure

- `tests/` — Test cases organized by category (search, config, container, docproc, vds, performance)
- `lib/` — Test framework core (TestCase, assertions, app generators, node proxies)
- `vespa_evaluator/` — AI-powered test evaluation framework (Python)

## Vespa Evaluator

The evaluator automates test selection and execution based on code change analysis.

### Quick Start

```bash
# Build test index (737+ test cases)
python -m vespa_evaluator scan --tests-dir tests --output test_index.json

# Preview which tests would be selected for a plan
python -m vespa_evaluator select --plan examples/sample_plan.json --index test_index.json

# Run full evaluation (dry-run without K8s)
python -m vespa_evaluator evaluate --plan examples/sample_plan.json --tests-dir tests
```

### Skills

- `/vespa-eval-run` — Full pipeline: generate plan from git diff → select cases → execute → auto-fix failures
- `/vespa-eval-fix` — Analyze test failures, trace to root cause, fix code, re-run

### Architecture

```
/vespa-eval-run                      /vespa-eval-fix
┌────────────────────┐              ┌────────────────────┐
│ 1. Plan (from diff)│              │ 1. Parse failures  │
│ 2. Select cases    │──failures──→ │ 2. Root cause      │
│ 3. Execute (K8s)   │              │ 3. Fix code        │
│ 4. Report          │←──results──  │ 4. Re-evaluate     │
└────────────────────┘              └────────────────────┘
         ↑                                    │
         └──── iterate until pass ────────────┘
```

## Test Conventions

- Tests inherit from `SearchTest`, `IndexedOnlySearchTest`, `ConfigTest`, etc.
- Each test class has `set_owner("name")` and `set_description("...")`
- Test methods start with `test_`
- App configs use Ruby DSL: `SearchApp.new.sd(...)`, `ConfigApp.new`, etc.

## When Fixing Test Failures

1. Fix the bug in source code, not by changing test expectations
2. Minimal, targeted changes only
3. Run the specific failing test to verify before committing
4. Never disable tests to make CI pass
