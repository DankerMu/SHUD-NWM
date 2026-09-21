"""Report shapes: the finding record, the per-check dataclasses and the configs.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Holds the
``Literal`` axis/mode aliases, ``FindingSpec`` (the one shape every check emits)
and the frozen dataclasses the structural-budget, compatibility-facade,
scoped-agent-context, stale-route and topology families pass between their own
helpers, plus the two governed-scope config tuples built from them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scripts.governance.entropy_audit.constants import SCOPED_AGENT_CONTEXT_SPEC_PATH

FindingAxis = Literal["structure", "semantics", "behavior", "context", "protocol", "control"]


AuditMode = Literal["report", "hard-gate"]


AllowlistState = Literal["allowlisted", "unallowlisted"]


StructuralBudgetClass = Literal["mandatory-governance", "yellow-zone"]


@dataclass(frozen=True)
class FindingSpec:
    check_id: str
    title: str
    axis: FindingAxis
    governance_face: str
    role: str
    evidence_path: str
    severity: Literal["low", "medium", "high"]
    priority: Literal["P0", "P1", "P2", "P3"]
    owner_area: str
    description: str
    recommendation: str
    module: str
    allowlist_reason: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class _StructuralFileExemption:
    family: str
    reason: str


@dataclass(frozen=True)
class _StructuralBudgetFile:
    relative_path: str
    line_count: int
    line_count_is_truncated: bool
    line_count_lower_bound: int
    size_bytes: int | None
    module: str
    budget_class: StructuralBudgetClass
    import_families: tuple[str, ...]
    ownership_surface_signals: tuple[str, ...]
    review_reason: str
    owner_action: str


@dataclass(frozen=True)
class _StructuralBudgetExemption:
    relative_path: str
    line_count: int
    line_count_is_truncated: bool
    line_count_lower_bound: int
    size_bytes: int | None
    module: str
    exemption_family: str
    exemption_reason: str


@dataclass(frozen=True)
class _StructuralUnknownLineCountFile:
    relative_path: str
    line_count: int
    line_count_is_truncated: bool
    line_count_lower_bound: int
    size_bytes: int | None
    module: str
    review_reason: str
    owner_action: str


@dataclass(frozen=True)
class _StructuralOwnershipGrowthSignal:
    relative_path: str
    module: str
    line_count: int
    signal_type: str
    detail: str
    owner_action: str


@dataclass(frozen=True)
class _CompatibilityFacadeConfig:
    name: str
    relative_path: str
    inventory_path: str


@dataclass(frozen=True)
class _ScopedAgentContextConfig:
    scope_path: str
    instruction_path: str
    owner_area: str
    required_glossary_terms: tuple[str, ...]
    required_references: tuple[str, ...]
    required_verification_commands: tuple[str, ...]


@dataclass(frozen=True)
class _CompatibilityFacadeImportedSymbol:
    exposed_name: str
    imported_name: str
    module: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.exposed_name, self.module, self.imported_name)


@dataclass(frozen=True)
class _CompatibilityFacadeAlias:
    exposed_name: str
    owner_module: str
    owner_attr: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.exposed_name, self.owner_module, self.owner_attr)


@dataclass(frozen=True)
class _CompatibilityFacadeDefinition:
    qualified_name: str
    simple_name: str
    kind: Literal["class", "function", "method"]
    line: int
    forwarding: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.qualified_name)


@dataclass(frozen=True)
class _CompatibilityFacadeSurface:
    import_families: tuple[str, ...]
    imported_symbols: tuple[_CompatibilityFacadeImportedSymbol, ...]
    aliases: tuple[_CompatibilityFacadeAlias, ...]
    definitions: tuple[_CompatibilityFacadeDefinition, ...]


@dataclass(frozen=True)
class _CompatibilityFacadeSignal:
    facade_name: str
    relative_path: str
    inventory_path: str
    signal_type: str
    message_key: str
    inventory_tokens: tuple[str, ...]
    line: int | None
    detail: str
    owner_action: str


@dataclass(frozen=True)
class _StructuralComparisonBase:
    requested: str | None
    requested_source: str
    resolved: str | None
    ref_kind: str
    status: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class _StructuralAddedLine:
    line_number: int | None
    text: str


@dataclass(frozen=True)
class _StructuralPhysicalLineCount:
    line_count: int
    line_count_is_truncated: bool
    line_count_lower_bound: int
    size_bytes: int | None


@dataclass(frozen=True)
class _BoundedGitBlobText:
    text: str
    truncated: bool


COMPATIBILITY_FACADE_CONFIGS = (
    _CompatibilityFacadeConfig(
        name="scheduler",
        relative_path="services/orchestrator/scheduler.py",
        inventory_path="docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
    ),
    _CompatibilityFacadeConfig(
        name="chain",
        relative_path="services/orchestrator/chain.py",
        inventory_path="docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
    ),
)


SCOPED_AGENT_CONTEXT_CONFIGS = (
    _ScopedAgentContextConfig(
        scope_path="services/orchestrator",
        instruction_path="services/orchestrator/AGENTS.md",
        owner_area="services/orchestrator",
        required_glossary_terms=(
            "active entrypoint",
            "compatibility facade",
            "current authority",
            "budget-counted finding",
        ),
        required_references=(
            SCOPED_AGENT_CONTEXT_SPEC_PATH,
            "docs/runbooks/two-node-deployment-overview.md",
        ),
        required_verification_commands=(
            "uv run pytest -q tests/test_entropy_audit_*.py",
            "openspec validate --all --strict --no-interactive",
        ),
    ),
    _ScopedAgentContextConfig(
        scope_path="services/production_closure",
        instruction_path="services/production_closure/AGENTS.md",
        owner_area="services/production_closure",
        required_glossary_terms=(
            "lane",
            "current authority",
            "historical evidence",
            "budget-counted finding",
        ),
        required_references=(
            SCOPED_AGENT_CONTEXT_SPEC_PATH,
            "docs/runbooks/node-27-bringup-checklist.md",
        ),
        required_verification_commands=(
            "uv run pytest -q tests/test_entropy_audit_*.py",
            "openspec validate --all --strict --no-interactive",
        ),
    ),
    _ScopedAgentContextConfig(
        scope_path="apps/api",
        instruction_path="apps/api/AGENTS.md",
        owner_area="apps/api",
        required_glossary_terms=(
            "active entrypoint",
            "current authority",
            "budget-counted finding",
            "gate-eligible finding",
        ),
        required_references=(
            SCOPED_AGENT_CONTEXT_SPEC_PATH,
            "docs/governance/ROLE_BOUNDARY.md",
            "docs/runbooks/qhh-backend-smoke.md",
        ),
        required_verification_commands=(
            "uv run pytest -q tests/test_entropy_audit_*.py tests/test_runtime_mode.py tests/test_api.py",
            "openspec validate --all --strict --no-interactive",
        ),
    ),
    _ScopedAgentContextConfig(
        scope_path="apps/frontend",
        instruction_path="apps/frontend/AGENTS.md",
        owner_area="apps/frontend",
        required_glossary_terms=(
            "active entrypoint",
            "legacy redirect alias",
            "current authority",
            "historical evidence",
        ),
        required_references=(
            SCOPED_AGENT_CONTEXT_SPEC_PATH,
            "openspec/specs/evidence-boundary-hardening/spec.md",
            "docs/runbooks/display-readonly-live-mvt.md",
        ),
        required_verification_commands=(
            "cd apps/frontend && pnpm test",
            "cd apps/frontend && pnpm build",
            "uv run pytest -q tests/test_entropy_audit_*.py",
            "openspec validate --all --strict --no-interactive",
        ),
    ),
)


@dataclass(frozen=True)
class _StructuralAddedLines:
    lines: tuple[_StructuralAddedLine, ...]
    truncated: bool
    truncation_reason: str | None = None


@dataclass(frozen=True)
class _StaleRouteClauseAnalysis:
    route_valued_match_starts: tuple[int, ...]
    route_valued_token_spans: frozenset[tuple[int, int]]
    redirect_word_spans: tuple[tuple[int, int], ...]
    active_instruction_context: bool
    active_route_valued_context: bool


@dataclass(frozen=True)
class _StaleRouteMentionFacts:
    clause_index: int
    left: int
    right: int
    arrow_shape: str
    route_valued: bool
    redirect_local: bool
    semantic_key: str


@dataclass(frozen=True)
class _StaleRouteLineContext:
    line: str
    clause_ranges: tuple[tuple[int, int], ...]
    clause_starts: tuple[int, ...]
    clause_texts: tuple[str, ...]
    clause_has_per_mention_redirect_syntax: tuple[bool, ...]
    clause_analyses: tuple[_StaleRouteClauseAnalysis, ...]
    mention_facts: dict[tuple[int, int], _StaleRouteMentionFacts]
    redirect_governing_texts: tuple[str, ...]
    mention_governing_texts: tuple[str, ...]
    governing_text: str
    has_historical_route_authority_banner: bool
    document_has_historical_route_authority_banner: bool


@dataclass(frozen=True)
class _StaleRouteMentionContext:
    clause: str
    explicit_redirect_text: str
    redirect_text: str
    governing_text: str
    has_historical_route_authority_banner: bool
    document_has_historical_route_authority_banner: bool


@dataclass(frozen=True)
class _StaleRouteStructuralContext:
    governing_text: str
    redirect_governing_text: str


@dataclass(frozen=True)
class _ArchiveStatusMarkerRange:
    start_line: int
    end_line: int


@dataclass(frozen=True)
class _TopologyDisplayEnvSource:
    line_no: int
    line: str


@dataclass(frozen=True)
class _TopologyDisplayEnvWriterUse:
    line_no: int
    line: str


@dataclass(frozen=True)
class _TopologyDisplayEnvFacts:
    sources: tuple[_TopologyDisplayEnvSource, ...]
    writer_uses: tuple[_TopologyDisplayEnvWriterUse, ...]


@dataclass(frozen=True)
class _StaleRouteLineMatch:
    line_no: int
    line: str
    token: str
    token_start: int
    token_end: int
    context: _StaleRouteMentionContext


_StaleRouteDuplicateKey = tuple[str, object, str, str, bool, bool]
