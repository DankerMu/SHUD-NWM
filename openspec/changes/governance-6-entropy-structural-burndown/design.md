## Context

The June 12, 2026 entropy snapshot records 356 governance findings, 169
budget-counted findings, and 4 gate-eligible findings. Governance-5 already
separated report-only semantics, retired active-tree path guards, route evidence
cleanup, and `apps.api.*` layer-inversion cleanup. The remaining work now has
two different risk profiles:

- Low-to-medium effort evidence and documentation cleanup, where current
  runbooks still make old display aliases look active and mocked Playwright
  regressions can be mistaken for live proof.
- High-effort structural cleanup in `services/orchestrator/scheduler.py` and
  `services/orchestrator/chain.py`, where large files mix leases, discovery,
  candidate state, Slurm submit, reservation, stage execution, evidence, and
  manifest behavior.

The second category must be treated as behavior-preserving refactor work. It is
not a license to simplify legacy compatibility, rename evidence fields, or
change Slurm lifecycle semantics.

## Goals / Non-Goals

**Goals:**

- Commit an explicit entropy baseline snapshot after maintainer request.
- Reduce active route/evidence drift in current docs and Playwright
  classification.
- Make the four current gate-eligible governance findings directly actionable.
- Split orchestrator structural work into small, testable PRs with stable
  compatibility shims.
- Preserve all public, database, Slurm, evidence, and compatibility contracts.

**Non-Goals:**

- No automatic baseline writes from `audit_repo_entropy.py`.
- No CI entropy hard-gate enablement.
- No one-shot rewrite of `scheduler.py` or `chain.py`.
- No repository/SQL behavior rewrite.
- No deletion of legacy state compatibility, old field aliases, legacy display
  redirects, or historical evidence.
- No status, reason, error code, schema version, or evidence-key renaming.
- No changes to `services/orchestrator/reservation.py`,
  `services/orchestrator/reconcile.py`, or `services/orchestrator/retry.py`
  protocol semantics in the first structural stage.

## Decisions

### D1. Baseline writes are explicit maintainer actions

`.entropy-baseline/latest.json` is written only because the maintainer asked for
the current state to be captured. The reproducible write path is a
maintainer-only `scripts/governance/write_entropy_baseline.py` helper. Normal
audit commands remain report-only and must not update the baseline. If a
previous baseline exists, the maintainer-only helper archives it before
replacement.

### D2. Current route authority wins over runbook drift

`apps/frontend/src/App.tsx` and `docs/governance/DOC_STATUS.md` define the M26
single-map route authority: `/` is the active display entrypoint; `/hydro-met`,
`/forecast`, `/meteorology`, `/flood-alerts`, `/basins/:id`, and
`/segments/:id` are compatibility redirects or historical references. Current
runbooks must use `/` plus `/ops` for live display proof, with a separate
legacy redirect smoke if needed.

### D3. Mocked regression stays useful but cannot be live proof

Broad `page.route('**/api/v1/**')` mocks may remain in deterministic mocked
regression specs when clearly classified. Live display evidence must use the
live profile and explicit runtime URLs, and it must not register broad API
mocks.

The audit detector must also catch multiline broad-mock registrations. A live
or live-looking spec must not be able to avoid governance detection by splitting
`page.route(` and `'**/api/v1/**'` across lines.

### D4. Structural decomposition uses compatibility shims

Each orchestrator extraction PR first moves code into a new module and keeps the
old import or private-method surface as a shim where tests or downstream code
still depend on it. Shims are retired only after tests and callers migrate in a
later focused PR.

### D5. Lease and submit ordering are protected invariants

Scheduler pass ordering remains:

1. lock root preflight,
2. acquire lease,
3. runtime root preflight,
4. startup reconcile,
5. discovery and candidate building,
6. lease-lost fence,
7. pre-execution evidence reservation,
8. mutation/submission.

Forecast-chain submission remains reserve-before-sbatch, lost reservation skips
submission, array and non-array submissions carry idempotency comments, and the
reservation binds only after a real Slurm job id is obtained.

### D6. Candidate-state compatibility is not simplified in stage one

Legacy candidate-state rows, manual retry, active Slurm sync, permanent/cancel
guards, terminal success, fresh zero canonical cases, and identity validation
must keep current evidence payloads and compatibility aliases. The first stage
may move helpers, not reinterpret them.

## Structural Refactor Sequence

1. Extract scheduler lease primitives into `scheduler_lease.py`.
2. Extract candidate-state and legacy identity validation into
   `scheduler_state.py`.
3. Extract discovery and backfill selection into `scheduler_discovery.py`.
4. Extract scheduler candidate construction into `scheduler_candidates.py`.
5. Extract scheduler execution and evidence helpers into
   `scheduler_execution.py` and `scheduler_evidence.py`.
6. Extract chain types and stage catalog into `chain_types.py` and
   `chain_stages.py`.
7. Extract chain stage reservation/submission/polling into
   `chain_stage_execution.py`.
8. Extract chain manifest and model-run assembly helpers into
   `chain_manifests.py`.
9. Extract chain array aggregation/accounting helpers into
   `chain_array_accounting.py`.

The order is intentional. Scheduler lease, state, discovery, candidate,
execution, and evidence extractions complete before chain extraction begins.
Lease and state helpers reduce the largest cross-cutting risk before moving
candidate selection or stage submission.

## Risks / Mitigations

- **Risk: duplicate Slurm submissions.** Mitigation: every chain extraction must
  keep reserve-before-sbatch and bind-after-submit tests green.
- **Risk: lease regression lets stale holder mutate.** Mitigation: lease tests
  must cover CAS renew, atomic replace, live-holder non-reclaim, cross-host TTL,
  and lease-lost fencing.
- **Risk: legacy candidate-state behavior changes silently.** Mitigation:
  extract helpers with golden evidence tests and no field renames.
- **Risk: docs cleanup hides useful historical evidence.** Mitigation: use
  historical/superseded banners rather than deleting old evidence.
- **Risk: broad mock detector misses multiline live-looking mocks.** Mitigation:
  harden `audit_repo_entropy.py` with multiline coverage tests before relying on
  the finding as a future gate.
- **Risk: refactor PRs become too large.** Mitigation: each issue owns one
  module boundary and a focused test set; no issue combines docs, frontend, and
  orchestrator code.

## Verification Strategy

- OpenSpec Stage 3 review-fix loops continue until Design Consistency, Spec
  Completeness, and Tasks Executability reviews report no P0/P1 findings.
- Governance audit JSON and Markdown remain report-only and baseline-safe.
- `openspec validate governance-6-entropy-structural-burndown --strict
  --no-interactive`.
- Focused route-authority grep over current docs and runbooks.
- Frontend mocked/live evidence tests from `apps/frontend`.
- Orchestrator focused pytest slices for lease, candidate state, backfill,
  candidate selection, execution evidence, chain reservation/submission, array
  accounting, and manifests.
- `uv run --no-sync ruff check services/orchestrator tests/...` for every
  structural PR.
- Final epic closure evidence includes a last cross-review with no P0/P1
  findings before the Governance-6 epic is closed.

## G6-01 Entropy Baseline Report-Only Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:

- `scripts/governance/audit_repo_entropy.py` CLI report entrypoint.
- `.entropy-baseline/latest.json` as the persisted baseline artifact.
- `tests/test_entropy_audit_script.py` CLI/report-only regression coverage.

Must preserve:

- Normal JSON and Markdown audit commands are report-only and keep
  `metadata.baseline_written` false.
- Explicit hard-gate mode still prints parseable stdout even when it exits
  non-zero.
- Existing `.entropy-baseline/latest.json` bytes are not created, replaced,
  archived, rewritten, reformatted, or touched by report commands.

Must add/change:

- Test evidence that JSON, Markdown, and hard-gate report commands leave the
  existing repository baseline byte-for-byte unchanged.
- Test evidence that hard-gate JSON stdout remains parseable while preserving
  report-only baseline semantics.

Risk packs considered:

- Public API / CLI / script entry: selected - `audit_repo_entropy.py` CLI modes
  and exit codes are the public boundary for governance automation.
- File IO / path safety / overwrite: selected - the invariant is no incidental
  write to `.entropy-baseline/latest.json`.
- Schema / columns / units / field names: selected - JSON metadata fields
  `baseline_written`, `baseline_path`, `baseline_exists`, and hard-gate fields
  must remain stable.
- Error handling / rollback / partial outputs: selected - hard-gate failure
  exits non-zero but stdout remains parseable and no baseline side effects
  occur.
- Legacy compatibility / examples: selected - existing report tests and
  downstream governance consumers must keep current JSON/Markdown shape.
- Config / project setup: not selected - no dependency or environment setup
  change.
- Auth / permissions / secrets: not selected - no credentials or permission
  boundary.
- Concurrency / shared state / ordering: not selected - no concurrent writer is
  introduced in G6-01.
- Resource limits / large input / discovery: not selected - no scan breadth or
  parser limit change.
- Release / packaging / dependency compatibility: not selected - no package or
  dependency change.
- Documentation / migration notes: not selected - issue scope is executable
  verification, not docs wording.

Domain risk packs:

- Run manifest / QC provenance: not selected - baseline report metadata is
  governance provenance, not NHMS run manifest/QC evidence.
- Published NHMS artifacts / display identity: not selected - no published
  model/display artifact identity changes.
- Other NHMS domain packs: not selected - no geospatial, time-series,
  numerical, PostGIS, Slurm, or provider behavior changes.

Invariant Matrix:

- Governing invariant: audit report commands may observe
  `.entropy-baseline/latest.json` but only the future maintainer-only writer may
  create, replace, archive, or modify it.
- Source-of-truth identity/contract: `.entropy-baseline/latest.json` byte
  content and JSON report metadata fields `baseline_path`,
  `baseline_exists`, and `baseline_written`.
- Producers: none for G6-01 - baseline production belongs to G6-02.
- Validators/preflight: `audit_repo_entropy.build_report`,
  `audit_repo_entropy._metadata`, CLI argument parsing in
  `audit_repo_entropy.main`.
- Storage/cache/query: `.entropy-baseline/latest.json` is read-observed only.
- Public routes/entrypoints: `python scripts/governance/audit_repo_entropy.py
  --format json|markdown` and `--mode hard-gate --format json`.
- Frontend/downstream consumers: governance tests and future CI automation that
  parse JSON stdout.
- Failure paths/rollback/stale state: hard-gate failure exit still emits JSON
  stdout and does not write, archive, or roll back baseline files.
- Evidence/audit/readiness: `tests/test_entropy_audit_script.py` and manual
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ...` commands.
- Regression rows:
  - Existing baseline + JSON report command -> parseable JSON with
    `baseline_written=false` and unchanged baseline bytes.
  - Existing baseline + Markdown report command -> Markdown report sections and
    unchanged baseline bytes.
  - Existing baseline + hard-gate JSON command -> parseable JSON stdout,
    expected hard-gate exit code, and unchanged baseline bytes.

Non-goals:

- No `scripts/governance/write_entropy_baseline.py` implementation in G6-01.
- No CI entropy hard-gate enablement.
- No trend dashboard or baseline comparison UI.

## G6-02 Maintainer-Only Entropy Baseline Writer Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Change surface:

- New `scripts/governance/write_entropy_baseline.py` maintainer-only CLI.
- `scripts/governance/audit_repo_entropy.py` as the report producer consumed by
  the writer, without changing report modes into writers.
- `.entropy-baseline/latest.json` and archived
  `.entropy-baseline/<timestamp>.json` baseline artifacts.
- `tests/test_entropy_audit_script.py` writer and report-only regression
  coverage.

Must preserve:

- Normal JSON, Markdown, and hard-gate audit report commands remain report-only
  and do not mutate `.entropy-baseline/latest.json`.
- The existing tracked baseline schema remains usable for future comparison:
  branch, commit, summary metrics, module heatmap, high-spread patterns, and
  cleanup priorities stay machine-readable.
- Existing `.entropy-baseline/latest.json` is archived before replacement
  rather than overwritten without a recoverable copy.

Must add/change:

- A maintainer-only baseline writer command that explicitly writes
  `.entropy-baseline/latest.json` when invoked.
- Replacement behavior that archives a pre-existing latest snapshot under a
  timestamped `.entropy-baseline/<timestamp>.json` path before writing a new
  latest snapshot.
- Tests proving explicit write, archive-before-replace, report-only separation,
  and parseable generated baseline content.

Risk packs considered:

- Public API / CLI / script entry: selected - `write_entropy_baseline.py` is a
  new maintainer-facing command with observable exit/output behavior.
- File IO / path safety / overwrite: selected - this issue intentionally writes
  and archives baseline files and must avoid accidental overwrite/loss.
- Schema / columns / units / field names: selected - baseline JSON fields must
  preserve the comparison contract.
- Error handling / rollback / partial outputs: selected - failed archive/write
  paths must not silently destroy the old latest baseline.
- Legacy compatibility / examples: selected - existing report-only tests and
  current tracked baseline consumers must remain compatible.
- Resource limits / large input / discovery: selected - the writer must not
  introduce unbounded extra reads beyond the existing audit report generation
  and bounded baseline file operations.
- Config / project setup: not selected - no new dependency or environment
  setup.
- Auth / permissions / secrets: not selected - maintainer-only means explicit
  local invocation, not credential or authorization integration.
- Concurrency / shared state / ordering: selected - archive-before-replace
  ordering is a shared-state transition even without multi-process locking.
- Release / packaging / dependency compatibility: not selected - no package or
  dependency change.
- Documentation / migration notes: not selected - issue scope is helper and
  tests; PR evidence covers usage.

Domain risk packs:

- Run manifest / QC provenance: not selected - baseline JSON is governance
  provenance, not NHMS run manifest/QC evidence.
- Published NHMS artifacts / display identity: not selected - no model/display
  artifact publication.
- Other NHMS domain packs: not selected - no geospatial, time-series,
  numerical, PostGIS, Slurm, or provider behavior changes.

Invariant Matrix:

- Governing invariant: only an explicit `write_entropy_baseline.py` maintainer
  invocation may create, archive, or replace entropy baselines; all normal audit
  report commands remain read-only.
- Source-of-truth identity/contract: `.entropy-baseline/latest.json` and
  archived `.entropy-baseline/<timestamp>.json` bytes plus baseline JSON fields
  `version`, `timestamp`, `repo`, `branch`, `commit`, `summary`, `modules`,
  `high_spread_patterns`, and `cleanup_priorities`.
- Producers: `write_entropy_baseline.py` consuming
  `audit_repo_entropy.build_report`; no producer role for
  `audit_repo_entropy.py` report modes.
- Validators/preflight: writer argument parsing, baseline directory/path
  resolution, existing latest detection, archive path construction, and JSON
  serialization validation.
- Storage/cache/query: `.entropy-baseline/` directory, `latest.json`, and
  timestamped archive files.
- Public routes/entrypoints: `python scripts/governance/write_entropy_baseline.py`
  plus unchanged `audit_repo_entropy.py --format json|markdown|--mode
  hard-gate`.
- Frontend/downstream consumers: future trend comparison tooling and governance
  tests reading baseline fields.
- Failure paths/rollback/stale state: archive-before-replace ordering; failures
  must not leave the previous latest silently lost.
- Evidence/audit/readiness: `tests/test_entropy_audit_script.py`, focused
  writer CLI tests in temporary repos, report-only regression tests, and manual
  writer invocation on a temporary baseline root.
- Regression rows:
  - No existing latest + explicit writer invocation -> creates
    `.entropy-baseline/latest.json` with required comparison fields and no
    archive.
  - Existing latest + explicit writer invocation -> archives old bytes under
    `.entropy-baseline/<timestamp>.json` before writing new latest.
  - Existing latest + JSON/Markdown/hard-gate audit report commands -> no
    baseline mutation, covered by G6-01 regression.
  - Invalid or blocked archive/write path -> stable non-zero failure without
    silently deleting the old latest baseline.

Boundary-surface checklist:

- Shared helper roots: `audit_repo_entropy.build_report` is reused but remains
  report-only.
- Public entrypoints: new writer CLI and existing audit CLI.
- Write/delete/overwrite surfaces: `.entropy-baseline/latest.json` and
  timestamped archive files only.
- Staging/publish/rollback surfaces: archive-before-replace ordering for
  explicit baseline replacement.
- Producer/consumer evidence boundaries: generated baseline fields must bind to
  the same audit report snapshot.
- Unchanged downstream consumers: existing report JSON/Markdown tests and
  G6-01 baseline immutability tests.

Non-goals:

- No automatic baseline writes from `audit_repo_entropy.py`.
- No CI entropy hard-gate enablement.
- No trend dashboard or baseline comparison UI.
- No credential/auth integration for maintainer-only local invocation.

## G6-03 Current Route-Authority Runbooks Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:

- `docs/runbooks/two-node-production-e2e-plan.md`.
- `docs/runbooks/two-node-deployment-overview.md`.
- `docs/runbooks/node-27-bringup-checklist.md`.
- Historical MVP runbooks under `docs/runbooks/` that keep pre-M26
  `/hydro-met` browser steps.

Must preserve:

- Current node-27 read-only display and `/ops` operational evidence guidance.
- Historical M21/MVP evidence value where old `/hydro-met` steps document past
  receipts or known blockers.
- M26 route authority from `docs/governance/DOC_STATUS.md`: `/` is the active
  single-map display entrypoint; `/ops` remains active for operations; legacy
  display aliases redirect or provide compatibility context only.

Must add/change:

- Current live browser proof in current runbooks uses `/` plus `/ops`.
- `/hydro-met`, `/forecast`, `/meteorology`, `/flood-alerts`, `/basins/:id`,
  `/segments/:id`, and `/overview` mentions in current runbooks are classified
  as legacy redirect, compatibility, or historical context.
- Old MVP runbooks that intentionally preserve pre-M26 `/hydro-met` evidence
  contain a visible historical/superseded notice pointing to current M26 route
  authority.

Risk packs considered:

- Public API / CLI / script entry: not selected - no runtime route, API, or CLI
  behavior changes.
- Config / project setup: not selected - no environment or dependency change.
- File IO / path safety / overwrite: not selected - docs-only edits.
- Schema / columns / units / field names: not selected - no data/schema fields.
- Auth / permissions / secrets: not selected - no permission or secret surface.
- Concurrency / shared state / ordering: not selected - no state transition.
- Resource limits / large input / discovery: selected - route-authority grep
  over current runbooks is the acceptance evidence.
- Legacy compatibility / examples: selected - old route aliases and historical
  MVP evidence must remain as redirect/historical context, not be deleted or
  recast as active current pages.
- Error handling / rollback / partial outputs: not selected - no executable
  failure path.
- Release / packaging / dependency compatibility: not selected - docs-only.
- Documentation / migration notes: selected - this issue's implementation is
  runbook migration to current route authority.

Domain risk packs:

- Published NHMS artifacts / display identity: selected - current browser proof
  wording must bind display evidence to the active `/` entrypoint and `/ops`.
- Run manifest / QC provenance: not selected - no run manifest or QC evidence
  schema changes.
- Other NHMS domain packs: not selected - no geospatial, time-series,
  numerical, PostGIS, Slurm, provider, or SHUD runtime behavior changes.

Required evidence:

- Focused route-authority grep over `docs/runbooks/two-node-production-e2e-plan.md`,
  `docs/runbooks/two-node-deployment-overview.md`, and
  `docs/runbooks/node-27-bringup-checklist.md` shows old display aliases are
  redirect, compatibility, or historical context rather than active live proof
  instructions.
- Focused banner check over `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`
  and `docs/runbooks/qhh-mvp-smoke-evidence.md` shows a visible
  historical/superseded notice pointing to M26 route authority.
- Manual diff review confirms current browser proof uses `/` plus `/ops`, with
  `/hydro-met -> /` only as legacy redirect smoke where retained.
- `openspec validate governance-6-entropy-structural-burndown --strict
  --no-interactive`.

Invariant Matrix:

- Governing invariant: current runbooks must name `/` plus `/ops` as the active
  live browser proof surface, while legacy display aliases are only redirect,
  compatibility, or historical evidence.
- Source-of-truth identity/contract: `docs/governance/DOC_STATUS.md` M26
  Display Route Authority and
  `openspec/changes/governance-6-entropy-structural-burndown/specs/evidence-boundary-hardening/spec.md`.
- Producers: docs-only edits in the owned runbooks.
- Validators/preflight: route-authority grep over current runbooks and manual
  banner/diff review.
- Storage/cache/query: none - no runtime state or artifact storage changes.
- Public routes/entrypoints: documentation mentions of `/`, `/ops`,
  `/hydro-met`, `/forecast`, `/meteorology`, `/flood-alerts`, `/basins/:id`,
  `/segments/:id`, and `/overview`; no route code changes.
- Frontend/downstream consumers: operators and agents following current
  runbooks for live display evidence.
- Failure paths/rollback/stale state: stale active-looking legacy route
  instructions must be converted to redirect/historical/compatibility wording,
  not silently left as current proof.
- Evidence/audit/readiness: issue diff, route-authority grep, historical banner
  check, OpenSpec validation, and docs review.
- Regression rows:
  - Current runbook live browser proof -> uses `/` plus `/ops`.
  - Current runbook old display alias mention -> classified as redirect,
    compatibility, or historical context.
  - Historical MVP runbook with preserved `/hydro-met` steps -> visible
    historical/superseded notice points to current M26 route authority.
  - Unchanged runtime route code -> no frontend/API behavior changes.

Non-goals:

- No frontend route code, Playwright implementation, audit parser, or
  orchestrator changes.
- No removal of legacy redirect aliases.
- No node-27 timing metric acceptance; local node-27 access timing is excluded
  by maintainer instruction.

## G6-04 Route-Authority Governance Grep Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Mandatory expanded triggers:

- Public CLI/report output: `audit_repo_entropy.py --format json|markdown` and
  hard-gate summaries expose these findings to governance automation.
- Finding schema fields: route findings depend on stable `check_id`,
  `allowlist_reason`, `allowlist_key`, `budget_counted`, and `gate_eligible`
  fields.
- Bounded text resource discovery: the check scans repository text files and
  must keep existing skip and byte-limit behavior.
- Legacy compatibility/examples: legacy route aliases remain valid only in
  redirect, compatibility, or historical contexts.

Change surface:

- `scripts/governance/audit_repo_entropy.py` route-authority audit detection,
  classification, allowlist-key generation, and report fields.
- `tests/test_entropy_audit_script.py` focused route-authority regression
  coverage.

Must preserve:

- `stale-display-route-token` remains report-only: unallowlisted route drift is
  budget-counted but not hard-gate eligible.
- Existing stale `/hydro-met` and `HydroMetPage` findings, allowlist keys,
  module assignment, JSON/Markdown report shape, and baseline report-only
  behavior remain compatible for current tests.
- M26 route authority from `docs/governance/DOC_STATUS.md` and
  `apps/frontend/src/App.tsx`: `/` is active display, `/ops` is active
  operational display, and legacy aliases remain compatibility redirects.

Downstream compatibility axes:

- JSON and Markdown report consumers still receive the same finding family and
  stable finding fields.
- Hard-gate summaries still exclude `stale-display-route-token` from
  `HARD_GATE_CHECK_IDS`.
- Existing allowlist keys for historical/pre-M26 evidence, redirect evidence,
  milestone summaries, frontend redirect tests, and HydroMetPage provenance
  remain compatible.
- Baseline/report-only behavior remains unchanged; the audit observes files but
  does not write `.entropy-baseline/latest.json`.
- `docs/governance/DOC_STATUS.md` and `apps/frontend/src/App.tsx` remain the
  route-authority sources; issue #460's "six legacy forms" acceptance wording is
  interpreted through those sources, so `/overview` is included.

Must add/change:

- Current docs/runbooks scans classify `/overview`, `/hydro-met`, `/forecast`,
  `/meteorology`, `/flood-alerts`, `/basins/:id`, `/segments/:id`, and concrete
  `/basins/<id>` / `/segments/<id>` mentions as one of: historical evidence,
  redirect alias, compatibility context, or drift.
- Active-looking current-runbook usage outside those allowlist classes is an
  unallowlisted `stale-display-route-token` finding.
- Tests cover every legacy route alias named by current route authority, not
  only `/hydro-met`.

Risk packs considered:

- Public API / CLI / script entry: selected - `audit_repo_entropy.py` report
  output is consumed by governance automation and hard-gate summaries.
- Config / project setup: not selected - no dependency or environment change.
- File IO / path safety / overwrite: not selected - read-only repo scan; no
  baseline or artifact writes.
- Schema / columns / units / field names: selected - finding fields
  `check_id`, `allowlist_reason`, `allowlist_key`, `budget_counted`, and
  `gate_eligible` must stay stable.
- Auth / permissions / secrets: not selected - no permission or secret surface.
- Concurrency / shared state / ordering: not selected - no shared state
  mutation.
- Resource limits / large input / discovery: selected - route grep must use the
  existing bounded text-file discovery semantics and avoid broad untracked or
  ignored artifact scans.
- Legacy compatibility / examples: selected - old aliases remain valid only as
  redirect, compatibility, or historical evidence; historical docs must not be
  recast as active drift.
- Error handling / rollback / partial outputs: not selected - no new public
  failure mode beyond existing report construction.
- Release / packaging / dependency compatibility: not selected - no packaging
  or dependency behavior change.
- Documentation / migration notes: selected - audit classification is tied to
  `DOC_STATUS.md` route authority and current runbook semantics.

Domain risk packs:

- Geospatial / CRS / basin geometry: not selected - route string
  classification does not change basin geometry or CRS behavior.
- Hydro-met time series / forcing windows: not selected - no forcing or
  forecast-window data is read or produced.
- SHUD numerical runtime / conservation / NaN: not selected - no solver runtime
  behavior.
- PostGIS / TimescaleDB domain behavior: not selected - no database schema or
  query behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler,
  Slurm gateway, or job lifecycle behavior.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider snapshot or external data integration.
- Run manifest / QC provenance: not selected - no run manifest or QC schema.
- Published NHMS artifacts / display identity: selected - audit classification
  protects the current display identity `/` plus `/ops` from stale route drift.

Required evidence:

- Route-authority drift regression: temporary current runbook with an active
  legacy alias instruction -> one unallowlisted `stale-display-route-token`
  finding that is budget-counted and report-only.
- Route-authority allowlist regressions: historical, redirect-alias, and
  compatibility wording -> allowlisted findings with stable allowlist keys.
- Legacy alias coverage regression: `/overview`, `/hydro-met`, `/forecast`,
  `/meteorology`, `/flood-alerts`, `/basins/:id`, `/segments/:id`, concrete
  `/basins/demo`, and concrete `/segments/demo` are all detected.
- Resource discovery regression: ignored or skipped artifact paths containing a
  legacy alias -> no route finding, proving the check stays within existing
  bounded text discovery semantics.
- Focused commands: `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`
  and `uv run --no-sync ruff check scripts/governance/audit_repo_entropy.py
  tests/test_entropy_audit_script.py`.
- `openspec validate governance-6-entropy-structural-burndown --strict
  --no-interactive`.

Non-goals:

- No edits to current runbook prose outside test fixtures.
- No frontend route behavior, redirect implementation, or Playwright profile
  change.
- No entropy baseline rewrite or hard-gate enablement.
- No node-27 timing metric acceptance; local node-27 access timing is excluded
  by maintainer instruction.

Review focus:

- Verify `/overview` is included because current `DOC_STATUS.md` and
  `App.tsx` define it as a legacy redirect alias.
- Verify active-looking current-runbook mentions are not accidentally
  allowlisted by broad words in unrelated context.
- Verify historical, redirect, and compatibility contexts produce distinct,
  stable allowlist reasons/keys.
- Verify scan scope stays aligned to existing tracked text discovery and does
  not introduce unbounded artifact reads.

## Open Questions

- Whether legacy display redirect aliases should ever be retired. This change
  keeps them.
- Whether orchestrator compatibility shims should be removed in a second
  Governance-6 follow-up after all internal callers migrate.
