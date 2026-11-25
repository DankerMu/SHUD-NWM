# Tasks

Fixture level: `standard`. Risk packs: gateway job-type mapping (a deleted
job type must be refused by the existing `TemplateNotFoundError` path, and no
other job type's template may change); governance baseline (a regenerated
`.entropy-baseline/latest.json` and the hard gate's counts); shared status
vocabulary (a constant removed from a module that many sites import).

Must preserve: every other entry of `DEFAULT_JOB_TYPE_TEMPLATES` and of
`config/job_type_templates.yaml` (the defaults stay a subset of the YAML
mapping; the YAML-only `compute_frequency_array` is untouched);
every other model override and the `job_type_overrides` of
`config/resource_profiles.yaml`; `SLURM_TERMINAL_STATES` and every other
vocabulary set and its parity row; what the two fixture tests that borrow
`smoke.sbatch` as a narrowing template prove (a job type mapped to a
template that is not the array template is refused / narrowed); the bodies
and assertions of the two renamed tests; the entropy hard gate with
`hard_gate_failing_count == 0` and a finding count equal to master's
measured in the same session (247 on 2026-11-25), or the difference
explained; `services/production_closure/**` untouched.

Order at the end (orchestrator): `openspec validate --strict`, then
`openspec archive production-closure-leftovers-cleanup --yes`, then the
greps of 7.2, then the baseline of 3.3. The greps cannot be empty before
the archive, because this change's own files and the live spec name the
searched words.

## 1. Alert runbooks (orchestrator)

- [ ] 1.1 `docs/runbooks/failed-basin-retry.md`, `slurm-backlog.md`,
  `source-latency.md`: the "Confirm the alert payload includes ..." line
  names the `alert` keys that link the runbook (`failed_cycles`;
  `slurm_queue_backlog` and `slurm_monitor_unavailable`;
  `scheduler_evidence_missing` and `scheduler_stale`) and the payload's
  real fields (`threshold` only where the monitor sets it). The closing
  "final readiness" sentences of `slurm-backlog.md` and `source-latency.md`
  are cut back to what is true. Nothing else changes.

## 2. Status vocabulary

- [ ] 2.1 Delete `READINESS_ITEM_STATUSES` from
  `packages/common/status_vocab.py` (and from `__all__` or any export list
  if it is named there), its pinned literal and parity row in
  `tests/test_status_vocab_parity.py`, and any allowlist / scanner entry
  that names it. The comments beside it in the module and the parity
  test's parameter id `readiness-only` are brought in line (a case that
  exists only for the deleted set goes; one that also proves something
  else is renamed). After the archive, `git grep READINESS_ITEM` outside
  `openspec/changes/archive/**` returns nothing.
- [ ] 2.2 Spec delta `status-vocabulary` (orchestrator): the sentence that
  names the constant is removed.

## 3. Entropy baseline

- [ ] 3.1 Drop the `services/production_closure` entry from
  `STRUCTURAL_HOTSPOT_MODULES` in
  `scripts/governance/write_entropy_baseline.py`.
- [ ] 3.2 `tests/test_entropy_audit_baseline_writer_summary.py`: replace
  `modules_with_high_entropy >= 2` by
  `modules["services/orchestrator"]["structure"]["score"] == "high"` and
  "`services/production_closure` has no row, or its structure score is not
  `high`" (D4); drop the `file_count` pin of `services/production_closure`.
  The test builds its baseline in memory, so it must pass without the
  tracked file being rewritten.
- [ ] 3.3 (orchestrator, last; D5) On a clean checkout of the archived
  tree, `uv run python scripts/governance/write_entropy_baseline.py`;
  commit `latest.json` and the one archive file it creates. Report the
  summary and the module keys before and after, split into what this
  change causes and what is drift since 2026-11-19.

## 4. Tiering runbook prose (orchestrator)

- [ ] 4.1 `docs/runbooks/tier-node27-timeseries-storage.md`: the three
  sentences that refer to the deleted aggregator (one of them says it
  "remains fail-closed" without the word "twelve-lane") are rewritten; the
  C3 / C4 description stays. After the archive, `git grep -n -i -E
  "twelve-lane|full-scope aggregator|producer-complete bundle"` outside
  `openspec/changes/archive/**` returns nothing.

## 5. The `smoke` job type

- [ ] 5.1 Remove `smoke` from `DEFAULT_JOB_TYPE_TEMPLATES`
  (`services/slurm_gateway/config.py`) and from
  `config/job_type_templates.yaml`; delete `infra/sbatch/smoke.sbatch`;
  remove the `m24_smoke` entry from `config/resource_profiles.yaml`.
  Search for any list, README, schema, allowlist, packaging manifest or
  test that enumerates templates or job types and bring it in line; report
  each place.
- [ ] 5.2 The two tests that borrow `smoke.sbatch` as a narrowing template
  (`tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`)
  switch to another existing non-array template and keep proving the same
  thing. The test that submits `job_type="smoke"` to the mock gateway as an
  arbitrary label switches to an existing non-array job type (the mock
  does not check job types), unless what it proves needs an unmapped label
  (then say so and keep it).
- [ ] 5.3 New test: a submission with `job_type="smoke"` through the real
  gateway class raises `TemplateNotFoundError` (code `TEMPLATE_NOT_FOUND`)
  and `subprocess.run` is never called.
- [ ] 5.4 `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` (orchestrator):
  the sentence that lists the smoke template in the canonical template set
  no longer names the file.

## 6. Test names

- [ ] 6.1 Rename
  `test_a_summary_tier_submitted_pass_still_passes_the_readiness_reader`
  (`tests/test_scheduler_evidence_decidability.py`) and
  `test_hydro_map_plan_fixture_names_indexes_that_the_migration_chain_creates`
  (`tests/test_river_ts_read_path_surrogate_keys.py`) to names that say
  what their bodies assert; bring their docstrings in line; nothing else in
  the bodies changes. Any non-archived reference to the old ids (shard
  lists, allowlists, docs) is updated; report each.

## 7. Evidence

- [ ] 7.1 Local: `uv run --no-sync pytest -q` on
  `tests/test_status_vocab_parity.py`, `tests/test_real_slurm_gateway.py`,
  `tests/test_slurm_array_contract.py`, `tests/test_job_array.py`,
  `tests/test_scheduler_evidence_decidability.py`,
  `tests/test_river_ts_read_path_surrogate_keys.py`,
  `tests/test_live_monitoring.py`, `tests/test_status_literal_scan.py`,
  `tests/test_orchestrator.py`, `tests/test_production_scheduler.py -k
  resource_profile_split`, and every `tests/test_entropy_audit_*.py` file
  (named explicitly); `uv run --no-sync ruff check --no-cache .`;
  `openspec validate production-closure-leftovers-cleanup --strict
  --no-interactive`; markdown lint of the edited documents; the entropy
  hard gate as stated under "Must preserve".
- [ ] 7.2 Greps of the acceptance criteria, after the archive, each outside
  `openspec/changes/archive/**`: the pattern of 4.1,
  `smoke.sbatch` (the two validation documents that write their own
  `smoke.sbatch` under a scratch path are listed, not edited),
  `READINESS_ITEM`, the two old test ids: nothing left.
- [ ] 7.3 node-27, temporary detached worktree of the PR head: the test
  files of 7.1. Receipt on the PR.
