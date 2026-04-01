---
name: vespa-eval-fix
description: Analyze Vespa system test failures from evaluation results, identify root causes in Vespa source code, fix the issues, and re-run the failed tests to verify. Use when eval results show failures.
argument-hint: "[eval-result-json-or-failure-description]"
allowed-tools: Read, Edit, Grep, Glob, Bash(python -m vespa_evaluator *), Bash(ruby *), Bash(git diff*), Bash(git log*)
---

# Vespa Eval Fix — Failure Analysis & Iterative Repair

You are the **Evaluator** half of a Generator⇄Evaluator loop (GAN-style,
per Anthropic's harness design). Your job: analyze test failures, trace them
to root causes in the Vespa codebase, apply targeted fixes, and verify.

## Input

Evaluation result or failure description: $ARGUMENTS

## Phase 1: Understand the Failure

1. **Parse the evaluation output**
   - If $ARGUMENTS is a file path, read it as JSON (the output of `python -m vespa_evaluator evaluate`)
   - Extract all entries with `"status": "failed"` or `"status": "error"`
   - Note the `test_fqn`, `test_method`, `error_message`, and `stdout` for each

2. **Classify each failure**
   - **Test logic failure**: assertion failed → the code under test has a bug
   - **Infrastructure error**: pod timeout, connection refused → environment issue, skip fixing
   - **Generated test issue**: if `test_fqn` contains "generated" → the generated test may be wrong

3. **Prioritize**: Fix test logic failures first. Skip infra errors. Flag generated test issues.

## Phase 2: Root Cause Analysis

For each test logic failure:

1. **Read the failing test case**
   - Use the `file_path` from the evaluation result to find the test source
   - Understand what the test asserts (hitcount, result ordering, ranking scores, etc.)

2. **Trace to Vespa source code**
   - The test's assertions tell you what behavior is expected
   - Use Grep to search for relevant Vespa components in the codebase
   - Check `git diff` to see recent changes that may have introduced the regression

3. **Identify the root cause**
   - Map the failure to a specific code change or logic error
   - Document: "Test X fails because change Y in file Z broke assumption W"

## Phase 3: Fix

1. **Apply minimal, targeted fixes**
   - Fix the actual bug, not the test (unless the test expectation is wrong)
   - Each fix should be small and focused — one logical change per file
   - Do NOT refactor surrounding code or add unrelated improvements

2. **Verify locally if possible**
   - If the fix is to a Ruby test helper or configuration, check syntax
   - If the fix is to application config (`.sd` files, `services.xml`), validate structure

## Phase 4: Re-Evaluate

1. **Re-run only the failed tests**
   ```
   python -m vespa_evaluator evaluate --plan <original-plan> --tests-dir tests --max-cases 10
   ```
   Or run a specific test directly if applicable.

2. **Check results**
   - If all previously-failed tests now pass → report success
   - If some still fail → go back to Phase 2 for remaining failures
   - Maximum 3 iterations to avoid infinite loops

3. **Report**
   Summarize:
   - How many failures were fixed
   - What changes were made (file + description)
   - Any remaining failures that need human attention
   - Whether any generated tests were incorrect (and why)

## Sprint Contract (Completion Criteria)

This fix cycle is complete when:
- [ ] All test logic failures are addressed (fixed or documented as won't-fix with reason)
- [ ] Infrastructure errors are reported but not blocking
- [ ] Changes are committed with clear messages
- [ ] Re-evaluation shows improvement (pass rate increased)

## Anti-patterns to Avoid

- Do NOT disable or skip failing tests to make them "pass"
- Do NOT modify test assertions to match buggy behavior
- Do NOT make broad changes to unrelated code
- Do NOT add workarounds; fix root causes
- Do NOT spend more than 3 iterations on a single failure
