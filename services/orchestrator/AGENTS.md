# Orchestrator Agent Instructions

This file scopes root `AGENTS.md` for `services/orchestrator/`. The current
authority for shared vocabulary is `openspec/glossary.md`; use terms such as
active entrypoint, compatibility facade, current authority, and budget-counted
finding exactly as the glossary defines them.

## Required Reading

- `openspec/changes/governance-7-structural-entropy-controls/specs/scoped-agent-context-governance/spec.md`
- `docs/runbooks/two-node-deployment-overview.md`
- `openspec/glossary.md`

`docs/runbooks/two-node-deployment-overview.md` is a runbook freshness anchor.
Its banner distinguishes M22 design intent from the current physical
deployment; do not treat historical writer/readonly wording as live host
assignment without checking root `AGENTS.md` and `docs/governance/ROLE_BOUNDARY.md`.

## Ownership Boundaries

- Keep dependency direction inward. `services/orchestrator` may compose shared
  packages, worker utilities, Slurm gateway clients, and production-closure
  evidence only through documented service contracts. Do not import from
  `apps/api` or `apps/frontend`.
- Put new scheduler behavior in the narrow owner module when one exists:
  `scheduler_state.py`, `scheduler_lease.py`, `scheduler_discovery.py`,
  `scheduler_candidates.py`, `scheduler_evidence.py`, or
  `scheduler_execution.py`. Treat `scheduler.py` as a compatibility facade and
  orchestration shell unless the active entrypoint itself is changing.
- Put new chain behavior in the narrow owner module when one exists:
  `chain_stages.py`, `chain_types.py`, `chain_stage_execution.py`,
  `chain_array_accounting.py`, `chain_manifests.py`, `reservation.py`,
  `retry.py`, `persistence.py`, `production_contract.py`, or
  `packages/common/contracts/time_normalization.py`. Treat `chain.py` compatibility exports and
  monkeypatch paths as facade surface.
- A new facade name in `scheduler.py` or `chain.py` is an explicit import or
  alias line; `tests/test_facade_name_resolution.py` must stay green. Do not
  grow facade surface without a reader.

## State And Mutation Fences

- Scheduler state, leases, reservations, retry state, manifests, and pipeline
  persistence each belong to their owner modules. Do not duplicate state caches
  in facades or callers to make a single test pass.
- Mutating DB rows, Slurm jobs, published artifacts, runtime roots, evidence
  files, or NFS paths must stay behind the existing role, path, and transaction
  guards. The compute role is the compute/Slurm oracle and connects to no live
  database; the PostgreSQL `:55433` of the retired node-22 host is archived and
  must not be connected to; node-27 is the live DB/display oracle.
- Preserve bounded evidence writes, approved-root checks, no-clobber behavior,
  and current-run binding when touching scheduler evidence or chain publication
  paths. A failure path should produce a stable domain blocker or exception, not
  a silent partial mutation.

## Focused Verification

Always run the issue-required governance checks after changing this file or
orchestrator scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For scheduler facade or owner-module changes, run the focused scheduler suites,
commonly:

```bash
uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py
```

For chain facade or owner-module changes, run the focused chain suites,
commonly:

```bash
uv run pytest -q tests/test_orchestration_chain.py tests/test_retry_cancel_consistency.py
```
