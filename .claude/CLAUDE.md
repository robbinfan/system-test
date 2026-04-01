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

- `/vespa-eval-run` — Full pipeline: plan → CMDB allocate → select cases → visit live data → execute → auto-fix
- `/vespa-eval-fix` — Analyze test failures, trace to root cause, fix code, re-run (max 3 iterations)
- `/vespa-eval-gendata <app> <schema>` — Visit production data, generate realistic test cases

These skills can chain external skills for infrastructure and data access:
- `cmdb-*` skills — Query CMDB for idle nodes, allocate/release resources
- `vespa-schema` / `vespa-visit` — Fetch live schemas and documents from production

### Architecture

```
/vespa-eval-run
┌──────────────────────────────────────────────────────────────┐
│ 1. Plan (from git diff)                                      │
│ 2. CMDB → allocate nodes by tier (SMALL/MED/LARGE/XLARGE)   │
│ 3. Select existing cases from 737+ test index                │
│ 4. Visit production data → generate realistic cases          │
│ 5. Execute in K8s Pods (actor-per-case, parallel)            │
│ 6. Failures? → /vespa-eval-fix (GAN-style loop, max 3x)     │
│ 7. Release CMDB resources, report                            │
└──────────────────────────────────────────────────────────────┘

  Generator⇄Evaluator loop (GAN-style):
  ┌─────────────┐        ┌──────────────┐
  │ eval-run    │──fail─→│ eval-fix     │
  │ (Generator) │        │ (Evaluator)  │
  │ select/gen  │←─fix── │ analyze/fix  │
  └─────────────┘        └──────────────┘
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
