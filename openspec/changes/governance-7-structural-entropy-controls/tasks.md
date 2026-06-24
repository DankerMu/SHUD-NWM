## 1. Structural File Budget

- [x] 1.1 Add a report-only structural file-budget check for tracked source
  files with `>1000` mandatory-governance and `500-1000` review-zone
  classification.
  Evidence: focused tests cover hard-budget, yellow-zone, generated/data/fixture
  exemption, and no baseline write:
  - tracked source with `1001` physical lines and no exemption -> appears in the
    structural budget as mandatory-governance with path, module, line count, and
    owner action.
  - tracked source with `500` to `1000` physical lines -> appears as yellow-zone
    review-only unless mixed-ownership signals are present.
  - generated, data-table, fixture, or protocol-like tracked source above the
    thresholds -> appears under governed exemptions, not ungoverned oversized
    implementation files.
  - report-only and explicit hard-gate commands -> do not create or modify
    `.entropy-baseline/latest.json`.
- [x] 1.2 Add the structural budget summary to governance reporting without
  enabling a CI hard gate.
  Evidence: report output includes oversized source files, yellow-zone files,
  governed exemptions, and top modules; existing entropy counts remain stable:
  - JSON metadata/summary preserves existing finding schema, `summary_counts`,
    `budget_counted_count`, `gate_eligible_count`, and exit-code semantics.
  - Markdown output includes the structural budget summary without changing the
    existing heatmap, high-spread, or prioritized-target sections.
  - `.github/workflows/governance.yml` remains report-only; its CI config touch
    is limited to shallow checkout plus targeted structural base-ref fetch for
    the report contract, and hard-gate mode remains explicit opt-in only.
- [x] 1.3 Add ownership-surface growth detection for oversized files.
  Evidence: focused tests cover bugfix edits that do not add surface, new import
  family/public entrypoint/parser/validation lane detection, generated/data/
  fixture exemptions, trend-safe reporting, and no baseline write:
  - oversized source changed only by a local bugfix-like edit -> no
    ownership-growth signal.
  - oversized source gaining a new import family -> ownership-growth signal.
  - oversized Python source gaining a multiline `from ... import (...)` block
    or an indented import -> ownership-growth signal.
  - oversized source gaining a public entrypoint or compatibility symbol ->
    ownership-growth signal.
  - oversized source gaining parser or validator responsibility -> ownership-
    growth signal.
  - every ownership-growth signal is report-only and points to inventory/update
    action rather than requiring immediate file splitting.
- [x] 1.4 Record current oversized source-file dispositions.
  Evidence: inventory covers at least `scheduler.py`, `chain.py`,
  `two_node_e2e_evidence.py`, `readiness_validation.py`, `apps/api/main.py`,
  and `M11MapLibreSurface.tsx`, with per-file priority, owner, disposition, and
  follow-up issue mapping. See
  `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md`.

## 2. Compatibility Facade Governance

- [x] 2.1 Create a compatibility inventory for
  `services/orchestrator/scheduler.py`.
  Evidence: inventory records compatibility export groups, real owner modules,
  known callers/tests, retention reasons, removal conditions, and verification
  commands. See
  `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`.
- [x] 2.2 Create a compatibility inventory for
  `services/orchestrator/chain.py`.
  Evidence: inventory records stage/manifest/reservation/retry/tile-publisher/
  worker/persistence facade groups, real owner modules, known callers/tests,
  retention reasons, removal conditions, caller migration paths, and
  verification commands. See
  `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`.
- [x] 2.3 Add guard tests that fail on new facade re-exports, new monkeypatch
  aliases, new non-forwarding facade implementation, or new import-family
  growth unless the corresponding inventory is updated.
  Evidence: `scripts/governance/audit_repo_entropy.py` now emits the
  report-only `compatibility_facade_guard` metadata section and
  `compatibility-facade-growth.*.inventory-required` findings when
  `scheduler.py` or `chain.py` grows facade surface without matching inventory
  coverage. `tests/test_entropy_audit_script.py` covers current-repo zero
  signals, scheduler owner-module re-export including annotated, dotted, and
  multi-target aliases, scheduler imported symbol,
  scheduler private monkeypatch alias, guard-hook-only inventory matching,
  chain non-forwarding local implementation, sync/async forwarding-to-local
  and local-to-forwarding definition transitions, async local implementation,
  chain project import-family growth, metadata-complete inventory updates, and
  bare-token inventory rejection.

## 3. Production Closure Lane Decomposition

- [x] 3.1 Define the `two_node_e2e_evidence` lane inventory and contracts.
  Evidence: metadata, Docker preflight/security, readonly DB, API/browser,
  logs, Slurm, compute summary, display summary, manual ops receipt,
  source-scope/cross-plane, producer identity/source artifact, and final
  aggregation surfaces each have owner module plans, input contracts,
  output/result shapes, blocker/finding code namespaces, focused verification
  commands, retention conditions, and extraction readiness notes. See
  `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`; focused evidence
  is `uv run pytest -q tests/test_two_node_e2e_evidence.py` plus
  `openspec validate governance-7-structural-entropy-controls --strict
  --no-interactive`.
- [x] 3.2 Define the `readiness_validation` lane inventory and contracts.
  Evidence: dependency summary, scheduler evidence, live proof receipt,
  exclusion, validation/final aggregation, and shared preflight/environment/
  receipt-artifact surfaces each have owner module plans, input contracts,
  output/result shapes, blocker/error namespaces, focused verification commands,
  retention conditions, and extraction readiness notes. See
  `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md`; issue #673
  verification is `uv run pytest -q tests/test_production_readiness_validation.py`
  plus `openspec validate governance-7-structural-entropy-controls --strict
  --no-interactive`.
- [x] 3.3 Extract the `docker_preflight` lane from
  `two_node_e2e_evidence.py` behind the existing aggregator entrypoint.
  Evidence: `services/production_closure/two_node_e2e_docker_preflight.py`
  owns Docker preflight schema/command/disk contract evaluation behind the
  unchanged `validate_two_node_e2e_evidence(config)` aggregator entrypoint;
  the aggregator passes shared helper callbacks for blocker construction,
  missing-lane/lane-summary adapters, status normalization, current-run,
  stale-lane, recorded-path, and integer parsing semantics, so sibling Docker
  security, readonly DB, API/browser, logs, producer identity, manual ops,
  source-scope/cross-plane, simple live lanes, and final aggregation remain out
  of scope. Focused parity coverage now asserts PASS summary path/checksum,
  redacted evidence shape, missing lane, unknown schema, failed command, missing
  DockerRootDir, current-run blockers, unsafe paths, missing resource evidence,
  missing/invalid/low disk evidence, missing/failed command evidence, producer
  blockers, blocker codes, and final status. Verification:
  `uv run pytest -q tests/test_two_node_e2e_evidence.py -k
  "docker_preflight"` (19 passed), `uv run pytest -q
  tests/test_two_node_e2e_evidence.py` (696 passed), `uv run ruff check
  services/production_closure tests/test_two_node_e2e_evidence.py` (passed),
  and `openspec validate governance-7-structural-entropy-controls --strict
  --no-interactive`.

## 4. Active Document Entropy Burn-Down

- [x] 4.1 Clean the active OpenSpec route/path drift budget.
  Evidence: active `openspec/specs/**` route/path findings are rewritten to
  current canonical terms or marked as machine-readable historical/compatibility
  context; before cleanup `/tmp/entropy-675-before.json` had 41 active
  `openspec/specs/**` route/path findings, 30 budget-counted; after cleanup
  `/tmp/entropy-675-after.json` has 31 active findings, all allowlisted and 0
  budget-counted, so no follow-up owner is required for the remaining
  historical/compatibility contexts. Global `budget_counted_count` dropped from
  259 to 229, and `baseline_written=false` in both audits. `openspec validate
  --all --strict --no-interactive` passes.
- [x] 4.2 Clean the active governance/module docs drift budget without deleting
  historical context.
  Evidence: `docs/governance/entropy-report.example.md`,
  `docs/governance/ROLE_BOUNDARY.md`, and `docs/modules/00_module_index.md`
  either use canonical paths or have audit-recognized retired/historical
  markers. Before cleanup `/tmp/entropy-676-before.json` had 45 active
  `docs/governance/**` + `docs/modules/**` route/path findings, 5
  budget-counted and unallowlisted; after cleanup `/tmp/entropy-676-after.json`
  has 40 active findings, 0 budget-counted, and 0 unallowlisted. Global
  `budget_counted_count` dropped from 229 to 224, and `baseline_written=false`
  in both audits.
- [x] 4.3 Clean `services/slurm_gateway/config.py` retired-path source/comment
  drift without changing runtime template behavior.
  Evidence: source comment is audit-classified as retired/historical context;
  focused entropy tests and relevant Slurm gateway config tests pass. Before
  cleanup `/tmp/entropy-677-before.json` had one
  `services/slurm_gateway/config.py` placeholder-path finding, budget-counted
  and unallowlisted; after cleanup `/tmp/entropy-677-after.json` has one
  finding for the same source comment, allowlisted and not budget-counted.
  Global `budget_counted_count` drops from 224 to 223, and
  `baseline_written=false` in both audits.
- [x] 4.4 Re-run report-only entropy audit and record the active
  budget-counted delta.
  Evidence: non-archive budget-counted route/path findings decrease from 36, or
  every remaining active finding maps to an explicit owner issue with reason.
  `/tmp/entropy-678-current.json` was generated with
  `uv run python scripts/governance/audit_repo_entropy.py --format json`;
  metadata: `finding_count=448`, `budget_counted_count=223`,
  `gate_eligible_count=0`, `baseline_written=false`. Non-archive
  budget-counted route/path findings are now 0, so no remaining active owner
  mapping is required. Archive route/path budget semantics remain follow-up work
  for #679-#681.

## 5. Archive Status Semantics

- [x] 5.1 Define archive/superseded front matter or standardized markers for
  historical documents and archived OpenSpec artifacts.
  Evidence: documentation names required fields or markers, including status,
  current authority, and supersession target where applicable.
  `docs/governance/DOC_STATUS.md` now defines YAML front matter and
  section-level `Archive status:` blocks with required `status`,
  `current_authority`, `superseded_by`, `status_since`, `archive_scope`, and
  `retained_for` semantics; it directs agents to resolve current authority
  before treating preserved archive/superseded text as actionable and states
  incomplete markers remain visible for triage.
- [x] 5.2 Update the entropy audit classification to use complete archive
  status semantics instead of broad archive-path suppression.
  Evidence: tests cover complete archive marker allowlisting, missing marker
  visibility, incomplete marker visibility, and no global ignore of archived
  material. The classifier must remain report-only and must not write
  `.entropy-baseline/latest.json`. `uv run pytest -q
  tests/test_entropy_audit_script.py` passed with 326 tests; `uv run ruff
  check scripts/governance/audit_repo_entropy.py
  tests/test_entropy_audit_script.py` passed; `openspec validate
  governance-7-structural-entropy-controls --strict --no-interactive` passed.
  `/tmp/entropy-680-current.json` report-only metadata:
  `finding_count=448`, `budget_counted_count=225`,
  `gate_eligible_count=0`, `baseline_written=false`; current complete-marker
  allowlist count is 0 because #681 owns first marker materialization, and the
  remaining archive route/path findings stay visible for triage.
- [ ] 5.3 Apply archive/superseded markers to the first governed materialization
  set.
  Evidence: target set covers archive or current-superseded documents that
  mention legacy route/path/topology text in the latest report; any remaining
  archive findings are documented with owner follow-up issues; report-only
  audit remains explainable without broad path suppression.

## 6. Scoped Agent Context And Glossary

- [ ] 6.1 Create `openspec/glossary.md` with canonical entropy-governance terms.
  Evidence: glossary contains the required terms from
  `scoped-agent-context-governance`.
- [ ] 6.2 Add entropy/control-plane audit coverage for scoped instruction
  presence, freshness, and glossary term/link checks.
  Evidence: tests cover missing scoped instructions for high-entropy
  directories, stale scoped context, and missing glossary linkage.
- [ ] 6.3 Add `services/orchestrator/AGENTS.md`.
  Evidence: file defines local dependency direction, scheduler/chain facade
  compatibility rules, state ownership, mutation fences, and focused
  verification commands; scoped audit passes.
- [ ] 6.4 Add `services/production_closure/AGENTS.md`.
  Evidence: file defines lane ownership, evidence schema/redaction/path-safety
  rules, readonly boundary invariants, and focused verification commands;
  scoped audit passes.
- [ ] 6.5 Add `apps/api/AGENTS.md`.
  Evidence: file defines app bootstrap/routing boundaries, role guard
  expectations, and focused API verification commands; scoped audit passes.
- [ ] 6.6 Add `apps/frontend/AGENTS.md`.
  Evidence: file defines map surface ownership, live-vs-mocked evidence rules,
  frontend verification commands, and glossary links; scoped audit passes.

## 7. Final Verification

- [ ] 7.1 Run local verification for the governance controls.
  Evidence: `uv run pytest -q tests/test_entropy_audit_script.py` plus new
  focused tests pass; `uv run ruff check .` passes.
- [ ] 7.2 Validate OpenSpec artifacts.
  Evidence: `openspec validate governance-7-structural-entropy-controls
  --strict --no-interactive` and `openspec validate --all --strict
  --no-interactive` pass.
- [ ] 7.3 Keep baseline write policy unchanged.
  Evidence: report-only and hard-gate entropy commands do not modify
  `.entropy-baseline/latest.json`; any baseline update remains a separate
  maintainer-confirmed action.
