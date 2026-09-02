# Close six test-oracle follow-ups on one test-only pass

## Why

Six open issues each name a test oracle that is weaker than the behaviour it
claims to pin — a fixture input nobody asserts on (#1745), a guard disjunct no
test isolates (#1733), a structural invariant that checks guard presence but
not the DELETE window it exists to certify (#1642), a hand-copied expected
dict that is the only oracle for an observability floor and already let one
real defect through (#1800), two wall-clock-budgeted tests whose budgets a slow
oracle machine can exhaust — subprocess-spawn latency for one, a slow
stage transition for the other (#1613), and five declared
residuals of the permanence-refusal guard that have measurements but no
tracking (#1649). All six are `db-free`, pure `tests/` changes with no
production behaviour change, filed by prior PRs as explicit follow-ups. Line
cites are against `origin/master` `9785e52d`.

## What Changes

One PR, one OpenSpec change, serial implementation, all under `tests/`:

- **#1745** — `tests/test_scheduler_lineage.py::test_unusable_earliest_clone_row_still_resolves_lineage_on_the_db_free_plane`
  (`:175-208`) asserts the constructed index entry carries `usable_flag is
  False` before the index is published, so the fixture wiring at
  `tests/lineage_state_index_fixtures.py:100` becomes an observed quantity.
- **#1733** — one new test next to the two provider-snapshot tests in
  `tests/test_scheduler_file_provider_refresh.py` (`:687-756`) isolates the
  `before != after` disjunct of `read_provider_snapshot`
  (`packages/common/provider_atomic.py:139`): content bytes and digest
  unchanged, `mode` changed by `os.chmod` between the payload read and the
  second preimage capture.
- **#1642** — `tests/test_timescale_write_guard_wire_site_invariant.py` gains
  a predicate that every `DELETE FROM <schema>.<table>` literal targeting a
  `HYPERTABLES_GUARDED` pair carries both a `valid_time >=` and a
  `valid_time <=` bound, factored into a helper pinned against synthetic
  bounded/unbounded/one-sided literals and applied to the repository scan.
- **#1800** — `tests/test_production_scheduler.py` gains a payload-driven
  lane-presence property over the shared
  `_incident_scheduler_evidence_payload` fixture: every source
  `restart_reconcile` lane that carries an `outcomes` sequence must appear in
  the bounded output. The nine `== _expected_bounded_restart_reconcile()`
  exact-equality assertions stay; two docstrings are corrected.
- **#1613** — the two load-sensitive tests
  (`tests/test_shud_runtime.py::test_execute_receipt_keeps_recovery_outcomes_when_the_observed_trail_is_long`,
  `tests/test_warm_start_chaining.py::test_cohort_reservation_records_each_models_warm_start_identity`)
  get budgets a slow runner cannot exhaust: `timeout_seconds=300` for the
  two watcher-held-stub tests, and a `job_timeout_seconds=120` default on the
  shared `_orchestrator` test helper (plus its eleven direct sites in the
  victim's file) with a pin on that default. The diagnosis task reproduced
  both failures verbatim with deterministic harnesses and refuted the
  cross-test shared-state reading (design D6).
- **#1649** — residuals 1 and 2 are closed in
  `tests/test_production_scheduler.py::test_scheduler_state_failure_holds_no_second_permanent_code_refusal_list`:
  identity pins (`__module__` + `__qualname__`) for every inventoried
  function and class name, and value pins for all eighteen module-level
  constants (was five). Residuals 3, 4 and 5 are scale limits with measured
  reasons not to close and are recorded as declared bounds in the spec, not
  fixed.

No production module changes. Guard mutations used for red proofs are
reverted before commit (`git diff -- packages services workers` is empty).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hypertable-compression`: the wire-site invariant requirement gains the
  DELETE-window predicate as structural, self-executing coverage (#1642).
- `scheduler-registry-refresh`: the provider-snapshot requirement gains the
  metadata-only divergence scenario and its isolation obligation (#1733).
- `job-retry-mechanism`: the format-insensitive coverage requirement's
  declared bounds are re-drawn — constant-value pins cover all eighteen
  constants, function/class identity pins close the same-kind reflective
  rebind leg up to identity forgery, and residuals 3/4/5 are stated as bounds
  (#1649).
- `runtime-evidence-and-operations`: the bounded observability floor gains
  the payload-driven lane-presence coverage obligation and its declared
  residual (#1800).

#1745 and #1613 change no requirement text: the lineage resolver's
`usable_flag` non-filter is already the fixture's stated contract, and #1613
is test-infrastructure robustness.

## Impact

- `tests/test_scheduler_lineage.py`, `tests/test_scheduler_file_provider_refresh.py`,
  `tests/test_timescale_write_guard_wire_site_invariant.py`,
  `tests/test_production_scheduler.py`, `tests/test_shud_runtime.py`,
  `tests/test_warm_start_chaining.py`, and (only if the diagnosis routes the
  fix there) the `_orchestrator` helper in `tests/test_orchestration_chain.py`.
- Oracle: local pytest + `ruff`; node-27 receipts for the #1733 mutation
  (acceptance demands both platforms) and for the #1613 mechanism harnesses
  run against the fixed tests (load itself never produced a red in 20
  iterations; the harnesses are the oracle). node-22 is not involved.
- Recorded deviation from #1613's original acceptance text: the issue body
  asks to name a cross-test shared-state leak; the issue author's second
  comment re-attributes the failures to CPU competition on wall-clock
  budgets. This change follows the comment, and the diagnosis task states
  what evidence rules the shared-state reading in or out.
