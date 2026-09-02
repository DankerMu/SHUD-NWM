# Design

Fixture level: expanded · repair intensity: medium · issues: #1745 #1733 #1642 #1800 #1613 #1649
Project profile: NHMS (`openspec/project-profile.md`). Line cites are against
`origin/master` `9785e52d`.

## Context

Every item is a test that fails to observe the thing it names. The change is
confined to `tests/`; production modules are touched only as temporary
mutations for red proofs and restored before commit. The two #1613 victims
are the only cause-unknown item and get a report-only diagnosis task before
their fix is specified in full (D6).

Fixture level is `expanded`, not `compact`, because the issue texts sit on
mandatory expanded triggers (DELETE, path/chmod, concurrency/timing, retry
spec) even though the diff is test-only; repair intensity stays `medium`
because no production surface changes and the one shared TEST helper that
does change (`_orchestrator`, D6) is a widened wall-clock budget whose
consumers are enumerated in `tasks.md` and run locally; `high` would buy an
Invariant Matrix for a test-only budget change.

## Goals / Non-Goals

**Goals:** each named oracle becomes red-capable against the mutation its
issue names; nothing existing is weakened; every test-only fix keeps the
production timeout/guard paths it touches still exercised by some test.

**Non-Goals:** production behaviour changes of any kind (#1800's
fixture↔producer synchronisation, #1649 residuals 3/4/5, #1642's
`tests/integration_helpers.py` teardown DELETE — the last is a teardown, not
a replace path, and stays outside the requirement's subject per #1640);
splitting `tests/test_scheduler_file_provider_refresh.py` (#1101);
`_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS` per-key presence guards.

## Decisions

**D1 (#1745) — observe the fixture input at the call site.** The test builds
the entry with `_entry(..., usable_flag=False)` inline inside the
`_repository(...)` list. Bind it to a name first, `assert
entry["usable_flag"] is False`, then publish. Alternative rejected: a
separate fixture self-test file — a new module for one field.
Red proof: hard-code `"usable_flag": True` at
`tests/lineage_state_index_fixtures.py:100` → this test red, then restore.

**D2 (#1733) — `mode` is the divergence lever.** `ProviderPreimage`
(`provider_atomic.py:52-81`) carries `device/inode/mode/uid/gid/size/mtime_ns/sha256`.
`os.chmod` on the destination between the payload read (call 2 of the
monkeypatched `read_bytes_limited_no_follow`) and the second capture changes
`mode` alone: digest equal, `before != after`. No root, no timestamp
granularity, deterministic on APFS and ext4. `uid/gid` need privileges; an
inode swap needs a same-content second file with matching `mtime_ns` and is
less deterministic. Shape mirrors the ABA test (`:687-727`): `calls == 3`,
`reason == "provider_preimage_changed"`, bytes unchanged. Restore the
original mode in a `finally` so `tmp_path` cleanup is unaffected.
Red proof: delete the `before != after` disjunct at `provider_atomic.py:139`
→ new test red while the two existing tests stay `2 passed` (they do not
isolate this disjunct; the contrast is part of the receipt); receipts on
macOS and node-27 (detached worktree, never the live tree), then restore.

**D3 (#1642) — the window predicate is a substring pair on the same
literal.** Python concatenates adjacent string literals at parse time, so
`"DELETE FROM met.forcing_station_timeseries " "WHERE ... valid_time >= %s AND
valid_time <= %s"` (`workers/forcing_producer/store.py:825-826`,
`packages/common/forcing_domain_handoff_apply.py:827-828`) is ONE
`ast.Constant`, and `workers/output_parser/parser.py:987-992` is one
triple-quoted constant. The predicate: for every call-argument string (via
`_iter_call_argument_strings`) containing `DELETE FROM {schema}.{table}` for
a guarded pair, the same string must contain both `valid_time >=` and
`valid_time <=`. **Equivalent-forms allowlist: exactly those two spellings.**
Half-open `valid_time <` is deliberately NOT admitted: the guard's window
(`check_batch_targets_uncompressed(valid_time_min, valid_time_max)`) is
closed on both ends, and a half-open DELETE would target a different window
than the one certified. A writer that legitimately needs another spelling
widens the allowlist in the same change with a reason. The predicate lives in
a helper (`_delete_literal_is_valid_time_bounded(text) -> bool` or
equivalent) so it can be pinned on synthetic literals in the style of
`_UNADMITTED_MODULE_LEVEL_FORMS` in `tests/test_production_scheduler.py`:
bounded → ok; no predicate → fail; `>=` only → fail; `<=` only → fail;
`>=` + `<` → fail; newline/indent/parameter-style variants of the bounded
form → ok. A repository-scan test applies it to every hit of
`_wire_site_hits()`. Non-guarded DELETEs (`met.interp_weight`,
`met.forcing_version_component`, `core.*`) are outside the subject, and so is
`packages/common/compressed_chunk_cold_probe/shell.py:417` — it carries the
half-open `valid_time < %s` form but sits on `_INTENTIONALLY_UNWIRED_MODULES`
and never reaches the scan; the allowlist is not widened for it.
Red proof: the synthetic negatives are the primary red; additionally strip
`AND valid_time <= %s` from `parser.py:992` → scan test red, then restore.

**D4 (#1800) — payload-driven lane presence, additive.** A property over the
shared fixture: run `scheduler_module._bounded_evidence_payload(payload,
reason="evidence_size_limit_exceeded", max_evidence_bytes=8_000)` (the tier
the existing consumers use and in which `restart_reconcile` is retained);
for every key `k` of the SOURCE `payload["restart_reconcile"]` whose value is
a `Mapping` with an `outcomes` value that is a non-`str`/`bytes` `Sequence`,
assert `k in bounded["restart_reconcile"]` and
`len(bounded[...][k]["outcomes"]) == len(source outcomes)`. The lane
criterion mirrors `_compact_bounded_reconcile_lane`
(`scheduler_evidence_payload.py:405-420`) on purpose: a lane with only
`count` must NOT be demanded (false red). The guard MUST NOT read or import
`_BOUNDED_RESTART_RECONCILE_LANES` — the compactor already iterates that
constant, so a constant-driven property can never go red on the #1797 shape.
The nine exact-equality assertions stay untouched; the guard's docstring
states why (they catch EXTRA keys leaking into the floor, which an existence
property cannot). `test_bounded_evidence_retains_inflight_error_when_only_the_inflight_segment_failed`'s
docstring (`:20502-20503`) states that its exact-shape assertion also pins
lane retention, not only the error key. Declared residual, in the guard's
docstring: a lane that exists only in the producer
(`scheduler_runtime.py:1611-1627`) and was never mirrored into the fixture is
still invisible; fixture↔producer sync is a separate decision.
Red proof: temporarily drop one lane from the loop in
`_compact_bounded_restart_reconcile` (`scheduler_evidence_payload.py:398`) →
property red, then restore.

**D5 (#1649) — close residuals 1 and 2; declare 3/4/5.**
- Residual 1 (same-kind reflective rebind of a `function`/`class` name):
  for every inventoried name whose declared kind is `function` or `class`,
  assert `getattr(value, "__module__", None) ==
  scheduler_state_failure_module.__name__` and `getattr(value,
  "__qualname__", None) == name`. `functools.lru_cache` /
  `functools.wraps` copy both attributes, so the existing no-false-red
  guarantee for decorating a helper is preserved — and that is also the
  declared limit: identity attributes are forgeable, and a `staticmethod`
  wrapper copies them since Python 3.10. The implementer re-measures the
  issue's three same-kind shapes (`setattr(sys.modules[__name__], "<def
  name>", <plain wrapper def>)`, subclass swap of
  `_ForcingSidecarProvenance`, `functools.partial`) and reports which the
  pin catches; a forged-identity wrapper is expected to slip and is recorded
  as the residual's new bound.
- Residual 2 (13 constants without value pins): pin the remaining thirteen
  with literal expected values copied from the source at `9785e52d`
  (`scheduler_state_failure.py:66,74,81,85,215,216,313,321,521,522,964,965,973`).
  Values that reference other constants (`_NON_REGULAR_OBJECT_KINDS` over
  `OBJECT_KIND_DIRECTORY`/`OBJECT_KIND_OTHER`) are pinned as the literal
  strings those constants hold, not by re-reading them. Friction is the
  contract: a legitimate value change now updates one assertion.
- Residuals 3 (callability vs `FunctionType`), 4 (two bounded behavioural
  axes; `OUT_OF_MEMORY`/`POLICY_BLOCKED` off-axis; 25 informative cells) and
  5 (raw-manifest / model-package inline literals) are scale choices with
  measured reasons; they are stated as bounds in the spec delta and in the
  test docstring, not closed. Widening them without a real recurrence buys
  friction for a hypothetical.
Red proof: reflective same-kind rebind of `_remedy_permits_permanent_failure`
to a plain wrapper `def` defined outside the module (harness in the
scratchpad, never committed) → identity pin red; duplicate assignment of one
newly pinned constant with a different value in a temporary module mutation →
value pin red, then restore.

**D6 (#1613) — raise the two victims' wall-clock budgets at the test seam;
diagnosis persisted at `.workplans/pr-1949/diag-1613/DIAGNOSIS.txt` (gitignored
local evidence).**
The report-only diagnosis reproduced both failures verbatim with deterministic
red commands and settled the mechanism and the trigger:
- Victim A (`tests/test_shud_runtime.py:6016`): `f012=bu…` is
  `budget_exhausted`, produced solely by the shared solver deadline
  (`workers/shud_runtime/runtime.py:683`, `:836-838`). The test consumes
  0.85 s of its 30 s budget on an idle machine (60 watcher polls at the 10 ms
  floor + two recovery reruns); the red needs ~29 s consumed. CPU hogs move the
  main solve by 1.00x (it is `sleep`-dominated), so **CPU competition alone is
  refuted for A**: the only lane that can eat the budget is subprocess
  spawn/exec latency (three `Popen` sites; a 14.5 s stall per spawn at the
  stock 30 s budget reproduces the exact string). The issue's cross-test
  shared-state reading is **superseded** (issue author's second comment) and
  was **not reproduced under the diagnosis's conditions**: no module-level
  cache, no env read on the execute path, and the red reproduces in a
  single-test process with a fresh `tmp_path`. It is not excluded as a
  contributor to the observed full-suite reds; the widened budget absorbs the
  latency either way, and the issue's `--ignore=tests/test_safe_fs.py`
  full-suite control is not re-run here (see the oracle paragraph).
- Victim B (`tests/test_warm_start_chaining.py:2740`): the per-stage poll
  deadline (`chain_stage_execution.py:1013-1017`, reached through
  `orchestrate_cycle`) trips in `state_save_qc` when one status-transition
  block exceeds `job_timeout_seconds=5`; slowest lane 0.042 s here, ~0.34 s
  on node-27 (the test runs 8.2x slower there), and load amplified a lane
  7.2x locally — same order as the ~15x node-27 margin, so the issue's CPU
  reading is plausible for B. No non-`tmp_path` roots and no singletons were
  found; the shared-state reading has the same superseded / not-reproduced
  status as for A.
Fix (test-only, minimal, oracles intact):
- A: `_runtime(..., timeout_seconds=300)` at the victim (`:6034`) and at its
  sibling that uses the same watcher-held stub (`:6092`). The kwarg exists
  (`:224-228`). Neither test asserts the timeout path; the stub's own 20 s
  self-cap bounds worst-case wall time. Each site pins
  `runtime.config.timeout_seconds >= 60` right after construction, so a revert
  to the helper's 30 s default is red. The only wall-clock-expiry test in the
  file (`:5888`, explicit `timeout_seconds=10`) is untouched.
- B: `_orchestrator` (`tests/test_orchestration_chain.py:9209`) gains a
  `job_timeout_seconds: float = 120.0` keyword (was hard-coded 5); the eleven
  direct `job_timeout_seconds=5` sites in `tests/test_warm_start_chaining.py`
  (one of them inside the shared `_cohort_orchestrator` helper) and the four
  inline `OrchestratorConfig(job_timeout_seconds=5)` builds in
  `tests/test_orchestration_chain.py` itself (`:4465/4492/4527/4553` at
  `9785e52d`; only the last, the full-cycle publish-root test, reaches the
  real-clock poll loop — the other three are raised for consistency; that
  test's own oracle cannot witness the raise because the publish-root refusal
  fires identically on a timed-out job, so its receipt is a plain before/after
  trace, not a red) are raised to the same value. The four timeout-path tests
  (`test_orchestration_chain.py:4318/4356/4395/12841` at `9785e52d`) build
  their own configs with `job_timeout_seconds=1` AND fake `time.monotonic`,
  and the three `HangingPollClock` tests keep the helper default but install a
  clock that jumps past any budget, so none is affected or slowed. One-line pins assert `_orchestrator`'s and
  `_cohort_orchestrator`'s default is ≥ 60 s with docstrings citing #1613, so
  a revert to a contention-sized budget at either helper seam is red.
- Oracle for the fix: the diagnosis's mechanism reds, re-run against the fixed
  tests — A with `DIAG1613_SPAWN_DELAY=14.5` at the (now 300 s) budget passes;
  B with a 6 s stall injected into the `state_save_qc` status transition
  (red at 5 s, green at 120 s). Both harnesses live in
  `.workplans/pr-1949/diag-1613/` — a gitignored evidence directory, so they
  are **local-only evidence**, run on macOS and in a detached node-27
  worktree; the durable, repo-resident oracle is the set of `>= 60` pins (A:
  both victim sites; B: `_orchestrator` and `_cohort_orchestrator`). A "N runs
  under a CPU hog" receipt is NOT the oracle: load never produced a red on
  either victim in 20 iterations. Issue criterion 2 ("stable green under the
  full suite") is discharged twice: by the mechanism harnesses (which show the
  budget absorbs the reproduced trigger) and by one full-suite run at the
  final HEAD on node-27 in the detached worktree (receipt posted as a PR
  comment). The issue's `--ignore=tests/test_safe_fs.py` A/B control is not
  repeated: its lever was suite composition (one run per arm at the stock
  budgets, and the issue's second comment shows the same command flipping
  red/green without any change), and the mechanism it pointed at is the
  latency the widened budget absorbs; a red in the full-suite run at HEAD
  would reopen it.
- Declared limits, recorded (not fixed here): (i)
  `test_run_shud_main_solve_and_recovery_share_one_timeout_budget` (`:5888`)
  is the same failure class with no test-only fix — raising its budget breaks
  its `elapsed < 1.5 * budget` oracle, and its own docstring concedes the
  slow-runner flake; (ii) other files carrying `job_timeout_seconds=5`
  (`test_analysis_pipeline.py:505`, `test_e2e_ifs.py:111`, `test_e2e_m3.py:173`,
  `test_ifs_forecast_integration.py:177/552/573`, `test_orchestrator.py:324`,
  `test_pipeline_logs_artifacts.py:448/493`, `test_warm_start.py:901`; all at
  `9785e52d`) are the same latent class but not observed victims; left as-is
  so this PR's diff and CI selection stay on the six issues' files (the two
  sites in `test_production_scheduler.py:46469/48432`, a file already in the
  change surface, are raised to 120 instead — round-2 review; two of the
  listed files, `test_pipeline_logs_artifacts.py` and `test_e2e_m3.py`, already
  sit in CI's importer closure of `test_orchestration_chain.py`, so for them
  the reason is diff scope alone); (iii) the ten remaining
  `job_timeout_seconds=120` literals in `tests/test_warm_start_chaining.py`
  carry no `>= 60` pin — nine are inline in test bodies with no helper seam,
  and the tenth sits in `_quarantine_state_index_orchestrator`, whose four
  callers never reach `orchestrate_cycle`'s poll deadline, so a pin there would
  guard an unreachable budget. The durable answer for (i) and (ii) is an
  injectable monotonic clock in `StageExecutionDependencies`
  (`chain_stage_execution.py:149` already injects `utcnow`) and a clock seam in
  `workers/shud_runtime` — a production change outside a test-only PR.
- Recorded deviation: the issue body's acceptance ("name the shared state,
  isolate the leak") is superseded by the issue author's second comment and by
  the diagnosis; what this PR removes is the wall-clock dependence. The
  shared-state reading is recorded as superseded and not reproduced under the
  diagnosis's conditions (not as refuted), and criterion 2 is discharged by the
  deterministic mechanism harnesses plus one full-suite node-27 run at the
  final HEAD (reason above).

## Risks / Trade-offs

- [D3 allowlist too narrow → future false red on a legitimate spelling] →
  the failure message names the allowlist and how to widen it; the three
  current sites all use the admitted form.
- [D4 property false-red on a `count`-only lane] → criterion mirrors the
  compactor's own `outcomes`-sequence test.
- [D5 identity pins forgeable] → declared bound; the pin still closes the
  plain same-kind rebind the issue measured.
- [D6 larger default budget hides a real hang] → every timeout-path test
  keeps its explicit 1 s / 10 s budget and faked clock; a hang in a
  non-timeout test surfaces as the pytest session's own duration (bounded by
  the stub's 20 s self-cap for A), not as a silent pass.
- [Node-27 mutation run touches the live tree] → all node-27 mutation and
  load runs execute in a detached worktree at the frozen SHA; `/home/nwm/NWM`
  is only `git pull --ff-only`ed.
