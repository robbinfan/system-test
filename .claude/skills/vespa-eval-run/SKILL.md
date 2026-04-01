---
name: vespa-eval-run
description: Run the Vespa evaluator pipeline — generate plan from git diff, select test cases, execute in K8s sandboxes, and auto-fix failures. The full Generator⇄Evaluator loop.
argument-hint: "[git-diff-or-plan-json-path]"
allowed-tools: Read, Edit, Grep, Glob, Bash(python -m vespa_evaluator *), Bash(git *), Bash(cat *), Skill(vespa-eval-fix)
---

# Vespa Eval Run — Full Evaluation Pipeline

You orchestrate the complete **Plan → Select → Execute → Fix** loop for
Vespa system test evaluation. This is the top-level entry point.

## Input

$ARGUMENTS — Either:
- A path to a plan JSON file (e.g., `examples/sample_plan.json`)
- A git ref or branch name to auto-generate a plan from diff
- Empty — will auto-detect changes from current branch vs main

## Step 1: Generate or Load Plan

### If $ARGUMENTS is a .json file:
```bash
cat $ARGUMENTS
```
Use it directly as the plan.

### If $ARGUMENTS is a git ref, or empty:
Analyze the diff to produce a plan:

```bash
git diff main...HEAD --stat
git diff main...HEAD
git log main..HEAD --oneline
```

From the diff, produce a plan JSON with:
- `summary`: One-line description of the change
- `changed_components`: List of components affected (map file paths to component types)
- `risk_level`: Assess based on scope of change
  - `low`: cosmetic, docs, test-only changes
  - `medium`: single component changes
  - `high`: cross-component changes, core logic
  - `critical`: data model changes, protocol changes
- `test_suggestions`: Map components to test categories:
  - Files in `searchcore/ranking/` → `{"category": "search", "area": "ranking"}`
  - Files in `config-model/` → `{"category": "config", "area": "deploy"}`
  - Files in `container-*/` → `{"category": "container"}`
  - Files in `storage/` → `{"category": "vds"}`
  - Files in `document-processing/` → `{"category": "docproc"}`
- `affected_areas`: Keywords for risk-based expansion search

Save the plan to `/tmp/vespa-eval-plan.json`.

## Step 2: Build Test Index (if needed)

```bash
python -m vespa_evaluator scan --tests-dir tests --output test_index.json
```

## Step 3: Preview Selection

```bash
python -m vespa_evaluator select --plan /tmp/vespa-eval-plan.json --index test_index.json
```

Review the selection. If it looks too broad or too narrow, adjust the plan:
- Too broad → remove low-priority suggestions, reduce risk_level
- Too narrow → add more affected_areas keywords

## Step 4: Execute

```bash
python -m vespa_evaluator evaluate \
  --plan /tmp/vespa-eval-plan.json \
  --tests-dir tests \
  --concurrency 10 \
  --output /tmp/vespa-eval-results.json
```

## Step 5: Analyze Results

Read `/tmp/vespa-eval-results.json` and check:
- **All passed** → Report success, done
- **Failures exist** → Invoke the fix skill:

```
/vespa-eval-fix /tmp/vespa-eval-results.json
```

This triggers the Generator⇄Evaluator loop:
1. `vespa-eval-fix` analyzes failures and applies fixes
2. `vespa-eval-fix` re-runs failed tests
3. Repeat up to 3 times

## Step 6: Final Report

After the fix loop completes, summarize:

### Evaluation Report
- **Plan**: (summary)
- **Cases selected**: N existing + M generated
- **Results**: X passed / Y failed / Z errors
- **Fixes applied**: List of changes made
- **Final pass rate**: N%
- **Remaining issues**: Any unfixed failures

### Files Changed
List all files modified during the fix cycle with brief descriptions.

## Component → Test Category Mapping Reference

| Source path pattern | change_type | Test category | Test area |
|---|---|---|---|
| `searchcore/**/ranking/**` | ranking | search | ranking |
| `searchcore/**/matching/**` | ranking | search | basicsearch |
| `searchlib/**/tensor/**` | ranking | search | tensor_eval |
| `config-model/**` | config | config | deploy |
| `configserver/**` | config | config | configserver |
| `container-*/**` | container | container | (all) |
| `storage/**` | storage | vds | (all) |
| `docproc/**` | feeding | docproc | (all) |
| `document/**` | schema | search | struct_and_map_types |
| `indexinglanguage/**` | feeding | search | feeding |
| `*.sd` (schema files) | schema | search | schemachanges |
| `services.xml` | config | config | deploy |
