## Why

The current entropy snapshot shows that Governance-5 reduced several automation
and boundary classes, but the remaining high-risk drift is now concentrated in
current runbook wording, mocked-vs-live evidence boundaries, and two oversized
orchestrator modules. This change turns those findings into a governed,
reviewable burn-down plan rather than another broad cleanup queue.

## What Changes

- Store the current entropy state in `.entropy-baseline/latest.json` so later
  work can compare trend and burn-down evidence against a concrete snapshot.
- Correct current runbook and validation language so `/` is the active
  single-map display entrypoint, while `/hydro-met` and other old display paths
  are legacy redirect aliases or historical evidence.
- Resolve the remaining gate-eligible broad Playwright API mock findings by
  separating mocked regression specs from live display proof.
- Reconcile the `DOC_STATUS.md` artifact ownership wording that currently
  misses the literal `.dockerignore` ownership term expected by the governance
  audit.
- Stage a large but behavior-preserving decomposition of
  `services/orchestrator/scheduler.py` and `services/orchestrator/chain.py`
  into small PRs with compatibility shims, stable evidence keys, and focused
  tests.
- Harden the approved production copyback/runtime boundary for two-node
  operation: `NHMS_OBJECT_STORE_COPYBACK_ROOT` remains compute-only, shared run
  products are validated with no-follow traversal, and q_down display artifacts
  become visible only after required copyback succeeds.
- Keep Governance Audit report-only; this change does not enable entropy
  hard-gates, rewrite Slurm reservation semantics, or delete legacy
  compatibility behavior.

## Capabilities

### New Capabilities

- `governance-entropy-baseline`: Captures and preserves the current entropy
  snapshot for future trend comparison without making baseline writes an
  incidental audit side effect.
- `evidence-boundary-hardening`: Aligns current display runbooks and
  mocked/live evidence classification with M26 route authority and governance
  audit expectations.
- `orchestrator-structural-burndown`: Decomposes scheduler and forecast-chain
  responsibilities behind stable compatibility shims while preserving lease,
  reservation, reconcile, retry, candidate-state, and evidence invariants.

### Modified Capabilities

<!-- No existing product capability is modified. This change plans governance,
documentation, evidence, and behavior-preserving internal refactor work. -->

## Impact

- Baseline: `.entropy-baseline/latest.json`.
- Governance docs: `docs/governance/DOC_STATUS.md`,
  `docs/governance/entropy-budget.md`, and related route/evidence status docs.
- Current runbooks: `docs/runbooks/two-node-production-e2e-plan.md`,
  `docs/runbooks/two-node-deployment-overview.md`,
  `docs/runbooks/node-27-bringup-checklist.md`, and historical/superseded
  banners for old MVP runbooks where needed.
- Frontend evidence: `apps/frontend/e2e/m11-routes.spec.ts`,
  `apps/frontend/e2e/monitoring.spec.ts`, live display Playwright profile, and
  validation docs that distinguish mocked regression from live receipt.
- Orchestrator internals: `services/orchestrator/scheduler.py`,
  `services/orchestrator/chain.py`, and new small modules under
  `services/orchestrator/`.
- Production runtime/copyback boundary: `services/tile_publisher/publisher.py`,
  `apps/api/runtime_mode.py`, two-node Docker runtime validation, compute/display
  env examples, and the two-node deployment docs/runbooks.
- Tests: focused scheduler, chain, retry, backfill, role-boundary, entropy
  audit, and frontend mocked/live evidence tests.
- Non-goals: no CI hard-gate enablement, no `.entropy-baseline/latest.json`
  writes from normal report generation, no deletion of legacy redirect aliases,
  no Slurm submit/reservation/reconcile semantic changes, and no broad
  repository cleanup outside the listed findings.
