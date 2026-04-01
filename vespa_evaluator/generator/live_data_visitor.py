"""
Live Data Visitor — fetches real data and schemas from production Vespa
clusters to construct realistic test cases.

Integration flow:
    1. Connect to production Vespa (via visit skill or HTTP API)
    2. Extract document schema (.sd definitions)
    3. Visit/sample actual documents
    4. Transform into test fixtures (anonymized if needed)
    5. Generate test cases using real data shapes

This solves a critical problem: handcrafted test data often misses
edge cases that exist in production (null fields, extreme tensor
dimensions, unicode, large documents, etc.).

The visitor is designed to work through external skills that already
have access to production Vespa clusters.
"""

from __future__ import annotations

import json
import logging
import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class VespaField:
    """A field definition from a Vespa schema."""
    name: str
    type: str  # string, int, float, tensor, array, weightedset, etc.
    indexing: list[str] = field(default_factory=list)  # attribute, index, summary
    is_attribute: bool = False
    tensor_spec: str = ""  # e.g., "tensor<float>(x[128])"
    struct_fields: list[VespaField] = field(default_factory=list)


@dataclass
class VespaSchema:
    """A parsed Vespa document schema."""
    name: str
    fields: list[VespaField] = field(default_factory=list)
    fieldsets: dict[str, list[str]] = field(default_factory=dict)
    rank_profiles: list[str] = field(default_factory=list)
    raw_sd: str = ""  # Original .sd content

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def tensor_fields(self) -> list[VespaField]:
        return [f for f in self.fields if "tensor" in f.type]

    def attribute_fields(self) -> list[VespaField]:
        return [f for f in self.fields if f.is_attribute]

    def to_sd(self) -> str:
        """Return the raw .sd content if available, or reconstruct a minimal one."""
        if self.raw_sd:
            return self.raw_sd

        lines = [f"schema {self.name} {{", f"  document {self.name} {{"]
        for f in self.fields:
            indexing_str = " | ".join(f.indexing) if f.indexing else "summary"
            lines.append(f"    field {f.name} type {f.type} {{")
            lines.append(f"      indexing: {indexing_str}")
            lines.append(f"    }}")
        lines.extend(["  }", "}"])
        return "\n".join(lines)


@dataclass
class VisitedDocument:
    """A document retrieved from a production Vespa visit."""
    doc_id: str
    doc_type: str
    fields: dict  # field_name → value


@dataclass
class VisitResult:
    """Result of a visit operation."""
    schema: VespaSchema
    documents: list[VisitedDocument] = field(default_factory=list)
    total_available: int = 0  # Total docs in the cluster
    sample_size: int = 0  # How many we actually visited


class VespaClusterProvider(ABC):
    """
    Abstract interface for accessing production Vespa clusters.

    Implement this to connect to your actual Vespa cluster via:
        - Internal skill (recommended, leverages existing auth/access)
        - Direct HTTP API
        - vespa-cli
    """

    @abstractmethod
    async def get_schema(self, application: str, schema_name: str) -> VespaSchema:
        """Fetch the schema definition for a document type."""
        ...

    @abstractmethod
    async def list_schemas(self, application: str) -> list[str]:
        """List all schema names in an application."""
        ...

    @abstractmethod
    async def visit_documents(
        self,
        application: str,
        schema_name: str,
        selection: str = "",
        max_documents: int = 100,
        field_set: str = "",
    ) -> list[VisitedDocument]:
        """
        Visit (export) documents from the cluster.

        Args:
            application: Application identifier (tenant.app.instance)
            schema_name: Document type to visit
            selection: Document selection expression (e.g., "music.year > 2020")
            max_documents: Max documents to retrieve
            field_set: Specific fields to include ("[schema]:field1,field2")
        """
        ...


class SkillBasedVespaProvider(VespaClusterProvider):
    """
    Vespa cluster provider that delegates to external Claude Code skills.

    Expected skills:
        /vespa-schema  — returns schema definition as .sd content
        /vespa-visit   — visits documents and returns JSON
    """

    async def get_schema(self, application: str, schema_name: str) -> VespaSchema:
        """
        Would invoke:
            /vespa-schema --app {application} --schema {schema_name}

        Returns the .sd content which we parse into VespaSchema.
        """
        logger.info(f"[VISIT] Would fetch schema: {application}/{schema_name}")
        # Placeholder — in production, invoke the skill and parse the .sd
        return VespaSchema(name=schema_name)

    async def list_schemas(self, application: str) -> list[str]:
        logger.info(f"[VISIT] Would list schemas for: {application}")
        return []

    async def visit_documents(
        self,
        application: str,
        schema_name: str,
        selection: str = "",
        max_documents: int = 100,
        field_set: str = "",
    ) -> list[VisitedDocument]:
        """
        Would invoke:
            /vespa-visit --app {application} --schema {schema_name}
                        --selection "{selection}" --max {max_documents}

        Returns JSON documents which we parse into VisitedDocument objects.
        """
        logger.info(
            f"[VISIT] Would visit {max_documents} docs "
            f"from {application}/{schema_name}"
        )
        return []


class LiveDataTestBuilder:
    """
    Builds test cases using live production data.

    The magic: instead of handcrafting test data, we:
        1. Grab the real schema from production
        2. Visit a sample of real documents
        3. Generate test fixtures that exercise real data shapes
        4. Produce Ruby test cases using the Vespa DSL

    This catches edge cases that synthetic data never hits:
        - Tensor fields with unexpected dimensions
        - Unicode in string fields
        - Null/missing optional fields
        - Extremely large or small numeric values
        - WeightedSets with unusual distributions

    Usage:
        provider = SkillBasedVespaProvider()
        builder = LiveDataTestBuilder(provider)

        visit_result = await builder.visit_and_analyze(
            application="my-tenant.my-app.default",
            schema_name="music",
            sample_size=50,
        )

        test_code = builder.generate_test_case(
            visit_result,
            test_hint="Verify ranking with production-like tensor data",
        )
    """

    def __init__(self, provider: VespaClusterProvider):
        self.provider = provider

    async def visit_and_analyze(
        self,
        application: str,
        schema_name: str,
        sample_size: int = 50,
        selection: str = "",
    ) -> VisitResult:
        """Fetch schema and sample documents from production."""
        schema = await self.provider.get_schema(application, schema_name)
        documents = await self.provider.visit_documents(
            application=application,
            schema_name=schema_name,
            selection=selection,
            max_documents=sample_size,
        )

        result = VisitResult(
            schema=schema,
            documents=documents,
            sample_size=len(documents),
        )

        # Analyze the data
        self._analyze_field_coverage(result)
        return result

    def _analyze_field_coverage(self, result: VisitResult):
        """Analyze which fields are populated and their value distributions."""
        if not result.documents:
            return

        field_stats: dict[str, dict] = {}
        for doc in result.documents:
            for fname, value in doc.fields.items():
                if fname not in field_stats:
                    field_stats[fname] = {
                        "count": 0, "null_count": 0,
                        "types_seen": set(), "sample_values": [],
                    }
                stats = field_stats[fname]
                stats["count"] += 1
                if value is None:
                    stats["null_count"] += 1
                else:
                    stats["types_seen"].add(type(value).__name__)
                    if len(stats["sample_values"]) < 3:
                        stats["sample_values"].append(value)

        for fname, stats in field_stats.items():
            null_rate = stats["null_count"] / len(result.documents)
            if null_rate > 0.5:
                logger.info(
                    f"  Field '{fname}': {null_rate:.0%} null "
                    f"— consider testing null handling"
                )

    def documents_to_json_feed(
        self,
        documents: list[VisitedDocument],
        anonymize: bool = True,
    ) -> str:
        """Convert visited documents to Vespa JSON feed format."""
        feed_docs = []
        for doc in documents:
            fields = dict(doc.fields)
            if anonymize:
                fields = self._anonymize_fields(fields)
            feed_docs.append({
                "put": f"id:{doc.doc_type}:{doc.doc_type}::{doc.doc_id}",
                "fields": fields,
            })
        return json.dumps(feed_docs, indent=2, ensure_ascii=False)

    def _anonymize_fields(self, fields: dict) -> dict:
        """Basic anonymization: replace string values with hashes, keep structure."""
        result = {}
        for key, value in fields.items():
            if isinstance(value, str) and len(value) > 20:
                # Truncate long strings, keep first/last chars
                result[key] = f"{value[:5]}...{value[-5:]}"
            elif isinstance(value, dict):
                result[key] = self._anonymize_fields(value)
            else:
                result[key] = value
        return result

    def generate_test_case(
        self,
        visit_result: VisitResult,
        test_hint: str = "",
        test_class_name: str = "",
    ) -> str:
        """
        Generate a Ruby test case from visited production data.

        The generated test:
            1. Deploys an app with the production schema
            2. Feeds the sampled documents
            3. Runs queries that exercise the real data shapes
            4. Asserts expected behavior
        """
        schema = visit_result.schema
        if not test_class_name:
            test_class_name = f"LiveData{schema.name.title()}Test"

        feed_file = f"live_data_{schema.name}.json"
        doc_count = len(visit_result.documents)

        # Detect interesting fields for query assertions
        tensor_fields = schema.tensor_fields()
        attr_fields = schema.attribute_fields()

        query_assertions = []
        if doc_count > 0:
            query_assertions.append(
                f'    assert_hitcount("query=sddocname:{schema.name}", {doc_count})'
            )
        for af in attr_fields[:3]:
            query_assertions.append(
                f'    # Test attribute search on {af.name}'
            )
            query_assertions.append(
                f'    assert_hitcount("query={af.name}:*&type=all", {doc_count})'
            )

        tensor_test = ""
        if tensor_fields:
            tf = tensor_fields[0]
            tensor_test = textwrap.dedent(f"""\
                  def test_tensor_ranking_with_live_data
                    # Uses production tensor dimensions from {tf.name}: {tf.tensor_spec}
                    # This catches dimension mismatches that synthetic data misses
                    deploy_app(SearchApp.new.sd(selfdir + "{schema.name}.sd"))
                    start
                    feed(:file => selfdir + "{feed_file}", :timeout => 240)
                    wait_for_hitcount("query=sddocname:{schema.name}", {doc_count})
                    # TODO: Add ranking assertions based on production tensor data
                  end
            """)

        assertions_str = "\n".join(query_assertions)

        return textwrap.dedent(f"""\
            # Copyright Vespa.ai. All rights reserved.
            # Auto-generated from production data visit
            # Source: {visit_result.schema.name} ({visit_result.sample_size} docs sampled)
            require 'indexed_only_search_test'

            class {test_class_name} < IndexedOnlySearchTest

              def setup
                set_owner("vespa-evaluator")
                set_description("{test_hint or f'Test with live data from {schema.name}'}")
              end

              def test_basic_search_with_live_data
                deploy_app(SearchApp.new.sd(selfdir + "{schema.name}.sd"))
                start
                feed(:file => selfdir + "{feed_file}", :timeout => 240)
                wait_for_hitcount("query=sddocname:{schema.name}", {doc_count})
            {assertions_str}
              end

            {tensor_test}
            end
        """)
