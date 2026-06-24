# Two-Node E2E Evidence Lane Inventory

Snapshot date: 2026-06-24

Scope: Governance-7 issue #672 inventory for
`services/production_closure/two_node_e2e_evidence.py`. This page records the
production-closure two-node E2E evidence lane contracts that future extraction
work can use without making product decisions.

This inventory is evidence-only. It does not move code, add runtime behavior,
extract a lane, change blocker/status semantics, inventory
follow-up #673 lanes, or write `.entropy-baseline/latest.json`.

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
git diff --check
```

Read-only inventory context was collected from:

```bash
rg -n "FINAL_REQUIRED_LANES|LaneEvaluation|STRICT_LOG_IDENTITY_FIELDS|_load_lane_documents|_evaluate_metadata|_resolve_scope|_resolve_strict_identities|_evaluate_docker_preflight|_evaluate_docker_security|_evaluate_readonly_db|_evaluate_source_lane|_evaluate_simple_live_lane|_evaluate_manual_ops|_evaluate_cross_plane|_source_scope_results|_final_status" services/production_closure/two_node_e2e_evidence.py
rg -n "TWO_NODE_E2E_[A-Z0-9_]+" services/production_closure/two_node_e2e_evidence.py
rg -n "metadata|docker_preflight|docker_security|readonly_db|api|browser|logs|simple_lane|slurm|compute_summary|display_summary|manual_ops|source_scope_results|lane_summaries" tests/test_two_node_e2e_evidence.py
```

## Non-Targets

- No #673 lane inventory in this slice.
- No implementation extraction in #672. Docker preflight extraction starts in
  #674.
- No new lane rows for implementation phases. This inventory records the
  current #672 two-node E2E evidence lane set only.
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

## Lane Contracts

| Lane / surface | Owner module plan | Input contract | Output/result shape | Blocker/finding code namespace | Focused verification command | Retention condition | Extraction readiness note |
|---|---|---|---|---|---|---|---|
| metadata | Future owner `services.production_closure.two_node_e2e_metadata_lane`; scope and strict-identity resolution may remain shared contracts consumed by source lanes. | Reads the first available `run.json`, `identity.json`, `metadata.json`, `cross-plane/run.json`, or `cross-plane/identity.json`. PASS input must use a recognized run metadata schema, bind to the current evidence bundle, declare source scope, and include strict identities for every declared source with `run_id`, `source`, `cycle_time`, `model_id`, and `job_id`. | `lane_summaries.metadata` plus top-level `metadata` and `strict_identity` summaries. The lane summary uses `LaneEvaluation.to_summary`; top-level metadata includes status, evidence path, sha256, schema, blockers, and findings; strict identities are exposed only after metadata PASS and are redacted. | `TWO_NODE_E2E_METADATA_*`, `TWO_NODE_E2E_DECLARED_SOURCES_MISSING`, `TWO_NODE_E2E_SOURCE_STRICT_IDENTITY_INCOMPLETE`, and strict identity mismatch/incomplete namespaces propagated to consumers. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"` | Retain in the aggregator until extraction proves schema aliases, bundle binding, source-scope resolution, five-field metadata identity, redaction, and downstream source-lane seeding are equivalent. | Ready as a shared-contract extraction before source-lane extraction; every lane depending on declared sources or expected identities consumes this result. |
| Docker preflight | Future owner `services.production_closure.two_node_e2e_docker_preflight`; aggregator keeps path discovery and composition until extracted. | Reads the first available `docker-preflight/summary.json`, `docker-preflight/docker-preflight.json`, or `docker-preflight.json`. PASS input must use schema `nhms.two_node_docker.preflight.v1`, bind to the current evidence run, include `evidence_root`, `tmpdir`, `docker_root_dir`, `min_free_bytes`, `disk` entries for `evidence_root`, `tmpdir`, and `docker_root`, and command evidence for `docker_version`, `docker_compose_version`, `docker_info_docker_root`, `docker_system_df`, and `df_h`. Recorded `evidence_root` and `tmpdir` paths currently checked by the aggregator must stay under approved evidence roots; `docker_root_dir` remains required host resource evidence and can be a DockerRootDir such as `/var/lib/docker`. | `lane_summaries.docker_preflight` with `status`, `evidence_path`, `evidence_sha256`, `summary_status`, `blockers`, `findings`, and redacted evidence when present. PASS remains PASS only when the producer summary and recomputed contract checks agree; PASS plus blockers becomes BLOCKED. | `TWO_NODE_E2E_DOCKER_PREFLIGHT_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID`, `TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"` | Retain in the aggregator until #674 proves equivalent current-run, disk, command, resource, path-safety, and blocker-code behavior behind the stable entrypoint. | Ready for first extraction slice because its inputs and blockers are self-contained; shared current-run and path helpers must remain shared contracts, not copied ad hoc. |
| Docker security | Future owner `services.production_closure.two_node_e2e_docker_security`; child artifact verification may move to shared Docker evidence helpers. | Reads the first available `docker-security/summary.json`, `docker-security/display-isolation.json`, `docker-security/docker-smoke.json`, `docker-smoke/docker-smoke.json`, or `docker-smoke.json`. PASS input must use schema `nhms.two_node_docker.security_summary.v1`, bind to the current run, include live Docker/container evidence, prove display runtime is `display_readonly`, prove Slurm routes and forbidden host capabilities are absent, prove published artifacts and root filesystem are readonly, and include source artifacts for `source_trust`, `static`, and `smoke` with schema, path, sha256, current-run, safe-read, and PASS subcontracts. | `lane_summaries.docker_security` with lane status plus redacted source artifact summaries. Missing proof or current-run mismatch blocks; forbidden capability, writer-like display runtime, writable published mount, or forbidden child finding fails. | `TWO_NODE_E2E_DOCKER_SECURITY_*`, `TWO_NODE_E2E_DOCKER_SOURCE_TRUST_*`, `TWO_NODE_E2E_DOCKER_STATIC_*`, `TWO_NODE_E2E_DOCKER_SMOKE_*`, `TWO_NODE_E2E_DOCKER_DISPLAY_*`, `TWO_NODE_E2E_DISPLAY_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"` | Retain in the aggregator until the owner module returns identical status, blockers, findings, child-artifact checks, source-trust role-env proof, and readonly capability semantics. | Ready after Docker preflight because it has a larger child-artifact contract. Extraction must keep child schema constants and forbidden proof aliases governed. |
| readonly DB | Future owner `services.production_closure.two_node_e2e_readonly_db_lane`; it may reuse `services.production_closure.readonly_db_validation` for source evidence merge helpers. | Reads the first available `db/readonly-db-boundary/summary.json` or `db/summary.json`. PASS input must use schema `nhms.readonly_db_boundary.evidence.v1`, match the current `run_id`, include `validation_provenance.mode=live` and `live_readonly_proof=true`, include a redacted `database_url`, include readonly role evidence, include route smoke, permission probes, manual-action probes, authoritative sibling/source artifacts, and cover every declared source identity. Route smoke uses route-specific strict identity fields: `job_logs` requires `job_id`; the other identity-bound routes use `run_id`, `source`, `cycle_time`, and `model_id`. | `lane_summaries.readonly_db` with recomputed status, blockers, findings, and redacted evidence. Mutating catalog privilege, writer role, successful mutation probe, stale source artifact, route identity mismatch, or missing source coverage prevents PASS according to blocker/finding severity. | `TWO_NODE_E2E_READONLY_DB_*`, plus shared strict identity and current-run namespaces where child evidence is compared. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"` | Retain in the aggregator until extracted code proves live readonly proof, no-write probe, route identity, source artifact, sibling file, and recomputed-status behavior are equivalent. | Ready for extraction only after deciding whether readonly child artifact readers stay local to this lane or become shared source-artifact helpers. Product semantics are already fixed here. |
| API proof | Future owner `services.production_closure.two_node_e2e_api_lane`; shared producer/identity helpers stay outside the lane. | Reads the first available `api/summary.json` or `api/evidence.json`. PASS input must bind to the current run, set live API evidence, avoid mock or historical latest fallback, include producer-backed command/request/response/artifact proof, cover every declared source, and include PASS checks for `latest_product`, `series`, `ops_status`, `ops_stages`, and `jobs`. API source/check strict matching currently uses `run_id`, `source`, `cycle_time`, and `model_id`; producer check proof also binds the check name and current evidence run. | `lane_summaries.api` plus per-source contribution to `source_scope_results`. Source/check FAIL creates findings and FAIL; missing check, non-PASS check, missing source, stale run, or missing producer proof creates blockers and BLOCKED. | `TWO_NODE_E2E_API_*`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_STRICT_IDENTITY_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"` | Retain until API proof extraction returns the same lane summary and source-scope effects for GFS/IFS full scope and reduced scope. | Ready once shared producer identity and strict identity helpers are factored or explicitly retained as aggregator-owned shared contracts. |
| browser proof | Future owner `services.production_closure.two_node_e2e_browser_lane`; browser artifacts and source-switch proof remain lane-owned. | Reads the first available `browser/summary.json` or `browser/evidence.json`. PASS input must bind to the current run, set live browser evidence, include producer-backed browser/network/artifact proof, avoid mock or historical latest fallback, cover every declared source, and include PASS checks for `hydro_met`, `ops`, `ops_jobs`, `ops_job_logs`, and `source_switch` when multiple sources are declared. Browser source records use four-field strict matching; `ops_jobs` and `ops_job_logs` check proof additionally requires `job_id`. | `lane_summaries.browser` plus per-source contribution to `source_scope_results`. Output shape matches `LaneEvaluation.to_summary`; job-like checks must include `job_id` identity where required. | `TWO_NODE_E2E_BROWSER_*`, shared `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_STRICT_IDENTITY_*`, and current-run namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"` | Retain until extraction proves live browser, source-switch, producer proof, strict identity, and source-scope aggregation parity. | Ready after API proof because it uses the same source-lane evaluator with browser-specific required checks. |
| logs | Future owner `services.production_closure.two_node_e2e_logs_lane`; log URI parsing and published-artifact safety can become shared helpers. | Reads the first available `logs/summary.json` or `logs/evidence.json`. PASS input must bind to the current run, set live log evidence, include producer-backed proof, cover every declared source, include PASS `job_logs` checks, include strict log identity with `job_id`, and provide an allowed published log URI or typed published-log unavailable evidence. Log URI input must not include credentials, query strings, fragments, private workspace paths, or unsafe path components. | `lane_summaries.logs` plus per-source contribution to `source_scope_results`. Allowed outputs include published log proof or typed unavailable proof; unsafe/private URI, missing read evidence, identity mismatch, or missing job ID blocks PASS. | `TWO_NODE_E2E_LOGS_*`, shared `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, strict identity, and current-run namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs"` | Retain until extraction proves `published://`, `file://`, and `s3://` allowlist behavior, private-path rejection, unavailable-log semantics, identity parsing, and redaction parity. | Ready only if log URI safety helpers are moved with tests; do not mix private compute logs with published display-safe log evidence. |
| Slurm proof | Future owner `services.production_closure.two_node_e2e_slurm_lane`; simple live-lane current-run and producer helpers should stay shared. | Reads the first available `slurm/summary.json` or `slurm/evidence.json`. PASS input must bind to the current run, include `live_slurm_evidence`, include producer-backed proof, avoid stale nested bundle IDs, and avoid mock or deterministic fixture evidence. | `lane_summaries.slurm` with `status`, evidence path, sha256, summary status, blockers, findings, and redacted evidence. Missing lane, stale run, missing live evidence, missing producer proof, or mock evidence prevents PASS. | `TWO_NODE_E2E_SLURM_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"` | Retain until extraction proves simple-live-lane status normalization, current-run recursion, producer proof, mock rejection, and summary shape parity. | Ready after the shared simple-live lane helper boundary is named; it should not absorb compute or display summary product rules. |
| compute summary | Future owner `services.production_closure.two_node_e2e_compute_summary_lane`; it should consume the same simple live-lane helper as Slurm. | Reads the first available `22-compute/summary.json`, `compute/summary.json`, or `compute-summary.json`. PASS-compatible input statuses are `PASS`, `ready`, and `submitted`; PASS input must bind to the current run, include `live_compute_evidence`, include producer-backed proof, avoid stale nested bundle IDs, and avoid mock or fixture evidence. | `lane_summaries.compute_summary` with `LaneEvaluation.to_summary` fields and redacted evidence. Missing lane, stale current-run binding, missing live evidence, missing producer proof, or mock evidence blocks or fails final PASS according to simple-live-lane semantics. | `TWO_NODE_E2E_COMPUTE_SUMMARY_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"` | Retain until extraction preserves the 22-compute path aliases, `ready`/`submitted` pass aliases, current-run checks, producer proof, and redacted summary shape. | Ready with Slurm/display summary as a small helper-backed extraction; node-22 compute semantics must remain explicit. |
| display summary | Future owner `services.production_closure.two_node_e2e_display_summary_lane`; it should consume the same simple live-lane helper as Slurm. | Reads the first available `27-display/summary.json`, `display/summary.json`, or `display-summary.json`. PASS-compatible input statuses are `PASS` and `ready`; PASS input must bind to the current run, include `live_display_evidence`, include producer-backed proof, avoid stale nested bundle IDs, and avoid mock or fixture evidence. | `lane_summaries.display_summary` with `LaneEvaluation.to_summary` fields and redacted evidence. Missing lane, stale current-run binding, missing live evidence, missing producer proof, or mock evidence blocks or fails final PASS according to simple-live-lane semantics. | `TWO_NODE_E2E_DISPLAY_SUMMARY_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"` | Retain until extraction preserves the 27-display path aliases, `ready` pass alias, current-run checks, producer proof, and redacted summary shape. | Ready with Slurm/compute summary as a small helper-backed extraction; node-27 display evidence must stay separate from readonly DB and browser lanes. |
| producer identity / source artifacts | Future shared owner `services.production_closure.two_node_e2e_producer_contracts`; lanes consume it instead of duplicating proof walkers. | Input is embedded in source lanes and simple live lanes through `source_artifacts`, `commands`, `requests`, `responses`, `browser_artifacts`, `screenshots`, `network`, `artifacts`, `evidence`, or `proofs`. Source artifact records must include path or artifact path, sha256 or digest, current-run binding or current run directory placement, approved evidence root, safe bounded JSON, and matching nested run ID. Authoritative producer proof must not be only wrapper metadata, diagnostics, debug, or notes. Producer check identity binds `source`, `check`, `run_id`, `cycle_time`, and `model_id`; `job_logs`, `ops_jobs`, and `ops_job_logs` additionally require `job_id`. | Shared blockers/findings are attached to the consuming lane summary and final `blockers`/`findings`; no separate runtime lane summary exists today. The result shape for extraction is a reusable contract object containing `blockers`, `findings`, redacted producer evidence summary, and artifact records accepted/rejected. | `TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_*`, `TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_*`, `TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH`, `TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE`, `TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH`. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"` | Retain as aggregator-local shared logic until at least two extracted lanes consume a single shared helper with identical blocker codes and redaction. | Ready as a shared-contract extraction, not as a product lane. It must remain source-compatible for Docker, readonly DB, API, browser, logs, cross-plane, manual ops, Slurm, compute summary, and display summary evidence. |
| manual ops receipts | Future owner `services.production_closure.two_node_e2e_manual_ops_lane`; receipt artifact validation may share approved-artifact readers. | Reads the first available `manual-ops/summary.json` or `manual-ops/evidence.json`. PASS input must use schema `nhms.two_node_e2e.manual_ops.v1`, bind to the current run, include redacted production operator auth, include 27 display retry/cancel fail-closed action evidence with 409 manual-action response metadata, include no-side-effect proof, and include actual node 22 `compute_control` receipt provenance and receipt artifacts for every declared source. Receipt artifacts must be bounded JSON under approved roots with sha256, producer, source, action, run ID, and receipt/provenance strict identity matching on `run_id`, `source`, `cycle_time`, and `model_id`. | `lane_summaries.manual_ops` with status, blockers, findings, and redacted evidence. 27 side effects or actual receipts produced by 27 fail; missing auth, missing response evidence, missing node 22 receipt, source coverage gaps, unredacted provenance, or stale receipt artifacts block. | `TWO_NODE_E2E_MANUAL_OPS_*`, shared strict identity/current-run/artifact safety namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "manual_ops"` | Retain until extraction proves display fail-closed, no-side-effect, production auth redaction, node 22 receipt provenance, source coverage, receipt artifact, and strict identity parity. | Ready after shared artifact helper boundaries are clear. The product decision is fixed: 27 cannot produce control receipts, and 22 receipts must be producer-backed. |
| source-scope / cross-plane aggregation | Future owner `services.production_closure.two_node_e2e_cross_plane_lane`; source-scope result construction can stay as shared aggregation helper. | Cross-plane input reads the first available `cross-plane/summary.json` or `cross-plane/evidence.json`. It also consumes declared sources, strict identities, reduced-scope flag, and source-lane results from API, browser, and logs. PASS input must bind to the current run, include live cross-plane evidence, include producer-backed proof, avoid mock/historical fallback, and include per-source records matching four-field strict identities. `source_scope_results` are then built from metadata strict identities and currently require `run_id`, `source`, `cycle_time`, `model_id`, and `job_id` for each declared source. | `lane_summaries.cross_plane` plus `source_scope_results` keyed by declared source. Each source result contains `status`, redacted `identity`, `lane_statuses`, `blockers`, and `findings`. Full PASS requires GFS and IFS source scope; reduced or incomplete scope becomes PARTIAL when not failed or blocked. | `TWO_NODE_E2E_CROSS_PLANE_*`, `TWO_NODE_E2E_SOURCE_*`, `TWO_NODE_E2E_REDUCED_SOURCE_SCOPE`, shared strict identity, producer, and current-run namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "cross_plane or source_scope or reduced_scope"` | Retain until extraction proves source-scope status composition, full GFS/IFS requirement, reduced-scope PARTIAL semantics, and strict identity aggregation parity. | Ready as an aggregation extraction once API/browser/logs lane result interfaces are stable. It should not absorb #673 lanes. |
| final aggregation | Future owner `services.production_closure.two_node_e2e_final_aggregation`; it remains the stable CLI/API composition boundary. | Consumes metadata, declared source scope, all `LaneEvaluation` results, and `source_scope_results`. Writes `final-e2e-evidence/summary.json` under the approved evidence root. Output creation must reject unsafe run IDs, unapproved roots, symlink/traversal paths, existing output without `force`, oversized payloads, JSON too deep to redact safely, and unredacted secret material. | Final summary schema `nhms.two_node_e2e.final_evidence.v1` with `status`, `generated_at`, `run_id`, public paths, metadata summary, `strict_identity`, `lane_summaries`, `source_scope_results`, top-level `blockers`, top-level `findings`, and `redaction` flags. Final status is FAIL if any lane/source fails, BLOCKED if any lane/source blocks, PARTIAL for reduced or incomplete full source scope or any partial lane/source, otherwise PASS. | `TWO_NODE_E2E_LANE_*`, `TWO_NODE_E2E_SOURCE_*`, `TWO_NODE_E2E_DECLARED_SOURCES_MISSING`, `TWO_NODE_E2E_EVIDENCE_*`, `TWO_NODE_E2E_RUN_ID_UNSAFE`, `TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED`, plus propagated lane namespaces. | `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "final or redaction or evidence_root or stale"` | Retain as the public entrypoint until every extracted lane returns the same structured result and equivalent full-command summaries for existing fixtures. | Ready last. Extraction before lane results stabilize would recreate the current aggregator coupling in a new file. |

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
| metadata | metadata discovery in `validate_two_node_e2e_evidence`, `_evaluate_metadata`, `_resolve_scope`, `_resolve_strict_identities`, `RUN_METADATA_SCHEMAS`, `STRICT_LOG_IDENTITY_FIELDS` | New metadata schema aliases, source-scope inputs, strict identity fields, or metadata blocker codes require this inventory to change. |
| Docker preflight | `_evaluate_docker_preflight`, `DOCKER_PREFLIGHT_*`, `docker-preflight/*` discovery | New preflight fields, commands, resource checks, or blocker codes require this inventory to change. |
| Docker security | `_evaluate_docker_security`, Docker security child schema constants, Docker proof aliases | New child artifacts, forbidden capability aliases, or source-trust labels require this inventory to change. |
| readonly DB | `_evaluate_readonly_db`, `READONLY_DB_*`, readonly source artifact helpers | New route, permission, manual-action, or source artifact requirements require this inventory to change. |
| API proof | `_evaluate_source_lane("api", ...)` and API required check tuple | New API checks or source identity requirements require this inventory to change. |
| browser proof | `_evaluate_source_lane("browser", ...)` and `_browser_required_checks` | New browser checks or source-switch semantics require this inventory to change. |
| logs | `_evaluate_source_lane("logs", ...)`, log URI helpers, published-log unavailable helpers | New log URI schemes, published roots, unavailable semantics, or job identity rules require this inventory to change. |
| Slurm proof | `_evaluate_simple_live_lane("slurm", ...)`, `slurm/*` discovery, `live_slurm_evidence` | New Slurm evidence aliases, live-proof fields, pass statuses, or blocker codes require this inventory to change. |
| compute summary | `_evaluate_simple_live_lane("compute_summary", ...)`, `22-compute/*` and compute summary discovery, `live_compute_evidence` | New compute summary aliases, live-proof fields, pass aliases, or blocker codes require this inventory to change. |
| display summary | `_evaluate_simple_live_lane("display_summary", ...)`, `27-display/*` and display summary discovery, `live_display_evidence` | New display summary aliases, live-proof fields, pass aliases, or blocker codes require this inventory to change. |
| producer identity / source artifacts | `_producer_source_artifact_*`, `_source_lane_check_producer_blockers`, strict identity helpers | New producer proof containers or source artifact acceptance rules require this inventory to change. |
| manual ops receipts | `_evaluate_manual_ops`, `MANUAL_OPS_*`, manual receipt artifact helpers | New manual actions, receipt producer rules, or response evidence rules require this inventory to change. |
| source-scope / cross-plane aggregation | `_evaluate_cross_plane`, `_source_scope_results`, `FULL_PASS_SOURCE_SET` | New source-scope status semantics, source set, or cross-plane proof requirements require this inventory to change. |
| final aggregation | `validate_two_node_e2e_evidence`, `LaneEvaluation.to_summary`, `_final_status`, `EvidenceWriter` | New final summary fields, status semantics, redaction behavior, or output safety rules require this inventory to change. |
