---
name: vespa-eval-gendata
description: Generate realistic test cases by visiting production Vespa data. Fetches live schemas and documents, anonymizes them, and produces Ruby test cases with real data shapes.
argument-hint: "<application> <schema-name> [--sample-size 50] [--selection 'expr']"
allowed-tools: Read, Edit, Grep, Glob, Bash(python *), Skill(vespa-visit), Skill(vespa-schema)
---

# Vespa Eval GenData — Production Data → Test Cases

Generate test cases that use **real production data shapes** instead of
handcrafted synthetic data. This catches edge cases that are invisible
to manually written tests.

## Input

- `$0` — Application identifier (e.g., `my-tenant.my-app.default`)
- `$1` — Schema/document type name (e.g., `music`, `product`)
- `--sample-size N` — Number of documents to sample (default: 50)
- `--selection 'expr'` — Optional Vespa selection expression to filter documents

## Step 1: Fetch Schema

Use the vespa-schema skill to get the live schema definition:

```
/vespa-schema --app $0 --schema $1
```

Parse the `.sd` output and note:
- All field names and types
- Tensor field specifications (dimensions, cell type)
- Which fields are attributes vs indexed
- Rank profiles defined
- Struct/map field structures

Save the schema to `tests/search/generated_live/$1.sd`

## Step 2: Visit Documents

Use the vespa-visit skill to sample real documents:

```
/vespa-visit --app $0 --schema $1 --max {sample_size} --selection "{selection}"
```

For each returned document:
1. **Anonymize sensitive fields**: Replace PII-like strings with hashed versions
   - Keep first/last 2 chars of strings for debugging: `"John Smith"` → `"Jo...th"`
   - Preserve numeric values (important for ranking)
   - Keep tensor structure and dimensions intact (critical for correctness)
   - Keep weightedset keys but randomize weights if they look like user data
2. **Preserve data shape**: The point is to test real shapes, not real content
3. **Convert to Vespa JSON feed format**

Save feed to `tests/search/generated_live/$1.json`

## Step 3: Analyze Data Patterns

Before generating tests, analyze the sampled documents for interesting patterns:

- **Null field rate**: Fields that are >50% null need null-handling tests
- **Tensor sparsity**: Sparse tensors need different test strategies
- **Value ranges**: Extreme values (very large/small numbers, empty strings)
- **Type mismatches**: Fields where runtime types don't match schema declaration
- **Cardinality**: High-cardinality vs low-cardinality fields

Log these findings — they drive which test assertions to generate.

## Step 4: Generate Test Case

Create a Ruby test case at `tests/search/generated_live/test_$1_live.rb`:

```ruby
require 'indexed_only_search_test'

class LiveData{SchemaName}Test < IndexedOnlySearchTest

  def setup
    set_owner("vespa-evaluator")
    set_description("Test with live data from {application}/{schema}")
  end

  def test_basic_search_with_live_data
    deploy_app(SearchApp.new.sd(selfdir + "{schema}.sd"))
    start
    feed(:file => selfdir + "{schema}.json", :timeout => 240)
    wait_for_hitcount("query=sddocname:{schema}", {doc_count})
    # Generated assertions based on data analysis
  end

  def test_null_field_handling
    # For fields with high null rates
  end

  def test_tensor_operations
    # For tensor fields with production dimensions
  end
end
```

## Step 5: Validate & Report

1. Check that the generated `.sd` file is syntactically valid
2. Check that the JSON feed file is valid JSON
3. Report:
   - Schema: N fields (M tensor, K attribute)
   - Documents: N sampled, M anonymized
   - Tests generated: list of test methods
   - Data patterns found: null rates, tensor shapes, etc.

## When to Use This

- **Before schema changes**: Visit current production data, generate tests,
  then verify the schema change doesn't break existing data
- **For ranking changes**: Production tensor data has real distributions
  that synthetic data doesn't match
- **For new features**: Generate baseline tests from production to ensure
  the feature works with real data, not just toy examples
- **Regression testing**: Periodically refresh test data from production
  to catch drift between test fixtures and reality
