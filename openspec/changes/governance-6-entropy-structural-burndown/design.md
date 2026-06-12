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

## Open Questions

- Whether legacy display redirect aliases should ever be retired. This change
  keeps them.
- Whether orchestrator compatibility shims should be removed in a second
  Governance-6 follow-up after all internal callers migrate.
