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

### Topology Profiles

Tests can run at different fidelity levels depending on change risk:

| Profile | Pods | Layout | Use for |
|---|---|---|---|
| `single-node` | 1 | All-in-one | Fast functional tests, schema validation |
| `minimal-split` | 3 | 1 CS + 1 QRS + 1 Content | Basic distribution tests |
| `production-like` | 11 | 3 CS + 2 QRS + 6 Content | Failover, redistribution, rolling upgrade |
| `full-scale` | 33 | 3 CS + 2 QRS + 28 Content | Production shadow (matches real topology) |

Auto-selected by `recommend_profile(risk_level, num_components)`:
- low → single-node, medium → minimal-split, high/critical → production-like
- full-scale is manual-only

Production reference: ConfigServer × 3, Content × 28 (single replica).

### Permission Model

Four levels with increasing blast radius (see `.claude/settings.json`):

| Level | Operations | Control |
|---|---|---|
| **Auto-allow** | scan, select, read code, git diff/log | No confirmation |
| **Hook-confirm** | evaluate (creates pods), CMDB allocate, visit live data, git push | Human confirms each |
| **Deny-listed** | kubectl outside eval namespace, force push, push main, rm -rf | Blocked entirely |
| **K8s hardened** | ResourceQuota (20 pods/80 CPU/80Gi), NetworkPolicy (Vespa ports only), RBAC (pod CRUD only) | Infra-level |

### Deployment & CI/CD Considerations

**Docker images are CI/CD-built** — the test runner image (`vespaengine/vespa-systemtest-runner`)
is produced by the CI pipeline, not built locally. This matters for evaluator execution:

- **In CI (preferred path)**: Evaluator runs as a CI job, images are available in the
  registry, K8s cluster is on the same network as Vespa services. This is the happy path.
- **Local dev**: Images may not be available locally. Network to internal Vespa clusters
  may not be routable. Use `--dry-run` mode or point to a dev K8s cluster with VPN access.
- **Image freshness**: The evaluator should use the image tag matching the branch under test,
  not `latest`. CI should pass the image tag as a parameter to the evaluator.

Recommended CI integration:

```yaml
# In your CI pipeline (e.g., GitHub Actions, Jenkins):
- name: Build test runner image
  run: docker build -t vespa-systemtest-runner:${{ github.sha }} .

- name: Run evaluator
  run: |
    python -m vespa_evaluator evaluate \
      --plan /tmp/plan.json \
      --tests-dir tests \
      --image vespa-systemtest-runner:${{ github.sha }} \
      --output /tmp/results.json
```

Key networking requirements for execution environment:
- Access to K8s API server (for pod creation in `vespa-eval` namespace)
- Access to Vespa config server (port 19071) from test pods
- Access to container/QRS (port 8080) and content nodes from test pods
- Access to CMDB API (if using resource allocation)
- Access to production Vespa (if using live data visit — read-only)

When running outside CI, the evaluator gracefully degrades:
- No `kubernetes` package → dry-run mode (no pods, simulated results)
- No CMDB access → skip resource allocation, use default tier specs
- No production Vespa access → skip live data, use only existing test cases

### TODO — Next Session

1. **TopologyManager**: Use CMDB nodes to build split-deployed clusters
   (ConfigServer/QRS/Proton on separate pods) for Level 2 topology testing
2. **Multi-level eval strategy**: Level 1 (system test) → Level 2 (topology) → Level 3 (production shadow)
3. **`--topology` flag** on `/vespa-eval-run` to select evaluation level
4. **Wire up real skills**: Replace placeholder CMDB/visit providers with actual
   `/cmdb-query`, `/vespa-visit`, `/vespa-schema` skill implementations
5. **Real K8s validation**: Install kubernetes Python package, configure namespace/PVC,
   validate pod lifecycle end-to-end
6. **LLM plan generation**: Auto-generate Plan from git diff via Claude API
7. **CI pipeline integration**: Add evaluator step to CI workflow, pass image tag,
   publish results as PR comment

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
