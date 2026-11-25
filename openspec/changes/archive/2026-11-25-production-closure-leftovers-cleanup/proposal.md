# Remove what the production_closure lane deletion left behind

## Why

The lane deletion of 2026-11-19 (#3221) removed the production_closure
validation lanes, the twelve-lane aggregator and `scripts/m24_gateway_proof.py`.
Issue #3233 lists what stayed behind. Checked again on master on 2026-11-25:

| Item of the issue | State on master | This change |
|---|---|---|
| 1. Alert runbooks without an emitter, two unbacked spec requirements | The four runbooks, `rollback-drills.md` and both requirements were already removed during the review of the deletion. Three live runbooks still tell the operator to look for metric names nothing emits. | Rewire those three lines to the alert keys the monitor emits. |
| 2. `READINESS_ITEM_STATUSES` | Still defined; read only by its own parity row. | Delete the constant, the row and the spec sentence. |
| 3. Entropy overlay scores `services/production_closure` P1 / high | Still there; the directory is now ten small modules. | Drop the overlay entry, adjust the summary test, regenerate the baseline. |
| 4. "twelve-lane" prose in the tiering runbook | Three sentences. | Rewrite them without the deleted aggregator. |
| 5. Gateway job type `smoke` | Mapped in two places, template present, no submitter. | Delete the job type, its template and the `m24_smoke` profile override that existed for it. |
| 6. Two tests named after deleted fixtures | Unchanged. | Rename. |

Nothing here changes runtime behaviour of a production path. It is dead
surface that misleads operators and reviewers.

## Decisions

- **D1 `SLURM_TERMINAL_STATES` stays.** It is the drift anchor the gateway
  state map is checked against (`real-slurm-gateway-contract`).
- **D2 The `smoke` job type is deleted, not documented.** Its only
  submitter was the deleted proof script; no runbook describes a manual
  submission; gateway liveness is what the health endpoint and the
  scheduler's own gateway check report. A manual submit of `smoke` through
  the gateway API after this change gets the existing refusal for a job
  type without a template (`TemplateNotFoundError`, 404
  `TEMPLATE_NOT_FOUND`). Whether an operator ever submitted `smoke` by hand
  cannot be read from the repository; the owner's standing instruction for
  this epic is to remove dead surface. The `m24_smoke` entry under `overrides` in
  `config/resource_profiles.yaml` was that script's model id and goes with
  it; a scheduler test that uses the string as an arbitrary override key
  keeps doing so (it writes its own file).
- **D3 The three alert runbooks are rewired, not rewritten.** Only the line
  that names the alert payload changes: it now names the `alert` keys of
  `services/orchestrator/monitoring.py` and the payload fields that exist
  (`alert`, `severity`, `observed`, `runbook`, and `threshold` on
  `failed_cycles`, `slurm_queue_backlog` and `scheduler_stale` only), and
  the two closing sentences that still speak of "final readiness" receipts
  of the deleted lanes are cut back to what is true. Whether
  `source-latency.md` is the right runbook for `scheduler_evidence_missing`
  and `scheduler_stale` is not decided here.
- **D4 The entropy summary test pins the orchestrator, not a count.** The
  `services/production_closure` row of the baseline exists only because of
  the overlay (it has no finding of its own); without the entry the row
  goes away, `total_modules` drops by one and `modules_with_high_entropy`
  becomes whatever the writer computes. The test asserts that
  `services/orchestrator` is scored `high` on structure and that
  `services/production_closure` either has no row or is not `high`; its
  `file_count` pin on that module is dropped (the `services/slurm_gateway`
  pin keeps covering per-module counts).
- **D5 The baseline is rewritten last, on a clean checkout of the archived
  tree.** The writer refuses a tree with untracked files and counts
  `openspec/`, so it runs after `openspec archive`. The tracked baseline
  dates from 2026-11-19; the rewrite therefore also picks up every change
  merged since. The PR reports the summary before and after and names
  which differences come from this change (the `production_closure` row,
  `total_modules`, `modules_with_high_entropy`, one file less under
  `infra`, the archived change's files under `openspec`, any move of the
  finding count) and which are drift.

## Not in this change

The issue's comments add leftovers of a different kind (spec homes and
environment comments). They stay open on #3233 for a second PR, which this
one does not close:

- the live monitor (`services/orchestrator/monitoring.py`, five alerts) has
  no spec requirement since the two readiness requirements were removed;
- the rejection of NaN / Inf SHUD output lost its spec scenario when
  `staging-end-to-end-closure` was retired;
- the scenario "Deferred auth is a release blocker" of
  `production-ops-readiness` still asks for an artifact whose only producer
  was a deleted lane;
- the comment in `infra/env/node27-download.example` that says the download
  path does not write the database, against a GFS adapter that writes
  `met.forecast_cycle`;
- scheduler keys in host env files that no scheduler unit reads.

## What does not change

- `services/production_closure/**` (the C2 read-only DB validator and the
  C4 acceptance tool stay).
- Every other job type, template and profile entry; `SLURM_TERMINAL_STATES`
  and every other vocabulary set.
- The docker/compose lane and its files (#3231).
- The bodies of the two renamed tests.

## Impact

- `packages/common/status_vocab.py`, `services/slurm_gateway/config.py`,
  `config/job_type_templates.yaml`, `config/resource_profiles.yaml`,
  `infra/sbatch/smoke.sbatch` (deleted),
  `scripts/governance/write_entropy_baseline.py`, `.entropy-baseline/latest.json`.
- Tests: `tests/test_status_vocab_parity.py`,
  `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`,
  `tests/test_entropy_audit_baseline_writer_summary.py`,
  `tests/test_scheduler_evidence_decidability.py`,
  `tests/test_river_ts_read_path_surrogate_keys.py`.
- Docs: `docs/runbooks/failed-basin-retry.md`, `slurm-backlog.md`,
  `source-latency.md`, `tier-node27-timeseries-storage.md`,
  `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`.
- Spec: `status-vocabulary` (one requirement modified).
