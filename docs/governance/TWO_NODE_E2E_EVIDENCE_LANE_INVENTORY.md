# Two-Node E2E Evidence Lane Inventory

Snapshot date: 2026-06-25

Scope: Governance-7 issue #672 inventory for
`services/production_closure/two_node_e2e_evidence.py` and Governance-8 issue
`#732` shared-contract guard metadata plus issue `#733` metadata lane owner
extraction, issue `#734` Docker preflight lane owner extraction, and issue
`#735` Docker security lane owner extraction, and issue `#736` readonly DB
lane owner extraction, and issue `#737` simple-live Slurm/compute/display lane
owner extraction, issue `#738` API proof lane owner extraction, issue `#739`
browser proof lane owner extraction, and issue `#740` logs lane owner
extraction, issue `#741` manual ops lane owner extraction, and issue `#742`
cross-plane/source-scope aggregation owner extraction, and issue `#743` final
aggregation owner extraction. This page records the
production-closure two-node E2E evidence lane contracts that future extraction
work can use without making product decisions.

This inventory is governance evidence for lane ownership. Governance-8 rows may
record extraction ownership, but they do not change product behavior,
blocker/status semantics, follow-up #673 lane inventories, or write
`.entropy-baseline/latest.json`.

## Authority

This page is a companion inventory for
`openspec/changes/governance-7-structural-entropy-controls/`. When it
disagrees with executable behavior,
`services/production_closure/two_node_e2e_evidence.py` wins. When it disagrees
with governance policy, `docs/governance/entropy-budget.md` and the active
OpenSpec change win.

The stable public entrypoint for this slice remains
`validate_two_node_e2e_evidence(config)` and the module CLI in
`services/production_closure/two_node_e2e_evidence.py`. Future lane modules
must sit behind that entrypoint until equivalence is proven.

## Evidence Commands

Issue #672 lists these verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py
openspec validate governance-7-structural-entropy-controls --strict --no-interactive
```

Additional PR hygiene evidence:

```bash
git diff --check
```

Governance-8 issue #732 shared-contract guard verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs or log_uri or redaction or evidence_root or path_safety or stale"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #733 metadata lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #734 Docker preflight lane extraction verification
commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #735 Docker security lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #736 readonly DB lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #737 simple-live lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #738 API proof lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #739 browser proof lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #740 logs lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #741 manual ops lane extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "manual_ops"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #742 cross-plane/source-scope aggregation extraction
verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "cross_plane or source_scope or reduced_scope"
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Governance-8 issue #743 final aggregation extraction verification commands:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py -k "final or redaction or evidence_root or stale"
uv run pytest -q tests/test_two_node_e2e_evidence.py
uv run ruff check services/production_closure tests/test_two_node_e2e_evidence.py
openspec validate governance-8-module-deepening --strict --no-interactive
git diff --check
```

Read-only inventory context was collected from:

```bash
rg -n "FINAL_REQUIRED_LANES|LaneEvaluation|STRICT_LOG_IDENTITY_FIELDS|FULL_PASS_SOURCE_SET|_load_lane_documents|evaluate_metadata_lane|resolve_metadata_scope|resolve_strict_identities|evaluate_docker_preflight|evaluate_docker_security|evaluate_readonly_db|evaluate_api_lane|evaluate_browser_lane|evaluate_logs_lane|evaluate_manual_ops_lane|evaluate_simple_live_lane|evaluate_cross_plane_lane|build_source_scope_results|CrossPlaneEvaluationHelpers|is_full_scope_sources|is_full_scope_pass|FINAL_AGGREGATION_|FINAL_EVIDENCE_SCHEMA|EvidenceWriter|build_final_summary|write_final_summary|final_status|collect_blockers_and_findings|metadata_summary|DOCKER_PREFLIGHT_|DOCKER_SECURITY_|READONLY_DB_|API_|BROWSER_|LOGS_|MANUAL_OPS_|CROSS_PLANE_|SIMPLE_LIVE_|SLURM_|COMPUTE_SUMMARY_|DISPLAY_SUMMARY_|_cross_plane_helpers|_final_aggregation_helpers|_final_status" services/production_closure/two_node_e2e_evidence.py services/production_closure/two_node_e2e_metadata_lane.py services/production_closure/two_node_e2e_docker_preflight.py services/production_closure/two_node_e2e_docker_security.py services/production_closure/two_node_e2e_readonly_db_lane.py services/production_closure/two_node_e2e_api_lane.py services/production_closure/two_node_e2e_browser_lane.py services/production_closure/two_node_e2e_logs_lane.py services/production_closure/two_node_e2e_manual_ops_lane.py services/production_closure/two_node_e2e_cross_plane_lane.py services/production_closure/two_node_e2e_simple_live_lane.py services/production_closure/two_node_e2e_final_aggregation.py
rg -n "TWO_NODE_E2E_[A-Z0-9_]+" services/production_closure/two_node_e2e_evidence.py services/production_closure/two_node_e2e_docker_preflight.py services/production_closure/two_node_e2e_docker_security.py services/production_closure/two_node_e2e_readonly_db_lane.py services/production_closure/two_node_e2e_api_lane.py services/production_closure/two_node_e2e_browser_lane.py services/production_closure/two_node_e2e_logs_lane.py services/production_closure/two_node_e2e_manual_ops_lane.py services/production_closure/two_node_e2e_cross_plane_lane.py services/production_closure/two_node_e2e_simple_live_lane.py services/production_closure/two_node_e2e_final_aggregation.py
rg -n "metadata|docker_preflight|docker_security|readonly_db|api|browser|logs|simple_lane|slurm|compute_summary|display_summary|manual_ops|cross_plane|source_scope_results|final|FINAL_AGGREGATION_GUARD_SYMBOLS|CROSS_PLANE_LANE_GUARD_SYMBOLS|build_source_scope_results|evaluate_cross_plane_lane|build_final_summary|write_final_summary|final_status|is_full_scope|lane_summaries" tests/test_two_node_e2e_evidence.py
```

## Non-Targets

- No #673 lane inventory in this slice.
- No implementation extraction in #672. Docker preflight extraction starts in
  #674.
- No new lane rows for implementation phases. This inventory records the
  current #672 two-node E2E evidence lane set only.
- No individual lane evaluator or final aggregation movement in #732. The
  shared-contract metadata guards existing aggregator-owned rules so later
  Governance-8 extraction issues can move lanes without duplicating current-run
  binding, strict identity, producer/source-artifact, redaction, path-safety, or
  log URI rules.
- No source proof lane, cross-plane aggregation, or final aggregation movement
  in #733. The stable `validate_two_node_e2e_evidence(config)` entrypoint still
  composes all lanes and final summaries.
- No Docker security child artifact, display readonly proof, or final
  aggregation movement in #734. Docker preflight remains limited to preflight
  document aliases, current-run binding, disk/command/resource checks,
  approved-root evidence path checks, DockerRootDir resource evidence, and
  blocker namespace ownership.
- No readonly DB lane, API/browser/logs lanes, or manual ops movement in #735.
  Docker security remains limited to child/source artifacts, display-readonly
  runtime proof, forbidden capability findings, readonly published/root
  filesystem proof, Docker security document aliases, and blocker namespace
  ownership.
- No Docker, readonly DB, API/browser/logs, manual ops, cross-plane/source-scope
  aggregation, DB schema/role, frontend/display route, Slurm scheduling, or
  final aggregation movement in #737. Simple live lane extraction only moves
  shared Slurm, compute summary, and display summary evaluator ownership.
- No weakening of path safety, redaction, readonly DB boundaries,
  current-run binding, producer/source-artifact proof, or final aggregation.

## Lane Set

The #672 runtime `lane_summaries` set is exactly the current
`FINAL_REQUIRED_LANES` closure. The cross-plane lane is in that required set,
but it is constructed after source-scope aggregation:

- metadata
- Docker preflight
- Docker security
- readonly DB
- API proof
- browser proof
- cross-plane
- manual ops receipts
- Slurm proof
- logs
- compute summary
- display summary

The inventory also covers shared or composed surfaces that are not independent
runtime lane summaries but are required extraction contracts for #672:

- producer identity / source artifacts
- source-scope / cross-plane aggregation
- final aggregation

## Shared Contract Guard Metadata

Governance-8 issue #732 records shared contract metadata in
`TWO_NODE_E2E_SHARED_CONTRACTS`. These are not independent runtime lanes; they
are aggregator-owned contracts that later lane owner modules must consume or
preserve behind `validate_two_node_e2e_evidence(config)`.

| Contract ID | Owner | Consumers | Guard symbols | Blocker/finding namespaces | Focused verification command |
|---|---|---|---|---|---|
| `lane-result-adapter` | `services.production_closure.two_node_e2e_evidence` | `metadata`, `docker_preflight`, `docker_security`, `readonly_db`, `api`, `browser`, `cross_plane`, `manual_ops`, `slurm`, `logs`, `compute_summary`, `display_summary` | `LaneEvaluation`, `LaneEvaluation.to_summary`, `validate_two_node_e2e_evidence`, `FINAL_REQUIRED_LANES`, `STATUS_PASS`, `STATUS_PARTIAL`, `STATUS_FAIL`, `STATUS_BLOCKED` | `TWO_NODE_E2E_LANE_`, `TWO_NODE_E2E_SOURCE_`, `TWO_NODE_E2E_EVIDENCE_` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"` |
| `current-run-binding` | `services.production_closure.two_node_e2e_evidence` | `metadata`, `docker_preflight`, `docker_security`, `readonly_db`, `api`, `browser`, `cross_plane`, `manual_ops`, `slurm`, `logs`, `compute_summary`, `display_summary` | `CURRENT_EVIDENCE_RUN_ID_KEYS`, `_current_run_blockers`, `_recursive_current_run_blockers`, `_explicit_bundle_run_ids`, `_explicit_bundle_run_ids_from_value` | `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"` |
| `producer-source-artifacts` | `services.production_closure.two_node_e2e_evidence` | `docker_preflight`, `docker_security`, `readonly_db`, `api`, `browser`, `logs`, `cross_plane`, `manual_ops`, `slurm`, `compute_summary`, `display_summary` | `PRODUCER_EVIDENCE_KEYS`, `SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS`, `PRODUCER_AUTHORITATIVE_PROOF_CONTAINER_KEYS`, `PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS`, `_has_producer_backed_lane_evidence`, `_source_lane_check_producer_blockers`, `_source_scoped_producer_evidence_blockers`, `_producer_source_artifact_blockers`, `_producer_source_artifact_record_blockers` | `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_`, `CHECK_PRODUCER_EVIDENCE_MISSING`, `CHECK_PRODUCER_IDENTITY_` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"` |
| `strict-identity` | `services.production_closure.two_node_e2e_evidence` | `metadata`, `readonly_db`, `api`, `browser`, `logs`, `cross_plane`, `manual_ops` | `two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS`, `two_node_e2e_metadata_lane.STRICT_LOG_IDENTITY_FIELDS`, `LOG_URI_IDENTITY_FIELDS`, `two_node_e2e_metadata_lane.resolve_strict_identities`, `two_node_e2e_metadata_lane.strict_identity_metadata_issues`, `_strict_identity_value_matches`, `_record_identity` | `TWO_NODE_E2E_STRICT_IDENTITY_`, `TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"` |
| `approved-root-path-safety` | `services.production_closure.two_node_e2e_evidence` | `metadata`, `docker_preflight`, `docker_security`, `readonly_db`, `api`, `browser`, `cross_plane`, `manual_ops`, `slurm`, `logs`, `compute_summary`, `display_summary` | `APPROVED_EVIDENCE_ROOTS`, `EvidenceWriter`, `_safe_resolved_evidence_root`, `_read_json`, `_read_json_bytes`, `_refuse_symlink_components`, `_recorded_path_approval_blockers`, `_producer_source_artifact_record_blockers` | `TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED`, `TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE`, `TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs or log_uri or redaction or evidence_root or path_safety or stale"` |
| `redaction` | `services.production_closure.two_node_e2e_evidence` | `metadata`, `docker_preflight`, `docker_security`, `readonly_db`, `api`, `browser`, `cross_plane`, `manual_ops`, `slurm`, `logs`, `compute_summary`, `display_summary` | `LaneEvaluation.to_summary`, `EvidenceWriter.write_json`, `redact_payload`, `redact_text`, `_blocker`, `_finding` | `TWO_NODE_E2E_EVIDENCE_REDACTION_DEPTH_EXCEEDED`, `TWO_NODE_E2E_EVIDENCE_PAYLOAD_TOO_LARGE` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs or log_uri or redaction or evidence_root or path_safety or stale"` |
| `log-uri-safety` | `services.production_closure.two_node_e2e_evidence` | `logs`, `browser` | `LOG_URI_KEYS`, `LOG_URI_REQUIRED_IDENTITY_FIELDS`, `PUBLISHED_LOG_ROOT_KEYS`, `PUBLISHED_LOG_S3_BUCKET_KEYS`, `_published_log_uri_blockers`, `_published_log_uri_identity_blockers`, `_safe_log_relative_path_blockers`, `_safe_log_absolute_path_blockers`, `_safe_log_uri_summary`, `_unsafe_log_uri_summary` | `TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_`, `TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI`, `TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH` | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs or log_uri or redaction or evidence_root or path_safety or stale"` |

## Lane Contracts

| Lane / surface | Owner module plan | Input contract | Output/result shape | Blocker/finding code namespace | Focused verification command | Retention condition | Extraction readiness note |
|---|---|---|---|---|---|---|---|
| metadata | Current owner `services.production_closure.two_node_e2e_metadata_lane`; aggregator keeps discovery, final composition, summary writing, and downstream lane calls behind `validate_two_node_e2e_evidence(config)`. | Reads the first available `run.json`, `identity.json`, `metadata.json`, `cross-plane/run.json`, or `cross-plane/identity.json` via `METADATA_DOCUMENT_CANDIDATES`. PASS-compatible input statuses are `PASS`, `ready`, and `current`; PASS input must use a recognized run metadata schema, bind to the current evidence bundle, declare source scope, and include strict identities for every declared source with `run_id`, `source`, `cycle_time`, `model_id`, and `job_id`. | `evaluate_metadata_lane(...)` returns `MetadataLaneEvaluation` with `lane_summaries.metadata` data, `MetadataScope`, and strict identities. The stable public summary still exposes top-level `metadata` and `strict_identity`; lane summary shape remains `LaneEvaluation.to_summary`; strict identities are exposed only after metadata PASS and are redacted. | `TWO_NODE_E2E_METADATA_*`, `TWO_NODE_E2E_DECLARED_SOURCES_MISSING`, `TWO_NODE_E2E_SOURCE_STRICT_IDENTITY_INCOMPLETE`, and strict identity mismatch/incomplete namespaces propagated to consumers. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"` | Retain aggregator compatibility until downstream source-lane extractions consume the owner result without changing source-scope seeding, five-field identity, redaction, or final summary behavior. | Extracted in #733 as the first two-node E2E lane owner module after shared-contract guard metadata. Source proof lanes, cross-plane aggregation, and final aggregation remain separate future slices. |
| Docker preflight | Current owner `services.production_closure.two_node_e2e_docker_preflight`; aggregator keeps final composition and shared current-run/path helper injection behind `validate_two_node_e2e_evidence(config)`. | Reads the first available `docker-preflight/summary.json`, `docker-preflight/docker-preflight.json`, or `docker-preflight.json` via `DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES`. PASS input must use schema `nhms.two_node_docker.preflight.v1`, bind to the current evidence run, include `evidence_root`, `tmpdir`, `docker_root_dir`, `min_free_bytes`, `disk` entries for `evidence_root`, `tmpdir`, and `docker_root`, and command evidence for `docker_version`, `docker_compose_version`, `docker_info_docker_root`, `docker_system_df`, and `df_h`. Recorded `evidence_root` and `tmpdir` paths currently checked through shared helpers must stay under approved evidence roots; `docker_root_dir` remains required host resource evidence and can be a DockerRootDir such as `/var/lib/docker`. | `evaluate_docker_preflight(...)` returns the `lane_summaries.docker_preflight` lane result with `status`, `evidence_path`, `evidence_sha256`, `summary_status`, `blockers`, `findings`, and redacted evidence when present. PASS remains PASS only when the producer summary and recomputed contract checks agree; PASS plus blockers becomes BLOCKED. | `TWO_NODE_E2E_DOCKER_PREFLIGHT_*`, `TWO_NODE_E2E_DOCKER_ROOT_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID`, `TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"` | Retain aggregator compatibility until later Docker security/readiness slices consume shared current-run and path helper contracts without changing Docker preflight status, blocker, resource, command, or redaction behavior. | Extracted in #734 as a self-contained owner module. Docker security child artifacts, display readonly proof, and final aggregation remain separate future slices. |
| Docker security | Current owner `services.production_closure.two_node_e2e_docker_security`; aggregator keeps final composition and shared current-run/path/read helper injection behind `validate_two_node_e2e_evidence(config)`. | Reads the first available `docker-security/summary.json`, `docker-security/display-isolation.json`, `docker-security/docker-smoke.json`, `docker-smoke/docker-smoke.json`, or `docker-smoke.json` via `DOCKER_SECURITY_DOCUMENT_CANDIDATES`. PASS input must use schema `nhms.two_node_docker.security_summary.v1`, bind to the current run, include live Docker/container evidence, prove display runtime is `display_readonly`, prove Slurm routes and forbidden host capabilities are absent, prove published artifacts and root filesystem are readonly, and include source artifacts for `source_trust`, `static`, and `smoke` with schema, path, sha256, current-run, safe-read, and PASS subcontracts. | `evaluate_docker_security(...)` returns the `lane_summaries.docker_security` lane result with lane status plus redacted source artifact summaries. Missing proof or current-run mismatch blocks; forbidden capability, writer-like display runtime, writable published mount, or forbidden child finding fails. | `TWO_NODE_E2E_DOCKER_SECURITY_*`, `TWO_NODE_E2E_DOCKER_LIVE_CONTAINER_EVIDENCE_MISSING`, `TWO_NODE_E2E_DOCKER_SOURCE_TRUST_*`, `TWO_NODE_E2E_DOCKER_STATIC_*`, `TWO_NODE_E2E_DOCKER_SMOKE_*`, `TWO_NODE_E2E_DOCKER_DISPLAY_*`, `TWO_NODE_E2E_DISPLAY_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"` | Retain aggregator compatibility until later readonly/API/browser/log/manual slices consume shared helper contracts without changing Docker security status, blocker, finding, child-artifact, source-trust, readonly capability, or redaction behavior. | Extracted in #735 as a self-contained owner module. Readonly DB lane, API/browser/logs lanes, and manual ops remain separate future slices. |
| readonly DB | Current owner `services.production_closure.two_node_e2e_readonly_db_lane`; aggregator keeps discovery, final composition, and shared current-run/path/read/identity helper injection behind `validate_two_node_e2e_evidence(config)`. The producer-side merge helper remains in `services.production_closure.readonly_db_validation`. | Reads the first available `db/readonly-db-boundary/summary.json` or `db/summary.json` via `READONLY_DB_DOCUMENT_CANDIDATES`. PASS input must use schema `nhms.readonly_db_boundary.evidence.v1`, match the current `run_id`, include `validation_provenance.mode=live` and `live_readonly_proof=true`, include a redacted `database_url`, include readonly role evidence, include route smoke, permission probes, manual-action probes, authoritative sibling/source artifacts, and cover every declared source identity. Route smoke uses route-specific strict identity fields: `job_logs` requires `job_id`; the other identity-bound routes use `run_id`, `source`, `cycle_time`, and `model_id`. | `evaluate_readonly_db(...)` returns `lane_summaries.readonly_db` with recomputed status, blockers, findings, and redacted evidence. Mutating catalog privilege, writer role, successful mutation probe, stale source artifact, route identity mismatch, or missing source coverage prevents PASS according to blocker/finding severity. Both canonical and `db/summary.json` alias layouts preserve current-run parent binding. | `TWO_NODE_E2E_READONLY_DB_*`, plus shared strict identity and current-run namespaces where child evidence is compared. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"` | Retain aggregator compatibility until later API/browser/log/manual/final slices consume shared helper contracts without changing readonly DB status, blocker, finding, sibling/source-artifact, no-write, route-identity, or redaction behavior. | Extracted in #736 as a self-contained owner module. Simple-live lanes were extracted in #737; API/browser/logs lanes, manual ops, cross-plane/source-scope aggregation, and final aggregation remain separate future slices. |
| API proof | Current owner `services.production_closure.two_node_e2e_api_lane`; aggregator keeps final aggregation and `source_scope_results` composition behind `validate_two_node_e2e_evidence(config)`. Shared producer/source artifact/current-run/strict-identity helpers remain aggregator-owned shared contracts injected through `ApiLaneEvaluationHelpers`. | Reads the first available `api/summary.json` or `api/evidence.json` via `API_DOCUMENT_CANDIDATES`. PASS input must bind to the current run, set `API_LIVE_FLAG` (`live_api_evidence`), avoid mock or historical latest fallback, include producer-backed command/request/response/artifact proof, cover every declared source, and include PASS checks from `API_REQUIRED_CHECKS`: `latest_product`, `series`, `ops_status`, `ops_stages`, and `jobs`. API source/check strict matching currently uses `run_id`, `source`, `cycle_time`, and `model_id`; producer check proof also binds the check name and current evidence run. | `evaluate_api_lane(...)` returns `lane_summaries.api`; aggregator contributes that lane status, blockers, and findings to per-source `source_scope_results`. Source/check FAIL creates findings and FAIL; missing check, non-PASS check, missing source, stale run, or missing producer proof creates blockers and BLOCKED. | `TWO_NODE_E2E_API_*`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_STRICT_IDENTITY_*`, `TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"` | Retain aggregator final/source-scope composition until browser/logs/cross-plane/final extractions have stable lane result interfaces and prove unchanged full GFS/IFS and reduced-scope behavior. | Extracted in #738 as an API owner module. Shared producer/source artifact and strict identity contracts are deliberately retained as aggregator-owned shared contracts rather than duplicated in the API lane. |
| browser proof | Current owner `services.production_closure.two_node_e2e_browser_lane`; aggregator keeps final aggregation and `source_scope_results` composition behind `validate_two_node_e2e_evidence(config)`. Shared producer/source artifact/current-run/strict-identity helpers remain aggregator-owned shared contracts injected through `BrowserLaneEvaluationHelpers`. | Reads the first available `browser/summary.json` or `browser/evidence.json` via `BROWSER_DOCUMENT_CANDIDATES`. PASS input must bind to the current run, set `BROWSER_LIVE_FLAG` (`live_browser_evidence`), include producer-backed browser/network/artifact proof, avoid mock or historical latest fallback, cover every declared source, and include PASS checks from `browser_required_checks(...)`: `hydro_met`, `ops`, `ops_jobs`, `ops_job_logs`, plus `source_switch` only when multiple sources are declared. Browser source records use four-field strict matching; `ops_jobs` and `ops_job_logs` check proof additionally requires `job_id`. | `evaluate_browser_lane(...)` returns `lane_summaries.browser`; aggregator contributes that lane status, blockers, and findings to per-source `source_scope_results`. Output shape matches `LaneEvaluation.to_summary`; job-like checks must include `job_id` identity where required. | `TWO_NODE_E2E_BROWSER_*`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_STRICT_IDENTITY_*`, `TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"` | Retain aggregator final/source-scope composition until logs/cross-plane/final extractions have stable lane result interfaces and prove unchanged full GFS/IFS and reduced-scope behavior. | Extracted in #739 as a browser owner module. Shared producer/source artifact and strict identity contracts are deliberately retained as aggregator-owned shared contracts rather than duplicated in the browser lane. |
| logs | Current owner `services.production_closure.two_node_e2e_logs_lane`; aggregator keeps final aggregation and `source_scope_results` composition behind `validate_two_node_e2e_evidence(config)`. Shared producer/source artifact, current-run, strict-identity, redaction, and log URI safety helpers remain aggregator-owned shared contracts injected through `LogsLaneEvaluationHelpers`. | Reads the first available `logs/summary.json` or `logs/evidence.json` via `LOGS_DOCUMENT_CANDIDATES`. PASS input must bind to the current run, set `LOGS_LIVE_FLAG` (`live_log_evidence`), include producer-backed proof, cover every declared source, include PASS checks from `LOGS_REQUIRED_CHECKS` (`job_logs`), include strict log identity with `job_id`, and provide an allowed published log URI or typed published-log unavailable evidence. Log URI input must not include credentials, query strings, fragments, private workspace paths, or unsafe path components. | `evaluate_logs_lane(...)` returns `lane_summaries.logs`; aggregator contributes that lane status, blockers, and findings to per-source `source_scope_results`. Allowed outputs include published log proof or typed unavailable proof; unsafe/private URI, missing read evidence, identity mismatch, or missing job ID blocks PASS. | `TWO_NODE_E2E_LOGS_*`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_STRICT_IDENTITY_*`, `TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs"` | Retain aggregator final/source-scope composition until cross-plane/final extractions have stable lane result interfaces and prove unchanged full GFS/IFS and reduced-scope behavior. | Extracted in #740 as a logs owner module. Published log URI safety and typed unavailable proof semantics remain single-source shared contracts injected into the logs owner instead of being duplicated. |
| Slurm proof | Current owner `services.production_closure.two_node_e2e_simple_live_lane`; aggregator keeps final composition and shared current-run/producer helper injection behind `validate_two_node_e2e_evidence(config)`. | Reads the first available `slurm/summary.json` or `slurm/evidence.json` via `SLURM_DOCUMENT_CANDIDATES`. PASS input must bind to the current run, include `live_slurm_evidence`, include producer-backed proof, avoid stale nested bundle IDs, and avoid mock or deterministic fixture evidence. | `evaluate_simple_live_lane(SLURM_LANE_CONFIG, ...)` returns `lane_summaries.slurm` with `status`, evidence path, sha256, summary status, blockers, findings, and redacted evidence. Missing lane, stale run, missing live evidence, missing producer proof, or mock evidence prevents PASS. | `TWO_NODE_E2E_SLURM_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"` | Retain aggregator compatibility until later final/source contract extractions consume `SimpleLiveLaneEvaluationHelpers` without changing status normalization, current-run recursion, producer proof, mock rejection, or summary shape. | Extracted in #737 with compute and display summary as one focused simple-live owner. Slurm scheduling behavior remains out of scope. |
| compute summary | Current owner `services.production_closure.two_node_e2e_simple_live_lane`; aggregator keeps final composition and shared current-run/producer helper injection behind `validate_two_node_e2e_evidence(config)`. | Reads the first available `22-compute/summary.json`, `compute/summary.json`, or `compute-summary.json` via `COMPUTE_SUMMARY_DOCUMENT_CANDIDATES`. PASS-compatible input statuses are `PASS`, `ready`, and `submitted`; PASS input must bind to the current run, include `live_compute_evidence`, include producer-backed proof, avoid stale nested bundle IDs, and avoid mock or fixture evidence. | `evaluate_simple_live_lane(COMPUTE_SUMMARY_LANE_CONFIG, ...)` returns `lane_summaries.compute_summary` with `LaneEvaluation.to_summary` fields and redacted evidence. Missing lane, stale current-run binding, missing live evidence, missing producer proof, or mock evidence blocks or fails final PASS according to simple-live-lane semantics. | `TWO_NODE_E2E_COMPUTE_SUMMARY_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"` | Retain aggregator compatibility until later final/source contract extractions consume `SimpleLiveLaneEvaluationHelpers` without changing the 22-compute path aliases, `ready`/`submitted` pass aliases, current-run checks, producer proof, or redacted summary shape. | Extracted in #737 with Slurm/display summary as one focused simple-live owner. Node-22 compute semantics remain explicit. |
| display summary | Current owner `services.production_closure.two_node_e2e_simple_live_lane`; aggregator keeps final composition and shared current-run/producer helper injection behind `validate_two_node_e2e_evidence(config)`. | Reads the first available `27-display/summary.json`, `display/summary.json`, or `display-summary.json` via `DISPLAY_SUMMARY_DOCUMENT_CANDIDATES`. PASS-compatible input statuses are `PASS` and `ready`; PASS input must bind to the current run, include `live_display_evidence`, include producer-backed proof, avoid stale nested bundle IDs, and avoid mock or fixture evidence. | `evaluate_simple_live_lane(DISPLAY_SUMMARY_LANE_CONFIG, ...)` returns `lane_summaries.display_summary` with `LaneEvaluation.to_summary` fields and redacted evidence. Missing lane, stale current-run binding, missing live evidence, missing producer proof, or mock evidence blocks or fails final PASS according to simple-live-lane semantics. | `TWO_NODE_E2E_DISPLAY_SUMMARY_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"` | Retain aggregator compatibility until later final/source contract extractions consume `SimpleLiveLaneEvaluationHelpers` without changing the 27-display path aliases, `ready` pass alias, current-run checks, producer proof, or redacted summary shape. | Extracted in #737 with Slurm/compute summary as one focused simple-live owner. Node-27 display evidence stays separate from readonly DB and browser lanes. |
| producer identity / source artifacts | Future shared owner `services.production_closure.two_node_e2e_producer_contracts`; lanes consume it instead of duplicating proof walkers. | Input is embedded in source lanes and simple live lanes through `source_artifacts`, `commands`, `requests`, `responses`, `browser_artifacts`, `screenshots`, `network`, `artifacts`, `evidence`, or `proofs`. Source artifact records must include path or artifact path, sha256 or digest, current-run binding or current run directory placement, approved evidence root, safe bounded JSON, and matching nested run ID. Authoritative producer proof must not be only wrapper metadata, diagnostics, debug, or notes. Producer check identity binds `source`, `check`, `run_id`, `cycle_time`, and `model_id`; `job_logs`, `ops_jobs`, and `ops_job_logs` additionally require `job_id`. | Shared blockers/findings are attached to the consuming lane summary and final `blockers`/`findings`; no separate runtime lane summary exists today. The result shape for extraction is a reusable contract object containing `blockers`, `findings`, redacted producer evidence summary, and artifact records accepted/rejected. | `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"` | Retain as aggregator-local shared logic until at least two extracted lanes consume a single shared helper with identical blocker codes and redaction. | Ready as a shared-contract extraction, not as a product lane. It must remain source-compatible for Docker, readonly DB, API, browser, logs, cross-plane, manual ops, Slurm, compute summary, and display summary evidence. |
| manual ops receipts | Current owner `services.production_closure.two_node_e2e_manual_ops_lane`; aggregator keeps final aggregation and shared current-run/path/read/identity helper injection behind `validate_two_node_e2e_evidence(config)`. `manual_action_name` and `manual_action_outcome_status` live in this owner and remain reused by the readonly DB owner through helper injection. | Reads the first available `manual-ops/summary.json` or `manual-ops/evidence.json` via `MANUAL_OPS_DOCUMENT_CANDIDATES`. PASS input must use schema `nhms.two_node_e2e.manual_ops.v1`, bind to the current run, include redacted production operator auth, include 27 display retry/cancel fail-closed action evidence with 409 manual-action response metadata, include no-side-effect proof, and include actual node 22 `compute_control` receipt provenance for every declared source. Receipt artifacts are optional supplemental evidence; when provenance provides `path`/`artifact_path` or `sha256`/`artifact_sha256`, the artifact must be bounded JSON under approved roots with sha256, producer, source, action, run ID, and receipt/provenance strict identity matching on `run_id`, `source`, `cycle_time`, and `model_id`. | `evaluate_manual_ops_lane(...)` returns `lane_summaries.manual_ops` with status, blockers, findings, and redacted evidence. 27 side effects or actual receipts produced by 27 fail; missing auth, missing response evidence, missing node 22 receipt, source coverage gaps, unredacted provenance, or invalid provided receipt artifacts block. | `TWO_NODE_E2E_MANUAL_OPS_*`, including `TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_*`, plus shared strict identity/current-run/stale namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "manual_ops"` | Retain aggregator final composition until cross-plane/final extractions have stable lane result interfaces and prove unchanged full-command summaries. | Extracted in #741 as a manual ops owner module. The product decision is fixed: 27 cannot produce control receipts, and 22 receipts must be producer-backed. |
| source-scope / cross-plane aggregation | Current owner `services.production_closure.two_node_e2e_cross_plane_lane`; aggregator keeps discovery, final aggregation, summary writing, and `_cross_plane_helpers` injection behind `validate_two_node_e2e_evidence(config)`. | Cross-plane input reads the first available `cross-plane/summary.json` or `cross-plane/evidence.json` through `CROSS_PLANE_DOCUMENT_CANDIDATES`. It also consumes declared sources, strict identities, reduced-scope flag, and source-lane results from API, browser, and logs. PASS input must bind to the current run, include live cross-plane evidence, include producer-backed proof, avoid mock/historical fallback, and include per-source records matching four-field strict identities. `source_scope_results` are built by `build_source_scope_results` from metadata strict identities and currently require `run_id`, `source`, `cycle_time`, `model_id`, and `job_id` for each declared source. | `evaluate_cross_plane_lane(...)` returns `lane_summaries.cross_plane`; `build_source_scope_results(...)` returns `source_scope_results` keyed by declared source. Each source result contains `status`, redacted `identity`, `lane_statuses`, `blockers`, and `findings`. Full PASS requires GFS and IFS source scope through `is_full_scope_pass`; reduced or incomplete scope becomes PARTIAL when not failed or blocked. | `TWO_NODE_E2E_CROSS_PLANE_*`, `TWO_NODE_E2E_SOURCE_*`, `TWO_NODE_E2E_REDUCED_SOURCE_SCOPE`, shared strict identity, producer, and current-run namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "cross_plane or source_scope or reduced_scope"` | Retain aggregator final composition until final aggregation extraction proves unchanged full GFS/IFS requirement, reduced-scope PARTIAL semantics, strict identity aggregation, and source-scope blocker/finding collection. | Extracted in #742 as the cross-plane/source-scope aggregation owner. Final aggregation remains separate 3.12 work, and this owner does not absorb #673 lanes. |
| final aggregation | Current owner `services.production_closure.two_node_e2e_final_aggregation`; facade keeps stable `validate_two_node_e2e_evidence(config)`, CLI routing, and compatibility re-exports for status/schema/path-safety names. | Consumes metadata, declared source scope, all `LaneEvaluation` results, and `source_scope_results`. Writes `final-e2e-evidence/summary.json` under the approved evidence root through `EvidenceWriter`. Output creation must reject unsafe run IDs, unapproved roots, symlink/traversal paths, existing output without `force`, oversized payloads, JSON too deep to redact safely, and unredacted secret material. | `build_final_summary(...)` assembles final summary schema `nhms.two_node_e2e.final_evidence.v1` with `status`, `generated_at`, `run_id`, public paths, metadata summary, `strict_identity`, `lane_summaries`, `source_scope_results`, top-level `blockers`, top-level `findings`, and `redaction` flags; `write_final_summary(...)` writes and returns the redacted public summary. Final status is FAIL if any lane/source fails, BLOCKED if any lane/source blocks, PARTIAL for reduced or incomplete full source scope or any partial lane/source, otherwise PASS. | `TWO_NODE_E2E_LANE_*`, `TWO_NODE_E2E_SOURCE_*`, `TWO_NODE_E2E_DECLARED_SOURCES_MISSING`, `TWO_NODE_E2E_REDUCED_SOURCE_SCOPE`, `TWO_NODE_E2E_EVIDENCE_*`, `TWO_NODE_E2E_RUN_ID_UNSAFE`, `TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED`, plus propagated lane namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "final or redaction or evidence_root or stale"` | Retain facade entrypoint and compatibility re-exports until external callers and tests no longer import final aggregation names from `services.production_closure.two_node_e2e_evidence`, and every extracted lane has equivalent structured result coverage. | Extracted in #743 after 3.1-3.11 stabilized lane result interfaces. Output safety helpers now live with the final aggregation owner and are re-exported by the facade for compatibility. |

## Cross-Lane Contracts

### Strict identity

Run metadata must declare source scope and strict identities. Metadata strict
identity entries and final `source_scope_results` identity records currently
require `run_id`, `source`, `cycle_time`, `model_id`, and `job_id` for each
declared source.

Lane-specific matching uses narrower or broader identity requirements depending
on the proof surface. API source/check records, cross-plane per-source records,
manual ops receipt artifacts, and non-log readonly DB route smoke records match
`run_id`, `source`, `cycle_time`, and `model_id`. Log proof, readonly DB
`job_logs`, browser `ops_jobs`/`ops_job_logs`, and producer proof for
`job_logs`, `ops_jobs`, or `ops_job_logs` additionally require `job_id`.
`cycle_time` comparison uses timestamp semantics, so equivalent compact and ISO
UTC encodings may match.

The full PASS source set is `GFS` plus `IFS`. A reduced or single-source bundle
can be valid evidence, but final aggregation must report PARTIAL unless it is
already FAIL or BLOCKED.

### Current-run binding

Every PASS-producing lane must bind to the current evidence run through one of
the governed current-run keys, such as `evidence_run_id`, `bundle_run_id`,
`evidence_bundle_id`, `validation_run_id`, `current_evidence_run_id`,
`current_bundle_run_id`, `expected_evidence_run_id`,
`parent_evidence_run_id`, `parent_bundle_run_id`, or `parent_bundle_id`.
Nested producer evidence with a mismatched current-run key blocks. Source
artifacts must either live under the current run directory or explicitly bind
to the current run ID.

### Producer and source artifacts

PASS evidence must be producer-backed, not just a wrapper summary. Accepted
producer proof can be commands, requests, responses, browser/network artifacts,
source artifacts, evidence, or proofs. Non-authoritative containers such as
metadata, wrapper, collector, context, diagnostics, debug, extra, or notes are
not sufficient by themselves.

Source artifact records must include a path and sha256 digest, stay under an
approved evidence root, read as bounded JSON without following unsafe paths,
match file content, and carry nested current-run binding. Docker security and
readonly DB have lane-specific child/source artifact subcontracts in addition
to the shared producer contract.

### Redaction

All public summaries use redacted payloads. Final output must not contain raw
secret material; database URLs must be redacted; manual production operator auth
must be redacted metadata only; log URIs must not carry userinfo, credential
path components, queries, fragments, or token-like secrets. Oversized or deeply
nested evidence must be bounded or rejected instead of being written raw.

### Path and log URI safety

Evidence roots are limited to repository `artifacts/` and `/scratch/frd_muziyao`.
Final evidence output and artifact reads must reject traversal, symlink
components, unsafe file types, unapproved roots, and stale unscoped paths.
Approved-root enforcement applies to evidence, temp, and artifact paths
currently checked by the aggregator, not to host resource facts such as Docker
`docker_root_dir` / DockerRootDir. Docker preflight still requires that host
DockerRootDir as resource evidence.

Log proof must use display-safe published locations. Accepted schemes are
`published://`, `file://` under an allowed published artifact root, or `s3://`
under an explicit bucket/prefix allowlist. Private workspace paths, compute
workspace paths, unsupported schemes, malformed URIs, unsafe path components,
userinfo, query strings, and fragments block final PASS.

### Final status semantics

Final aggregation status is ordered by severity:

1. Any lane or source FAIL makes the final status FAIL.
2. Otherwise, any lane or source BLOCKED makes the final status BLOCKED.
3. Otherwise, missing full `GFS` plus `IFS` source PASS scope, reduced scope,
   or any PARTIAL lane/source makes the final status PARTIAL.
4. Only full-scope all-PASS evidence returns PASS.

Blockers represent missing, stale, unsafe, incomplete, or non-authoritative
evidence. Findings represent evidence that proves an unsafe or failed condition.
Extraction work must preserve this blocker/finding split because callers and
receipts use it to distinguish "cannot prove" from "proved unsafe".

## Guard Hook Seed

Issue #674 and later extraction issues can use this inventory as the expected
owner map:

| Lane / surface | Owner selector in current aggregator | Guard expectation |
|---|---|---|
| shared contracts | `TWO_NODE_E2E_SHARED_CONTRACTS`, `lane-result-adapter`, `current-run-binding`, `producer-source-artifacts`, `strict-identity`, `approved-root-path-safety`, `redaction`, and `log-uri-safety` | New shared contract, shared guard symbol, blocker/finding namespace, or consumer set requires this inventory and the shared-contract metadata tests to change. |
| lane closure and document discovery | `FINAL_REQUIRED_LANES`, `_load_lane_documents`, lane construction in `validate_two_node_e2e_evidence` | Lane additions/removals, lane order/name changes, discovery alias changes, required checks, live flags, or pass aliases require this inventory to change. |
| metadata | `two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES`, `evaluate_metadata_lane`, `resolve_metadata_scope`, `resolve_strict_identities`, `strict_identity_metadata_issues`, `RUN_METADATA_SCHEMAS`, `STRICT_LOG_IDENTITY_FIELDS`, and aggregator `_metadata_lane_helpers` | New metadata schema aliases, pass aliases, source-scope inputs, strict identity fields, owner result shape, or metadata blocker codes require this inventory and the metadata owner tests to change. |
| Docker preflight | `two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES`, `evaluate_docker_preflight`, `DOCKER_PREFLIGHT_SCHEMA`, `DOCKER_PREFLIGHT_REQUIRED_COMMANDS`, `DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS`, and aggregator `_docker_preflight_helpers` | New preflight aliases, fields, commands, resource checks, path/current-run helper bindings, or blocker namespaces require this inventory and the Docker preflight owner tests to change. |
| Docker security | `two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES`, `evaluate_docker_security`, `DOCKER_SECURITY_SUMMARY_SCHEMA`, `DOCKER_SECURITY_CHILD_SCHEMAS`, `DOCKER_REQUIRED_FALSE_PROOFS`, `DOCKER_REQUIRED_TRUE_PROOFS`, `DOCKER_FORBIDDEN_BOOL_KEYS`, `DOCKER_FORBIDDEN_FINDING_TOKENS`, and aggregator `_docker_security_helpers` | New security aliases, child artifacts, forbidden capability aliases, readonly proof aliases, source-trust labels, helper bindings, or blocker namespaces require this inventory and the Docker security owner tests to change. |
| readonly DB | `two_node_e2e_readonly_db_lane.READONLY_DB_DOCUMENT_CANDIDATES`, `evaluate_readonly_db`, `READONLY_DB_LIVE_SCHEMA`, `READONLY_DB_REQUIRED_ROUTE_NAMES`, `READONLY_DB_STRICT_ROUTE_FIELDS`, `READONLY_DB_REQUIRED_PERMISSION_TARGETS`, `READONLY_DB_SOURCE_ARTIFACT_FILENAMES`, and aggregator `_readonly_db_helpers` | New aliases, live proof fields, route identities, permission/manual-action requirements, source artifact contracts, helper bindings, or blocker namespaces require this inventory and the readonly DB owner tests to change. |
| API proof | `two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES`, `API_REQUIRED_CHECKS`, `API_LIVE_FLAG`, `ApiLaneEvaluationHelpers`, `evaluate_api_lane`, and aggregator `_api_lane_helpers` | New API aliases, required checks, live-proof fields, helper bindings, source identity requirements, or blocker namespaces require this inventory and the API owner tests to change. |
| browser proof | `two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES`, `BROWSER_BASE_REQUIRED_CHECKS`, `BROWSER_SOURCE_SWITCH_CHECK`, `BROWSER_JOB_ID_REQUIRED_CHECKS`, `BROWSER_LIVE_FLAG`, `BrowserLaneEvaluationHelpers`, `browser_required_checks`, `evaluate_browser_lane`, and aggregator `_browser_lane_helpers` | New browser aliases, required checks, source-switch semantics, job-like identity rules, live-proof fields, helper bindings, source identity requirements, or blocker namespaces require this inventory and the browser owner tests to change. |
| logs | `two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES`, `LOGS_REQUIRED_CHECKS`, `LOGS_JOB_ID_REQUIRED_CHECKS`, `LOGS_LIVE_FLAG`, `LogsLaneEvaluationHelpers`, `evaluate_logs_lane`, and aggregator `_logs_lane_helpers` / log URI safety helpers | New logs aliases, required checks, published-root schemes, unavailable semantics, helper bindings, or job identity rules require this inventory and the logs owner tests to change. |
| Slurm proof | `two_node_e2e_simple_live_lane.SLURM_LANE_CONFIG`, `SLURM_DOCUMENT_CANDIDATES`, `evaluate_simple_live_lane`, and aggregator `_simple_live_lane_helpers` | New Slurm evidence aliases, live-proof fields, pass statuses, helper bindings, or blocker codes require this inventory and the simple-live owner tests to change. |
| compute summary | `two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_LANE_CONFIG`, `COMPUTE_SUMMARY_DOCUMENT_CANDIDATES`, `evaluate_simple_live_lane`, and aggregator `_simple_live_lane_helpers` | New compute summary aliases, live-proof fields, pass aliases, helper bindings, or blocker codes require this inventory and the simple-live owner tests to change. |
| display summary | `two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_LANE_CONFIG`, `DISPLAY_SUMMARY_DOCUMENT_CANDIDATES`, `evaluate_simple_live_lane`, and aggregator `_simple_live_lane_helpers` | New display summary aliases, live-proof fields, pass aliases, helper bindings, or blocker codes require this inventory and the simple-live owner tests to change. |
| producer identity / source artifacts | `_producer_source_artifact_*`, `_source_lane_check_producer_blockers`, strict identity helpers | New producer proof containers or source artifact acceptance rules require this inventory to change. |
| manual ops receipts | `two_node_e2e_manual_ops_lane.MANUAL_OPS_DOCUMENT_CANDIDATES`, `MANUAL_OPS_SCHEMA`, `MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS`, `MANUAL_OPS_RESPONSE_REDACTION_KEYS`, `MANUAL_OPS_SIDE_EFFECT_CATEGORIES`, `ManualOpsLaneEvaluationHelpers`, `evaluate_manual_ops_lane`, `manual_action_name`, `manual_action_outcome_status`, and aggregator `_manual_ops_lane_helpers` | New manual actions, receipt producer rules, response evidence rules, helper bindings, or blocker namespaces require this inventory and the manual ops owner tests to change. |
| source-scope / cross-plane aggregation | `two_node_e2e_cross_plane_lane.CROSS_PLANE_DOCUMENT_CANDIDATES`, `CROSS_PLANE_LIVE_FLAG`, `CrossPlaneEvaluationHelpers`, `build_source_scope_results`, `evaluate_cross_plane_lane`, `is_full_scope_sources`, `is_full_scope_pass`, `FULL_PASS_SOURCE_SET`, and aggregator `_cross_plane_helpers` | New source-scope status semantics, source set, strict identity aggregation, cross-plane proof requirements, helper bindings, or blocker namespaces require this inventory and the cross-plane owner tests to change. |
| final aggregation | `two_node_e2e_final_aggregation.FINAL_EVIDENCE_SCHEMA`, `two_node_e2e_final_aggregation.EvidenceWriter`, `two_node_e2e_final_aggregation.build_final_summary`, `two_node_e2e_final_aggregation.write_final_summary`, `two_node_e2e_final_aggregation.final_status`, `two_node_e2e_final_aggregation.collect_blockers_and_findings`, `two_node_e2e_final_aggregation.metadata_summary`, `two_node_e2e_final_aggregation._safe_resolved_evidence_root`, `validate_two_node_e2e_evidence`, `LaneEvaluation.to_summary`, `_final_status` | New final summary fields, status semantics, redaction behavior, compatibility re-exports, or output safety rules require this inventory to change. |
