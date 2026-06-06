# Issue #255 — Fresh forecast cycle ingestion: scheduler wiring closure (worklog)

Branch: `feat/m23-255-generic-canonical-ingestion`. Closes the real gap behind the
m24 §P dependency gate (#287): the generic production daemon could not actually
self-drive fresh-cycle canonical ingestion.

## Why this PR exists (honest correction of the #287 reconciliation)

`tasks.md` recorded #255 CLOSED and the m24 §P gate (#287) "OPEN, not BLOCKED",
asserting "fresh ingestion is usable by the m24 daemon (§4)". A codex audit + two
independent read-only analyses proved that claim was over-optimistic:

- The generic scheduler (`plan-production --continuous --submit`) only planned
  forecast + downstream candidates. `from_env` always wired the canonical
  readiness gate (`_MetStoreCanonicalReadinessProvider`, queries
  `met.canonical_met_product`). A brand-new cycle with **zero** canonical was
  hard-blocked at `scheduler.py` readiness gate (`canonical_incomplete` /
  `canonical_identity_mismatch`) and **never reached the chain**, so the daemon
  never submitted `download`/`convert`.
- The #292 daemon receipt corroborates: `submitted_count=0`, blocked on
  `canonical_identity_mismatch`; #291's `download`/`convert` (jobs 6029/6030)
  were **manual** `nhms-canonical convert` runs on the login node, not daemon
  self-drive.

The chain machinery (download→convert→forcing→forecast→parse→frequency→publish
via the Slurm gateway, #288/#290/#291) was complete. The only missing piece was
the scheduler decision to admit a zero-canonical cycle into the chain.

## Change (commit 2a9d4d9 + review fixes)

`services/orchestrator/scheduler.py` only (chain.py / CLI unchanged):

- **Classify the readiness block by canonical row count.** A cycle with
  `candidate_row_count == 0` (no canonical rows at all) is a *fresh ingestion*,
  not a corrupt/partial canonical. It is stamped
  `state_evidence.fresh_ingestion = {required: true, mode: "full_chain"}`, carries
  **no** `restart_stage`, and flows through to `_execute_candidates` →
  `orchestrate_cycle`, so the chain runs from `download`. A cycle that already has
  canonical rows but fails identity/variable/lead checks keeps the **hard block**
  (never swallow bad/half-baked canonical).
- **Fresh candidates bypass the optional in-process forcing producer**
  (`_produce_forcing_for_candidates`, gated by `NHMS_PRODUCTION_FORCING_ENABLED`):
  there is no canonical yet to drive it; the chain's Slurm `forcing` stage
  produces forcing on the compute node. Ready-canonical candidates are unchanged
  and still call the producer.

### Review fixes (3-pack cross-review → verify gate)

- **MAJOR**: an empty forecast horizon (`expected_leads == []`, reason
  `no_expected_leads` / `missing_canonical_variables`) also reports zero rows; it
  is a broken horizon/policy config, **not** a fresh cycle.
  `_canonical_evidence_is_fresh_zero_row` now requires non-empty `expected_leads`
  → such cycles keep the hard block.
- **MAJOR**: cohort routing now has a single source of truth —
  `_candidate_restart_stage` returns `None` for fresh full-chain candidates, so a
  residual `restart_stage` merged from a retry `state_decision` can never divert
  a fresh cycle off the `(0, "full")` full-chain cohort.
- **Safety guard (confirmed airtight)**: provider-unavailable / query-failed
  readiness (`canonical_unavailable`, no `candidate_row_count` key) is never
  reclassified as fresh — a DB outage cannot make the daemon mass-submit
  full-chain runs.

## Tests (`tests/test_production_scheduler.py`)

Fresh happy path (skips in-process forcing, no `restart_stage`,
`fresh_ingestion.mode == full_chain`, submitted), identity-contract propagation,
partial-canonical-still-blocks (`candidate_row_count > 0`), ready-canonical
regression unchanged, double-submit (active Slurm job → skip), ingestion-stage
failure does not fabricate success, `no_expected_leads` stays blocked,
fresh-with-residual-restart forces full cohort, `canonical_unavailable` guard
stays blocked, multi-basin same-cycle merges into a single download.

## Verification

- node-22 (oracle, real DB, real `/tmp`): `uv run pytest -q
  tests/test_production_scheduler.py tests/test_orchestration_chain.py
  tests/test_qhh_scripts_static.py` (#293 guardrail included).
- `uv run ruff check .` clean. Local macOS runs mask the object-store fixtures
  via the `/tmp`-symlink issue; node-22 is authoritative.
- Live receipt (daemon self-drives a fresh cycle end-to-end via the gateway):
  pending — see `m24-multibasin-continuous-daemon-live` §4 go-live.
