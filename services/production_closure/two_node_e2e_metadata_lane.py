from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

RUN_METADATA_SCHEMAS = frozenset(
    {
        "nhms.two_node_e2e.run.v1",
        "nhms.two_node_e2e.bundle.v1",
        "nhms.two_node_e2e.identity.v1",
    }
)
STRICT_IDENTITY_FIELDS = ("run_id", "source", "cycle_time", "model_id")
STRICT_LOG_IDENTITY_FIELDS = (*STRICT_IDENTITY_FIELDS, "job_id")
FULL_PASS_SOURCE_SET = frozenset({"GFS", "IFS"})
METADATA_DOCUMENT_CANDIDATES = (
    "run.json",
    "identity.json",
    "metadata.json",
    "cross-plane/run.json",
    "cross-plane/identity.json",
)
METADATA_LANE_OWNER = "services.production_closure.two_node_e2e_metadata_lane"
METADATA_LANE_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"'
)
METADATA_LANE_GUARD_SYMBOLS = (
    "METADATA_DOCUMENT_CANDIDATES",
    "RUN_METADATA_SCHEMAS",
    "STRICT_IDENTITY_FIELDS",
    "STRICT_LOG_IDENTITY_FIELDS",
    "FULL_PASS_SOURCE_SET",
    "MetadataScope",
    "MetadataLaneEvaluation",
    "evaluate_metadata_lane",
    "resolve_metadata_scope",
    "evaluate_metadata",
    "resolve_strict_identities",
    "strict_identity_metadata_issues",
)


class EvidenceDocumentLike(Protocol):
    path: Path
    payload: Mapping[str, Any]
    sha256: str


class LaneResultLike(Protocol):
    status: str


LaneResultT_co = TypeVar("LaneResultT_co", bound=LaneResultLike, covariant=True)


class BlockerFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class FindingFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class MissingLaneAdapter(Protocol[LaneResultT_co]):
    def __call__(self, name: str, code: str) -> LaneResultT_co: ...


class LaneFromStatusAdapter(Protocol[LaneResultT_co]):
    def __call__(
        self,
        name: str,
        doc: EvidenceDocumentLike,
        *,
        status: str,
        summary_status: str,
        blockers: Sequence[Mapping[str, Any]] = (),
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> LaneResultT_co: ...


class NormalizedStatus(Protocol):
    def __call__(self, value: Any, *, pass_aliases: Sequence[str] = (STATUS_PASS,)) -> str: ...


class CombinedStatus(Protocol):
    def __call__(
        self,
        statuses: Sequence[str],
        *,
        findings: Sequence[Mapping[str, Any]] = (),
        blockers: Sequence[Mapping[str, Any]] = (),
    ) -> str: ...


class ExplicitBundleRunIds(Protocol):
    def __call__(self, payload: Mapping[str, Any]) -> list[tuple[str, Any]]: ...


class NestedGet(Protocol):
    def __call__(self, payload: Mapping[str, Any], keys: Sequence[str]) -> Any: ...


LaneResultT = TypeVar("LaneResultT", bound=LaneResultLike)


@dataclass(frozen=True)
class MetadataScope:
    declared_sources: tuple[str, ...]
    reduced_scope: bool
    reduced_scope_declared: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared_sources": self.declared_sources,
            "reduced_scope": self.reduced_scope,
            "reduced_scope_declared": self.reduced_scope_declared,
        }


@dataclass(frozen=True)
class MetadataLaneEvaluation(Generic[LaneResultT]):
    doc: EvidenceDocumentLike | None
    metadata: Mapping[str, Any]
    scope: MetadataScope
    lane: LaneResultT
    strict_identities: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class MetadataLaneEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: NormalizedStatus
    combined_status: CombinedStatus
    blocker: BlockerFactory
    finding: FindingFactory
    explicit_bundle_run_ids: ExplicitBundleRunIds
    nested_get: NestedGet
    sources_from_value: Callable[[Any], tuple[str, ...]]
    source_name: Callable[[Any], str | None]
    identity_value: Callable[[Mapping[str, Any], str], str | None]
    optional_bool: Callable[[Any], bool | None]


def evaluate_metadata_lane(
    doc: EvidenceDocumentLike | None,
    metadata: Mapping[str, Any],
    *,
    evidence_run_id: str,
    configured_declared_sources: tuple[str, ...],
    configured_reduced_scope: bool | None,
    helpers: MetadataLaneEvaluationHelpers[LaneResultT],
) -> MetadataLaneEvaluation[LaneResultT]:
    scope = resolve_metadata_scope(
        configured_declared_sources=configured_declared_sources,
        configured_reduced_scope=configured_reduced_scope,
        metadata=metadata,
        helpers=helpers,
    )
    lane = evaluate_metadata(
        doc,
        metadata,
        evidence_run_id=evidence_run_id,
        declared_sources=scope.declared_sources,
        helpers=helpers,
    )
    strict_identities = resolve_strict_identities(
        metadata if lane.status == STATUS_PASS else {},
        declared_sources=scope.declared_sources,
        helpers=helpers,
    )
    return MetadataLaneEvaluation(
        doc=doc,
        metadata=metadata,
        scope=scope,
        lane=lane,
        strict_identities=strict_identities,
    )


def resolve_metadata_scope(
    *,
    configured_declared_sources: tuple[str, ...],
    configured_reduced_scope: bool | None,
    metadata: Mapping[str, Any],
    helpers: MetadataLaneEvaluationHelpers[Any],
) -> MetadataScope:
    declared = configured_declared_sources
    if not declared:
        declared = helpers.sources_from_value(
            metadata.get("declared_sources")
            or metadata.get("sources")
            or metadata.get("source_scope")
            or metadata.get("source_scope_results")
        )
    if not declared:
        declared = helpers.sources_from_value(
            metadata.get("strict_identities")
            or metadata.get("source_identities")
            or helpers.nested_get(metadata, ("strict_identity", "sources"))
        )
    reduced_scope_value = configured_reduced_scope
    if reduced_scope_value is None:
        reduced_scope_value = helpers.optional_bool(metadata.get("reduced_scope"))
    if reduced_scope_value is None:
        reduced_scope_value = str(metadata.get("scope") or "").lower() in {"reduced", "single_source"}
    reduced_declared = configured_reduced_scope is not None or "reduced_scope" in metadata or "scope" in metadata
    if declared and frozenset(declared) != FULL_PASS_SOURCE_SET:
        reduced_scope_value = True
    return MetadataScope(
        declared_sources=declared,
        reduced_scope=bool(reduced_scope_value),
        reduced_scope_declared=bool(reduced_declared),
    )


def resolve_strict_identities(
    metadata: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
    helpers: MetadataLaneEvaluationHelpers[Any],
) -> dict[str, dict[str, Any]]:
    raw = (
        metadata.get("strict_identities")
        or metadata.get("source_identities")
        or helpers.nested_get(metadata, ("strict_identity", "sources"))
        or {}
    )
    identities: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        for source, value in raw.items():
            source_name = helpers.source_name(source)
            if source_name and isinstance(value, Mapping):
                identity = dict(value)
                identity.setdefault("source", source_name)
                identities[source_name] = identity
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, Mapping):
                source_name = helpers.source_name(value.get("source") or value.get("source_id"))
                if source_name:
                    identities[source_name] = dict(value)
    for source in declared_sources:
        identities.setdefault(source, {"source": source})
    return identities


def evaluate_metadata(
    doc: EvidenceDocumentLike | None,
    metadata: Mapping[str, Any],
    *,
    evidence_run_id: str,
    declared_sources: tuple[str, ...],
    helpers: MetadataLaneEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    if doc is None:
        return helpers.missing_lane("metadata", "TWO_NODE_E2E_METADATA_MISSING")
    schema = metadata.get("schema")
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    summary_status = str(metadata.get("status", STATUS_PASS))
    if schema not in RUN_METADATA_SCHEMAS:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_METADATA_SCHEMA_UNSUPPORTED",
                "Run metadata must use a recognized two-node E2E schema.",
                schema=schema,
                recognized_schemas=sorted(RUN_METADATA_SCHEMAS),
            )
        )
    metadata_declared_sources = helpers.sources_from_value(
        metadata.get("declared_sources")
        or metadata.get("sources")
        or metadata.get("source_scope")
    )
    if not metadata_declared_sources:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_METADATA_DECLARED_SOURCES_MISSING",
                "Run metadata must declare source scope.",
            )
        )
    elif set(metadata_declared_sources) != set(declared_sources):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_METADATA_DECLARED_SOURCES_MISMATCH",
                "Run metadata declared source scope must match the final configured scope.",
                metadata_declared_sources=list(metadata_declared_sources),
                configured_declared_sources=list(declared_sources),
            )
        )
    explicit_ids = helpers.explicit_bundle_run_ids(metadata)
    if not explicit_ids:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_METADATA_CURRENT_BUNDLE_ID_MISSING",
                "Run metadata must declare the current evidence bundle id.",
                expected_evidence_run_id=evidence_run_id,
            )
        )
    else:
        for key, value in explicit_ids:
            if str(value) != evidence_run_id:
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_METADATA_STALE_BUNDLE_ID",
                        "Run metadata belongs to a different evidence bundle.",
                        key=key,
                        evidence_run_id=value,
                        expected_evidence_run_id=evidence_run_id,
                    )
                )
    if not declared_sources:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DECLARED_SOURCES_MISSING",
                "Final evidence requires declared source scope.",
            )
        )
    identity_blockers, identity_findings = strict_identity_metadata_issues(
        metadata,
        declared_sources=declared_sources,
        helpers=helpers,
    )
    blockers.extend(identity_blockers)
    findings.extend(identity_findings)
    status = helpers.combined_status(
        [
            helpers.normalized_status(
                metadata.get("status", STATUS_PASS),
                pass_aliases=(STATUS_PASS, "ready", "current"),
            )
        ],
        findings=findings,
        blockers=blockers,
    )
    return helpers.lane_from_status(
        "metadata",
        doc,
        status=status,
        summary_status=summary_status,
        blockers=blockers,
        findings=findings,
    )


def strict_identity_metadata_issues(
    metadata: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
    helpers: MetadataLaneEvaluationHelpers[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    entries: list[tuple[str, dict[str, Any]]] = []
    raw = (
        metadata.get("strict_identities")
        or metadata.get("source_identities")
        or helpers.nested_get(metadata, ("strict_identity", "sources"))
    )
    if raw is None:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_METADATA_STRICT_IDENTITIES_MISSING",
                "Run metadata must include strict identities for declared sources.",
            )
        )
        return blockers, findings
    if isinstance(raw, Mapping):
        for source_key, value in raw.items():
            if not isinstance(value, Mapping):
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_INVALID",
                        "Strict identity entry must be an object.",
                        source_key=source_key,
                    )
                )
                continue
            entries.append((str(source_key), dict(value)))
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            if not isinstance(value, Mapping):
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_INVALID",
                        "Strict identity list entry must be an object.",
                        entry_index=index,
                    )
                )
                continue
            key = value.get("source") or value.get("source_id") or f"entry[{index}]"
            entries.append((str(key), dict(value)))
    else:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_METADATA_STRICT_IDENTITIES_INVALID",
                "Run metadata strict identities must be a mapping or list.",
            )
        )
        return blockers, findings
    declared_set = set(declared_sources)
    seen_embedded_sources: dict[str, str] = {}
    seen_keys: set[str] = set()
    for raw_key, identity in entries:
        source_from_key = helpers.source_name(raw_key)
        source_from_identity = helpers.source_name(identity.get("source") or identity.get("source_id"))
        if not source_from_key:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_SOURCE_KEY_MISSING",
                    "Strict identity entry key must identify a source.",
                    source_key=raw_key,
                )
            )
            continue
        if source_from_key in seen_keys:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_METADATA_DUPLICATE_SOURCE_KEY",
                    "Strict identity contains duplicate source keys.",
                    source=source_from_key,
                )
            )
        seen_keys.add(source_from_key)
        if source_from_identity is None:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_SOURCE_MISSING",
                    "Strict identity entry must declare its embedded source.",
                    source_key=source_from_key,
                )
            )
        elif source_from_identity != source_from_key:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_METADATA_SOURCE_KEY_MISMATCH",
                    "Strict identity source key must match its embedded source.",
                    source_key=source_from_key,
                    embedded_source=source_from_identity,
                )
            )
        if source_from_key not in declared_set:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_METADATA_UNDECLARED_STRICT_IDENTITY_SOURCE",
                    "Strict identity source is not declared in source scope.",
                    source=source_from_key,
                    declared_sources=list(declared_sources),
                )
            )
        if source_from_identity:
            previous_key = seen_embedded_sources.get(source_from_identity)
            if previous_key and previous_key != source_from_key:
                findings.append(
                    helpers.finding(
                        "TWO_NODE_E2E_METADATA_DUPLICATE_EMBEDDED_SOURCE",
                        "Strict identity embeds the same source under multiple keys.",
                        embedded_source=source_from_identity,
                        source_keys=[previous_key, source_from_key],
                    )
                )
            seen_embedded_sources.setdefault(source_from_identity, source_from_key)
        missing_fields = [field for field in STRICT_LOG_IDENTITY_FIELDS if not helpers.identity_value(identity, field)]
        if missing_fields:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_INCOMPLETE",
                    "Strict identity entry is incomplete.",
                    source=source_from_key,
                    missing_fields=missing_fields,
                )
            )
    for source in declared_sources:
        if source not in seen_keys:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_METADATA_DECLARED_SOURCE_IDENTITY_MISSING",
                    "Run metadata is missing strict identity for a declared source.",
                    source=source,
                )
            )
    return blockers, findings
