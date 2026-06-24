## Context

The current report-only entropy audit on `master` emits 463 findings, 259
budget-counted findings, and zero gate-eligible findings. Most findings are
document or OpenSpec drift, but the manual heatmap highlights structural risks
in large files that act as compatibility or evidence aggregators:

- `services/orchestrator/scheduler.py` has more than 6000 lines, many imported
  owner families, and scheduler-state compatibility monkeypatch bindings.
- `services/orchestrator/chain.py` has more than 6900 lines and aggregates
  stage, manifest, reservation, retry, tile publisher, worker, and persistence
  behavior.
- `services/production_closure/two_node_e2e_evidence.py` has more than 9000
  lines and holds many evidence lanes, alias matrices, path-safety checks, and
  final aggregation rules.
- `services/production_closure/readiness_validation.py` has more than 3500
  lines and mixes dependency proof, scheduler evidence, live proof, and final
  readiness aggregation.
- `apps/api/main.py` and `apps/frontend/src/components/map/M11MapLibreSurface.tsx`
  are smaller but still above the proposed 1000-line governance threshold.

Only root `AGENTS.md` and `CLAUDE.md` currently exist, and
`openspec/glossary.md` is absent. That means future agents see the whole
repository through a broad root instruction file instead of local ownership
rules for the highest-entropy directories.

## Goals / Non-Goals

**Goals:**

- Stop structural entropy from growing while preserving compatibility and
  production behavior.
- Make large-file governance measurable with line-count, import-family,
  compatibility-symbol, and active budget-count metrics.
- Split implementation work into small, issue-sized PRs with clear module
  ownership.
- Lower active document entropy without erasing historical/audit evidence.
- Give future agents scoped context for the directories most likely to spread
  shallow patterns.

**Non-Goals:**

- No blanket rule that every file must immediately be below 1000 lines.
- No deletion of archive/history just to reduce finding totals.
- No removal of compatibility shims until callers/tests are migrated or a
  recorded compatibility decision permits removal.
- No production topology, Slurm, SHUD, DB schema, API contract, or frontend
  behavior change unless a later implementation issue explicitly owns it.
- No CI hard-gate enablement in this change by itself.

## Issue #667 Fixture

Fixture level: expanded. The first implementation slice changes a shared
governance script/report schema, so the mandatory expanded triggers are
`script entry`, `schema/field`, `source file classification`, report-only
baseline non-write behavior, and large-file discovery.

Change surface:

- `scripts/governance/audit_repo_entropy.py` report construction, metadata,
  Markdown rendering, and helper classification functions.
- `tests/test_entropy_audit_script.py` focused structural-budget fixtures.
- `.github/workflows/governance.yml` report-only structural base-ref wiring for
  the governance entropy audit.

Must preserve:

- Existing entropy finding records, `summary_counts`, budget-counted and
  gate-eligible semantics remain compatible.
- Report-only and explicit hard-gate audit commands never create or update
  `.entropy-baseline/latest.json`.
- Default and CI-facing behavior stays report-only; #667 allows the minimal
  `.github/workflows/governance.yml` base-ref fetch/config touch needed for the
  report-only structural comparison contract, but does not enable or modify a
  hard gate.
- Generated/data/fixture-like large files are reported separately, not treated
  as ungoverned oversized implementation files.

Must add/change:

- Classify tracked source files over `1000` lines as mandatory-governance and
  files from `500` to `1000` lines as yellow-zone review candidates.
- Add a structural-budget summary that exposes oversized files, yellow-zone
  files, governed exemptions, and top oversized modules.
- Detect ownership-surface growth signals for oversized files: new import
  families, public entrypoints, parser/validator lanes, and compatibility
  symbols, while allowing bugfix edits that do not add surface.

Risk packs considered:

- Public API / CLI / script entry: selected - audit CLI JSON/Markdown output
  gains a structural-budget section without changing existing exit semantics.
- Config / project setup: selected - the only workflow/config touch is the
  report-only governance workflow's structural base-ref checkout/fetch wiring;
  no hard gate, secret, dependency, or production environment behavior changes.
- File IO / path safety / overwrite: selected - audit reads tracked files and
  must preserve no-write baseline policy.
- Schema / columns / units / field names: selected - report metadata/summary
  schema gains structural-budget fields while existing finding schema remains
  stable.
- Auth / permissions / secrets: not selected - no credentials or privileged
  runtime path.
- Concurrency / shared state / ordering: not selected - audit remains a single
  local report pass.
- Resource limits / large input / discovery: selected - large-file discovery
  must stay scoped to tracked source files and existing skip/exemption families.
- Legacy compatibility / examples: selected - current report consumers and
  baseline writer tests must continue to work.
- Error handling / rollback / partial outputs: selected - hard-gate/report mode
  and baseline non-write behavior must remain stable on failure.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - OpenSpec task/evidence text
  records the report-only, no-hard-gate boundary.

Domain packs:

- Geospatial / CRS / basin geometry: not selected - no geospatial data path.
- Hydro-met time series / forcing windows: not selected - no forecast data path.
- SHUD numerical runtime / conservation / NaN: not selected - no model runtime.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler
  or Slurm behavior.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider integration.
- Run manifest / QC provenance: not selected - no run manifest contract.
- Published NHMS artifacts / display identity: not selected - no display
  artifact publishing.

Required #667 evidence:

- `>1000` non-exempt tracked source -> mandatory-governance record with path,
  line count, module, import-family signal, and owner action.
- `500-1000` tracked source -> yellow-zone record; if mixed ownership signals
  are present, the reason is explicit.
- generated/data/fixture-like tracked source -> governed exemption record,
  separate from ungoverned oversized source.
- oversized bugfix fixture with no new import/entry/parser/validator surface ->
  no ownership-growth signal.
- oversized fixture with new import family, public entrypoint, parser/validator
  lane, or compatibility symbol -> ownership-growth signal.
- governance workflow -> remains report-only while resolving a non-HEAD
  structural comparison base through shallow checkout plus targeted base fetch.
- report-only and hard-gate audit commands leave `.entropy-baseline/latest.json`
  unchanged.
- existing entropy finding schema, summary counts, and exit-code semantics stay
  compatible.

## Issue #672 Fixture

Fixture level: expanded. The issue defines production-closure lane contracts
for a large validation aggregator and future extraction work, so the mandatory
expanded triggers are `production_closure`, evidence schema/field contract,
legacy compatibility, path/redaction/current-run safety, and future public
entrypoint preservation. This issue is documentation/inventory only; it does
not extract code or change runtime validation behavior.

Change surface:

- `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` lane inventory.
- `openspec/changes/governance-7-structural-entropy-controls/tasks.md`
  evidence for task 3.1.

Must preserve:

- `services/production_closure/two_node_e2e_evidence.py` remains the stable
  aggregator and CLI-facing validation entrypoint for #672.
- Existing lane status vocabulary, blocker/finding code namespaces, redaction,
  current-run binding, source identity, path-safety, readonly DB boundary, and
  final aggregation semantics are documented rather than weakened.
- #673 readiness validation inventory and #674 docker-preflight extraction stay
  separate follow-up scopes.

Must add/change:

- Record the lane owner-module plan, input/output contract, blocker/finding
  namespace, and focused verification command for metadata, Docker
  preflight/security, readonly DB, API/browser proof, logs, Slurm, compute
  summary, display summary, manual ops receipts, source-scope/cross-plane,
  producer identity/source artifacts, and final aggregation surfaces in
  `two_node_e2e_evidence`.
- Make a future extraction issue able to choose a lane without making new
  product decisions.

Risk packs considered:

- Public API / CLI / script entry: selected - the inventory must preserve the
  existing final evidence validator entrypoint and future lane result shape.
- Config / project setup: not selected - no environment, dependency, workflow,
  or deployment configuration changes.
- File IO / path safety / overwrite: selected - lane contracts must retain
  approved evidence-root, symlink/traversal, source artifact, and log URI
  safety semantics.
- Schema / columns / units / field names: selected - lane input/output schemas,
  status values, blocker/finding namespaces, and redacted evidence summaries
  are the primary contract.
- Auth / permissions / secrets: selected - readonly DB and manual ops lanes
  must keep no-write boundaries and credential-safe redaction requirements.
- Concurrency / shared state / ordering: not selected - no runtime scheduler or
  concurrent execution behavior changes.
- Resource limits / large input / discovery: selected - inventory must keep
  bounded evidence payload and scoped source-artifact discovery expectations.
- Legacy compatibility / examples: selected - the current aggregator entrypoint
  and legacy alias/source identity compatibility remain intact until extraction
  issues prove equivalence.
- Error handling / rollback / partial outputs: selected - blocker/finding
  status and partial/BLOCKED semantics are required lane outputs.
- Release / packaging / dependency compatibility: not selected - no package or
  dependency change.
- Documentation / migration notes: selected - this issue is the authoritative
  extraction plan and evidence mapping for #674.

Domain packs:

- Slurm production lifecycle / mock-vs-real parity: selected - final evidence
  can include live Slurm/manual ops/log proof and must not confuse mocked or
  diagnostic evidence with live proof.
- Run manifest / QC provenance: selected - strict identity, producer-backed
  evidence, source artifacts, and current evidence bundle binding are core lane
  contracts.
- Published NHMS artifacts / display identity: selected - readonly DB, API,
  browser, logs, and cross-plane evidence must remain bound to the same source,
  cycle, run, model, and job identity.
- Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
  SHUD numerical runtime, PostGIS / TimescaleDB domain behavior, and external
  provider snapshot reproducibility: not selected - #672 documents validation
  lane boundaries only and does not change scientific or database data paths.

Required #672 evidence:

- Inventory lists every #672 in-scope lane with owner module plan, input
  contract, output/result shape, blocker/finding namespace, focused
  verification command, retention condition, and extraction readiness note.
- Inventory verification input/output: given
  `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`, the documented
  runtime lane-summary set matches `FINAL_REQUIRED_LANES` for #672
  (`metadata`, `docker_preflight`, `docker_security`, `readonly_db`, `api`,
  `browser`, `cross_plane`, `manual_ops`, `slurm`, `logs`, `compute_summary`,
  and `display_summary`). The inventory also covers the shared
  producer-identity/source-artifact surface and final aggregation surface, and
  every row includes owner module plan, input contract, output/result shape,
  blocker/finding namespace, focused verification command, retention condition,
  and extraction readiness note. The inventory must not include #673
  readiness-validation rows or #674 implementation-extraction rows.
- Cross-lane contracts document strict identity, current-run binding,
  producer/source-artifact proof, redaction, path/log URI safety, and final
  aggregation status semantics.
- `uv run pytest -q tests/test_two_node_e2e_evidence.py` remains the focused
  regression command for the documented contracts.
- `openspec validate governance-7-structural-entropy-controls --strict
  --no-interactive` passes.

## Issue #673 Fixture

Fixture level: expanded. The issue defines readiness-validation lane contracts
for a production-closure aggregator that reads bounded evidence, scheduler
artifacts, live proof receipts, scoped exclusions, and final readiness summaries.
The mandatory expanded triggers are `production_closure`, evidence schema/field
contract, live proof/current-run binding, path/redaction safety, and final
readiness status preservation. This issue is documentation/inventory only; it
does not extract code or change runtime validation behavior.

Change surface:

- A new readiness-validation lane inventory document under `docs/governance/`.
- `openspec/changes/governance-7-structural-entropy-controls/tasks.md`
  evidence for task 3.2.

Must preserve:

- `services/production_closure/readiness_validation.py` remains the stable
  validator entrypoint and CLI-facing `validate-readiness` behavior for #673.
- Deterministic dependency summaries and scheduler evidence remain review
  lineage only; they do not satisfy final production readiness without accepted
  live proof receipts.
- Final readiness remains `ready` only when every required live proof item is
  `passed` and `live_proof_accepted=true`; otherwise the summary remains
  `release_blocked`.
- Existing status/execution-mode vocabulary, proof contract aliases,
  blocker/error namespaces, redaction, bounded JSON/path safety, symlink
  rejection, and scoped exclusion semantics are documented rather than weakened.
- #672 two-node E2E evidence inventory and #674 docker-preflight extraction stay
  separate scopes.

Must add/change:

- Record owner-module plan, input/output contract, blocker/error namespace, and
  focused verification command for dependency summaries, scheduler evidence,
  live proof receipts, scoped exclusions, validation/final aggregation, and
  shared preflight/environment/receipt artifact surfaces in
  `readiness_validation`.
- Make a future readiness lane extraction issue able to choose a lane without
  making new product decisions.

Risk packs considered:

- Public API / CLI / script entry: selected - the inventory must preserve the
  existing `validate_readiness`/`validate-readiness` entrypoint and output files.
- Config / project setup: selected - readiness inputs are driven by environment
  variables and CLI options, but #673 only documents them and does not change
  configuration.
- File IO / path safety / overwrite: selected - dependency summaries, scheduler
  evidence, live proof files, evidence roots, existing output behavior, bounded
  reads, regular-file checks, and symlink rejection are core contracts.
- Schema / columns / units / field names: selected - readiness item shape,
  summary/release blocker schemas, live proof schemas, and accepted status aliases
  are primary contracts.
- Auth / permissions / secrets: selected - auth proof, target-environment proof,
  environment capture, and path/secret redaction must remain safe.
- Concurrency / shared state / ordering: not selected - no scheduler execution or
  concurrent processing behavior changes.
- Resource limits / large input / discovery: selected - proof payloads, scheduler
  evidence files, dependency summaries, JSON depth/node limits, and scheduler
  evidence file limits remain bounded.
- Legacy compatibility / examples: selected - existing proof aliases, dependency
  binding aliases, scheduler binding aliases, and deterministic fast-CI behavior
  remain intact until extraction issues prove equivalence.
- Error handling / rollback / partial outputs: selected - malformed, missing,
  oversized, stale, unsafe, or unaccepted proof remains release-blocking and
  redacted.
- Release / packaging / dependency compatibility: not selected - no dependency or
  packaging changes.
- Documentation / migration notes: selected - this issue is the authoritative
  extraction plan and evidence mapping for future readiness lane work.

Domain packs:

- Slurm production lifecycle / mock-vs-real parity: selected - scheduler evidence
  can be deterministic review evidence, while final live scheduler proof requires
  accepted live binding to production scheduler evidence.
- Run manifest / QC provenance: selected - dependency and scheduler live proof
  receipts bind to producer run IDs, artifact refs, checksums/receipt IDs, schemas,
  target environment, and current readiness run ID.
- Published NHMS artifacts / display identity: selected - E2E and MVT dependency
  proof surfaces must stay separate from #672 lane contracts and final readiness
  must not claim live display proof from deterministic summaries alone.
- Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
  SHUD numerical runtime, PostGIS / TimescaleDB domain behavior, and external
  provider snapshot reproducibility: not selected - #673 documents readiness
  validation lane boundaries only and does not change scientific/data behavior.

Required #673 evidence:

- Inventory lists every #673 in-scope readiness lane with owner module plan, input
  contract, output/result shape, blocker/error namespace, focused verification
  command, retention condition, and extraction readiness note.
- Inventory verification input/output: given the readiness inventory document, the
  documented lane/surface set covers dependency summaries (`slurm`,
  `object_store`, `source`, `e2e`, `mvt`), scheduler evidence, live proof receipts
  (`auth`, `alert`, `rollback`, optional `scheduler`, dependency proof receipts,
  `target_env`), scoped exclusions, validation/final aggregation, and shared
  preflight/environment/receipt artifacts. The inventory must not include #672
  two-node E2E lane rows or #674 docker-preflight implementation extraction.
- Cross-lane contracts document deterministic-vs-live proof semantics,
  status/execution-mode truth table, current-run/target-environment binding,
  dependency and scheduler producer binding, redaction, path safety, bounded JSON,
  and final readiness status semantics.
- `uv run pytest -q tests/test_production_readiness_validation.py` remains the
  focused regression command for the documented contracts.
- `openspec validate governance-7-structural-entropy-controls --strict
  --no-interactive` passes.

## Issue #674 Fixture

Fixture level: expanded. The issue performs the first production-closure lane
extraction from `two_node_e2e_evidence.py`, so the mandatory expanded triggers
are `production_closure`, public entrypoint preservation, evidence schema/field
contract, path/redaction/current-run safety, and final aggregation status
preservation.

Change surface:

- New owner module `services.production_closure.two_node_e2e_docker_preflight`
  for the Docker preflight lane.
- Minimal aggregator wiring in
  `services/production_closure/two_node_e2e_evidence.py` behind the existing
  `validate_two_node_e2e_evidence(config)` entrypoint.
- Focused regression tests in `tests/test_two_node_e2e_evidence.py`.
- Task 3.3 evidence in this OpenSpec change.

Must preserve:

- `validate_two_node_e2e_evidence(config)` and the module CLI remain stable.
- `lane_summaries.docker_preflight` keeps the same `LaneEvaluation.to_summary`
  shape: `status`, `evidence_path`, `evidence_sha256`, `summary_status`,
  `blockers`, `findings`, and redacted evidence summary.
- Equivalent fixtures preserve final status ordering and Docker preflight
  blocker codes for missing lane, stale/current-run mismatch, unknown schema,
  missing resource evidence, unsafe recorded paths, producer blockers, missing
  disk evidence, invalid disk evidence, low disk space, missing command evidence,
  failed commands, and missing DockerRootDir.
- Shared current-run, recorded-path approval, blocker construction, status
  normalization, bounded evidence, and redaction semantics may remain
  aggregator-owned shared contracts; extraction must not copy or fork those
  semantics in a way that creates a second truth source.
- Sibling Docker security, readonly DB, API/browser, logs, producer identity,
  source-scope/cross-plane, manual ops, simple live lanes, and final aggregation
  are not extracted or behavior-changed by this issue.

Must add/change:

- Move Docker preflight evaluation responsibility into a named owner module
  that returns a structured lane result with status, blockers, findings, and a
  redacted evidence summary through the existing lane-summary adapter.
- Keep aggregator wiring limited to passing the discovered Docker preflight
  document, current evidence run ID, and shared helper callbacks or contracts
  needed to preserve current behavior.
- Add/retain focused tests proving Docker preflight PASS, current-run blockers,
  unsafe path blockers, missing resource/command/disk evidence, producer
  blockers, low disk, command failure, missing lane, and final status parity.

Risk packs considered:

- Public API / CLI / script entry: selected - extraction sits behind the stable
  validator and must not change CLI/API output.
- Config / project setup: not selected - no new environment variable,
  dependency, workflow, or deployment configuration.
- File IO / path safety / overwrite: selected - preflight evidence paths,
  recorded paths, output summaries, and symlink/traversal safety must remain
  governed by existing helpers.
- Schema / columns / units / field names: selected - Docker preflight schema,
  required commands, required disk labels, `free_bytes`/`min_free_bytes`, lane
  summary fields, and blocker code namespaces are primary contracts.
- Auth / permissions / secrets: selected - public summaries must remain
  redacted and must not expose host path or secret material beyond existing
  redaction semantics.
- Concurrency / shared state / ordering: not selected - no concurrent execution
  or scheduler state change.
- Resource limits / large input / discovery: selected - evidence remains read
  through the existing bounded JSON document loader and redacted summary path.
- Legacy compatibility / examples: selected - discovery aliases and existing
  Docker preflight fixtures must keep working.
- Error handling / rollback / partial outputs: selected - BLOCKED vs FAIL/PASS
  behavior and all current blocker codes must be preserved for equivalent
  fixtures.
- Release / packaging / dependency compatibility: not selected - no packaging
  or dependency change.
- Documentation / migration notes: selected - task evidence records the new lane
  owner and verification commands.

Domain packs:

- Slurm production lifecycle / mock-vs-real parity: not selected - Docker
  preflight validates host/container resource evidence only; Slurm lanes are
  out of scope.
- Run manifest / QC provenance: selected - Docker preflight PASS must bind to
  the current evidence run and preserve stale/missing run blockers.
- Published NHMS artifacts / display identity: selected - recorded evidence
  paths and final public summaries must remain path-safe and redacted, but this
  issue does not alter display identity lanes.
- Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
  SHUD numerical runtime, PostGIS / TimescaleDB domain behavior, and external
  provider snapshot reproducibility: not selected - #674 only extracts a
  Docker preflight evidence lane.

Required #674 evidence:

- Equivalent Docker preflight fixtures produce the same lane status, final
  status, evidence path/checksum fields, summary status, blocker code set, and
  redacted evidence shape before and after extraction.
- Focused tests cover at least PASS, missing current-run binding, stale copied
  evidence, missing resource fields, missing/failed commands, missing/invalid/
  low disk evidence, unsafe recorded paths, producer blockers, missing
  DockerRootDir, and missing Docker preflight lane.
- The new owner module does not import sibling lane implementation details and
  `two_node_e2e_evidence.py` only delegates Docker preflight evaluation plus
  composition.
- `uv run pytest -q tests/test_two_node_e2e_evidence.py` passes.
- `uv run ruff check services/production_closure tests/test_two_node_e2e_evidence.py`
  passes.

## Issue #675 Fixture

Fixture level: standard. The issue cleans active OpenSpec wording only, so the
mandatory triggers are active document entropy, current route authority,
retired-path token handling, and OpenSpec validation. It does not change source
code, archived OpenSpec material, governance docs, or runtime behavior.

Change surface:

- Active OpenSpec specs under `openspec/specs/**`.
- Task 4.1 evidence in this OpenSpec change.

Must preserve:

- Useful historical product and migration evidence remains present; cleanup
  must not delete scenarios merely to reduce audit counts.
- Current display route authority remains `/` as the single-map display
  entrypoint; legacy display paths are described only as redirect aliases,
  compatibility context, or historical evidence.
- Retired active-tree paths are either rewritten to current canonical paths or
  described without re-presenting the retired path token as current active
  guidance.
- Archive material, current governance docs, source comments, and runtime code
  remain out of scope.

Must add/change:

- Rewrite active `openspec/specs/**` route/path findings to canonical current
  terms or machine-readable compatibility/historical context.
- Re-run the report-only entropy audit and record the active
  `openspec/specs/**` before/after finding delta.

Risk packs considered:

- Public API / CLI / script entry: not selected - no API or CLI behavior change.
- Config / project setup: not selected - no config or environment change.
- File IO / path safety / overwrite: not selected - no runtime file IO change.
- Schema / columns / units / field names: not selected - no runtime schema
  change; only OpenSpec wording changes.
- Auth / permissions / secrets: not selected - no auth or secret handling
  change.
- Concurrency / shared state / ordering: not selected - documentation only.
- Resource limits / large input / discovery: selected - the entropy audit scans
  tracked text and must remain report-only without baseline writes.
- Legacy compatibility / examples: selected - the change preserves legacy route
  and retired-path evidence as compatibility or historical context where still
  useful.
- Error handling / rollback / partial outputs: not selected - no runtime errors.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected - the issue is active-spec cleanup.

Domain packs:

- Published NHMS artifacts / display identity: selected - display route wording
  must follow M26 single-map authority and not treat legacy aliases as active
  independent pages.
- Slurm production lifecycle / mock-vs-real parity: selected only for retired
  Slurm template path wording; no Slurm runtime behavior changes.
- Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
  SHUD numerical runtime, PostGIS / TimescaleDB domain behavior, external
  provider snapshot reproducibility, and run manifest/QC provenance: not
  selected - no scientific/runtime contract changes.

Required #675 evidence:

- The report-only entropy audit before cleanup lists active
  `openspec/specs/**` route/path findings.
- After cleanup, active `openspec/specs/**` unallowlisted or budget-counted
  route/path findings are zero or each remaining active finding maps to an
  explicit follow-up owner issue; allowlisted historical/compatibility findings
  do not need follow-up owners.
- `openspec validate --all --strict --no-interactive` passes.
- `uv run python scripts/governance/audit_repo_entropy.py --format json`
  passes and does not write `.entropy-baseline/latest.json`.

## Issue #676 Fixture

Fixture level: standard. The issue cleans active governance/module documentation
wording only, so the mandatory triggers are active document entropy, retired
path token handling, document authority preservation, and OpenSpec validation.
It does not change source code, active OpenSpec specs, archive material, or
runtime behavior.

Change surface:

- Active governance/module docs under `docs/governance/**` and
  `docs/modules/**`.

Workflow evidence exception:

- This reviewed fixture records the #676 workflow boundary. It does not broaden
  #676 implementation scope beyond active governance/module docs.

Must preserve:

- Current role-boundary and module-index guidance remains useful for active
  contributors.
- Schema example documentation remains illustrative without becoming a committed
  baseline or current finding-count source.
- Useful historical/retired context remains present through governed inventory
  references or descriptive retired-placeholder wording.
- Source code, active OpenSpec cleanup/change artifacts, and archive material
  remain out of #676 implementation scope, except this reviewed fixture and
  task checkbox evidence.

Must add/change:

- Rewrite active governance/module doc route/path findings to canonical current
  paths or audit-recognized retired/historical context.
- Re-run the report-only entropy audit and record the active
  `docs/governance/**` + `docs/modules/**` before/after finding delta.

Risk packs considered:

- Public API / CLI / script entry: not selected - no API or CLI behavior change.
- Config / project setup: not selected - no config or environment change.
- File IO / path safety / overwrite: not selected - no runtime file IO change.
- Schema / columns / units / field names: not selected - schema example wording
  remains documentation only and does not change report schema.
- Auth / permissions / secrets: not selected - no auth or secret handling
  change.
- Concurrency / shared state / ordering: not selected - documentation only.
- Resource limits / large input / discovery: selected - the entropy audit scans
  tracked text and must remain report-only without baseline writes.
- Legacy compatibility / examples: selected - the change preserves retired-path
  context without re-presenting retired active-tree paths as current entries.
- Error handling / rollback / partial outputs: not selected - no runtime errors.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected - the issue is active-doc cleanup.

Domain packs:

- Published NHMS artifacts / display identity: not selected - no display route
  contract change.
- Slurm production lifecycle / mock-vs-real parity: selected only for retired
  Slurm template wording in role/module docs; no Slurm runtime behavior changes.
- Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
  SHUD numerical runtime, PostGIS / TimescaleDB domain behavior, external
  provider snapshot reproducibility, and run manifest/QC provenance: not
  selected - no scientific/runtime contract changes.

Required #676 evidence:

- The report-only entropy audit before cleanup lists active
  `docs/governance/**` and `docs/modules/**` route/path findings.
- After cleanup, active governance/module doc unallowlisted or budget-counted
  route/path findings are zero or each remaining active finding maps to an
  explicit follow-up owner issue; allowlisted inventory/authority findings do
  not need follow-up owners.
- `openspec validate --all --strict --no-interactive` passes.
- `uv run python scripts/governance/audit_repo_entropy.py --format json`
  passes and does not write `.entropy-baseline/latest.json`.

## Issue #677 Fixture

Fixture level: standard. The issue classifies one source-comment drift surface,
so the mandatory triggers are retired path token handling, focused audit tests,
Slurm gateway runtime-behavior preservation, and lint/test verification. It does
not change runtime template defaults or perform docs cleanup.

Change surface:

- `services/slurm_gateway/config.py` source comment classification only.
- `scripts/governance/audit_repo_entropy.py` and
  `tests/test_entropy_audit_script.py` only if classifier support is needed.

Workflow evidence exception:

- This reviewed fixture and task checkbox evidence record #677 workflow status.
  They do not broaden #677 implementation scope into active docs cleanup.

Must preserve:

- `SlurmGatewaySettings.template_dir` remains `infra/sbatch`.
- `DEFAULT_JOB_TYPE_TEMPLATES` and job-template mapping behavior remain
  unchanged.
- The retired template explanation remains useful and machine-classifiable as
  source-comment historical/retired context.
- Active docs and arbitrary source files must not become broadly allowlisted for
  retired path tokens.

Must add/change:

- Make the `services/slurm_gateway/config.py` retired-template source comment
  audit-classified as allowlisted historical/retired context.
- Add focused entropy-audit coverage showing that the Slurm gateway source
  comment is allowlisted while an active doc mention remains budget-counted.

Risk packs considered:

- Public API / CLI / script entry: not selected - no public API or CLI behavior
  change.
- Config / project setup: not selected - runtime config defaults are preserved.
- File IO / path safety / overwrite: not selected - no runtime file IO change.
- Schema / columns / units / field names: not selected - no runtime schema
  change.
- Auth / permissions / secrets: not selected - no auth or secret handling
  change.
- Concurrency / shared state / ordering: not selected - no runtime state change.
- Resource limits / large input / discovery: selected - the entropy audit scans
  tracked text and must remain report-only without baseline writes.
- Legacy compatibility / examples: selected - the source comment preserves a
  retired Slurm template migration note without consuming the active drift
  budget.
- Error handling / rollback / partial outputs: not selected - no runtime errors.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected only for source-comment wording and
  workflow evidence; no docs cleanup.

Domain packs:

- Slurm production lifecycle / mock-vs-real parity: selected - the change is
  limited to retired Slurm template classification and must not change Slurm
  gateway runtime behavior.
- Published NHMS artifacts / display identity, geospatial / CRS / basin
  geometry, Hydro-met time series / forcing windows, SHUD numerical runtime,
  PostGIS / TimescaleDB domain behavior, external provider snapshot
  reproducibility, and run manifest/QC provenance: not selected.

Required #677 evidence:

- Before cleanup, the report-only entropy audit lists the
  `services/slurm_gateway/config.py` retired-template comment as budget-counted.
- After cleanup, the same source comment is allowlisted and not budget-counted,
  while active docs remain budget-counted unless separately cleaned.
- `uv run pytest -q tests/test_entropy_audit_script.py
  tests/test_slurm_route_contract.py` passes.
- `uv run ruff check services/slurm_gateway tests/test_entropy_audit_script.py`
  passes.
- `uv run python scripts/governance/audit_repo_entropy.py --format json` passes
  and does not write `.entropy-baseline/latest.json`.

## Issue #678 Fixture

Fixture level: standard. The issue records evidence after #675, #676, and #677;
it does not change detector logic or clean additional source/docs drift.

Change surface:

- Governance entropy evidence/worklog documentation under `docs/governance/**`.

Workflow evidence exception:

- This reviewed fixture and task checkbox evidence record #678 workflow status.
  They do not broaden #678 implementation scope beyond evidence recording.

Must preserve:

- The report-only audit remains non-mutating and does not write
  `.entropy-baseline/latest.json`.
- The evidence distinguishes non-archive active budget from archive material.
- Remaining archive budget findings are not silently claimed as fixed by #678.

Must add/change:

- Record the current report-only audit command, metadata, and non-archive
  budget-counted route/path delta after #675-#677.
- Record owner disposition for any remaining active route/path findings; if the
  active remainder is zero, record that no active owner mapping is required.

Risk packs considered:

- Public API / CLI / script entry: not selected - no API, CLI, or detector
  behavior change.
- Config / project setup: not selected - no config change.
- File IO / path safety / overwrite: selected only to verify the audit does not
  write `.entropy-baseline/latest.json`.
- Schema / columns / units / field names: not selected - no schema change.
- Auth / permissions / secrets: not selected - no auth or secret handling
  change.
- Concurrency / shared state / ordering: not selected - evidence-only change.
- Resource limits / large input / discovery: selected - the entropy audit scans
  tracked text and must remain report-only.
- Legacy compatibility / examples: selected - evidence must not erase or
  misclassify archive material.
- Error handling / rollback / partial outputs: not selected - no runtime errors.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected - this is a governance worklog
  update.

Domain packs:

- No runtime/scientific domain packs selected. This is evidence recording only.

Required #678 evidence:

- `uv run python scripts/governance/audit_repo_entropy.py --format json
  >/tmp/entropy-678-current.json` passes.
- Non-archive budget-counted route/path findings decrease from 36 to 0, or any
  remaining active findings have explicit owner issue/reason mapping.
- `.entropy-baseline/latest.json` is unchanged.

## Decisions

### 1. Treat line count as an entry criterion, not the whole diagnosis

Files over 1000 lines must enter the structural governance inventory. Files
between 500 and 1000 lines are reviewed when they show responsibility mixing,
many import families, frequent conflict churn, or compatibility logic. The
budget is intentionally paired with responsibility and import-family metrics so
single-purpose generated/data/fixture files are not split mechanically.

Alternative considered: fail every file above 1000 lines immediately. Rejected
because it would force large, risky rewrites of compatibility facades and
production-closure validators before the project has caller inventories and
lane contracts.

### 2. Freeze facade growth before removing facade code

`scheduler.py` and `chain.py` still protect downstream imports and monkeypatch
paths. The first governance step is an inventory: symbol, real owner, caller,
reason retained, removal condition, and verification. Guard tests then prevent
new non-forwarding implementation, new re-export groups, or new cross-domain
import families unless the inventory and budget are updated.

Alternative considered: immediately delete private re-exports. Rejected because
existing tests and downstream code still rely on those compatibility surfaces.

### 3. Decompose production closure by lane contracts

Production-closure files should move toward lane modules such as Docker
security, readonly DB, API/browser proof, published logs, producer identity,
dependency proof, scheduler evidence, live proof, manual ops receipt, and final
aggregation. Aggregators remain stable entrypoints but only compose structured
lane results.

Alternative considered: split by arbitrary line ranges. Rejected because it
would reduce file length while preserving mixed ownership and increasing
navigation cost.

### 4. Burn down active document budget, not total historical mentions

The 36 non-archive budget-counted findings are the active cleanup target.
Current OpenSpec/docs should either use canonical path/route language or mark
old terms with machine-readable historical/compatibility semantics. Archived
evidence remains visible but gains status metadata so the audit can narrow
allowlists.

Alternative considered: suppress `openspec/changes/archive/**` entirely.
Rejected because archive contents still provide evidence and can contain
misleading current-looking text unless status is explicit.

### 5. Add local agent context where shallow patterns are most contagious

Scoped `AGENTS.md` files should be added first under
`services/orchestrator/`, `services/production_closure/`, `apps/api/`, and
`apps/frontend/`. Each should contain only local invariants: dependency
direction, state ownership, error/evidence model, compatibility rules, docs
freshness, and verification commands. `openspec/glossary.md` should define
canonical governance terms used by these controls.

Alternative considered: expand root `AGENTS.md`. Rejected because the root file
is generated and already broad; adding local rules there makes context harder to
apply and maintain.

## Risks / Trade-offs

- **Risk: mechanical file splitting creates more entropy.** Mitigation: require
  owner/lane contracts and behavior-preserving tests before line-count
  reduction is claimed.
- **Risk: facade freezes block necessary fixes.** Mitigation: allow bugfixes
  that do not add ownership surface; require inventory updates only for new
  compatibility or import-family growth.
- **Risk: active document cleanup weakens historical evidence.** Mitigation:
  require canonical update or machine-readable marker, not deletion.
- **Risk: scoped instructions drift from code.** Mitigation: include freshness
  checks and verification commands in the new instruction files.
- **Risk: audit numbers improve without real architecture improvement.**
  Mitigation: issue acceptance must include measured budget deltas plus
  structural evidence such as reduced aggregator-owned logic, bounded import
  families, or guarded compatibility surfaces.

## Migration Plan

1. Add budget/reporting support for large source files and responsibility
   signals without failing CI.
2. Inventory `scheduler.py` and `chain.py`, then add guard tests to stop facade
   growth.
3. Define production-closure lane contracts, then extract one lane at a time
   behind stable entrypoints.
4. Clean active OpenSpec/docs budget-counted route/path drift and adjust
   detector allowlists only where text is already explicitly retired.
5. Add archive status metadata and audit support.
6. Add scoped instructions and glossary entries.
7. Re-run report-only entropy audit, focused tests, and OpenSpec validation;
   write no new baseline unless explicitly requested by a maintainer.

Rollback is by reverting the specific issue PR. Guard-only issues should fail
closed in report-only mode and must not delete compatibility code or archive
evidence.
