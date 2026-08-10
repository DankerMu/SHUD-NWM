# Tasks: state-save-source-freshness-gate

Issue: #1325 (priority:high) — `save_state_for_run` publishes
successor state without verifying the source run produced the
output; KILLED/cleaned runs publish "clean" states that poison
warm-start lineage (#1164 field incident's other half).

Fixture level: expanded. Origin: issue-scribe from #1202's
out-of-scope routing (PR #1321 tasks 5.0(c)); no `Suggested fixture
level` field; orchestrator triage: P1 silent-data-correctness blast
radius, cross-file contract (writer + gate), spec-delta obligation ⇒
expanded.

Evidence floor:
- `uv run pytest -q tests/test_warm_start_chaining.py
  tests/test_state_manager.py tests/test_slurm_array_contract.py`
- `uv run pytest -q tests/test_shud_runtime.py` (writer anchors)
- `uv run pytest -q tests/test_production_scheduler.py` (round-1
  P2-1: its state-save fixtures write provenance-less manifests —
  regression surface, not optional)
- `uv run ruff check .`
- `openspec validate state-save-source-freshness-gate --strict
  --no-interactive`

Deviations (recorded):
1. Issue AC-5's literal "pipeline event" on rejection is satisfied
   through the existing exit-code → orchestrator generic
   stage-classification propagation, NOT a new event write from the
   DB-free compute-node CLI (proposal Ruling 6 / design D4). The
   typed token lands in Slurm stderr/logs; candidate records see a
   generic stage failure.
2. Issue AC-3's freshness anchor: the issue's recommended wall-clock
   comparison ("产物必须新于本次 run 执行的起点") is NOT
   implementable soundly — `hydro_run.start_time` is simulation time
   (≡ `cycle_time`, written once, never updated per attempt:
   `chain_forecast_state.py:86`, `chain_repository.py:363-394`, no
   `UPDATE ... SET start_time` repo-wide; the base spec's three-way
   consistency scenario itself equates `start_time` with `T_{N+1}`),
   and on the `durable_shud_output_reused` retry lane any
   "newer-than-this-submission" predicate permanently rejects the
   designed recovery path (fixture round-1 P1-1/P1-2). Replaced by
   the solver-success-witness contract (proposal Ruling; design D1):
   AC-3's harmful population (killed/partial-attempt residue) is
   rejected via witness absence; a previous SUCCESSFUL solve's tree
   of the same run_id is deliberately admissible.
3. Issue recommendation "清单不存在时默认拒绝" is implemented via
   the writer widening (design D2): manifest-missing IS a hard
   reject (G2) post-upgrade, while zero-checkpoint-hour configs stay
   alive through the empty-manifest + gated-fallback path (A5). The
   issue's alternative (existence-only gate) was NOT taken.
4. Issue's suggested provenance field set (`generated_at` /
   `run_attempt` / `slurm_job_id`): `run_attempt` has no fact source
   in the runtime (round-1 P2-3) — replaced by `slurm_job_id` +
   `array_task_id` from `_task_outcome_attempt_identity` facts;
   `generated_at` is recorded as evidence, not used as a gate clock.
5. Issue's "受影响面" listed `chain_manifests.py` and
   `StateRunContext` threading — dropped entirely with the
   wall-clock anchor (proposal What-Changes 4); the change surface
   shrinks to `state_cli.py` + `runtime.py` + tests + spec.

## 0. Run ledger (upstream-contract escalation and re-entry)

- Run 1 (issue #1325 as originally written): fixture reviews
  r1/r2/r3 each NOT CLEAN (r1: 4 P1 + 3 P2; r2: 1 P1 + 4 P2; r3:
  1 P1 + 1 P2) → two-iteration repair bound tripped → issue
  reclassified upstream-contract-defective. Root contract defects:
  (a) recommended wall-clock freshness anchor built on
  `hydro_run.start_time`, which is simulation time ≡ `cycle_time`
  (vacuously-true predicate on the DB path); (b) AC-3's literal
  clock comparison is either vacuous or permanently rejects the
  `durable_shud_output_reused` recovery lane; (c) "清单缺失默认拒绝"
  kills the analysis / short-horizon lanes whose ONLY publish path
  is the manifest-less fallback (population the issue missed);
  (d) `run_attempt` provenance field has no fact source; (e) AC-5
  "pipeline event" not emittable from the DB-free CLI; (f) the
  assumed downstream backstop (three-way time consistency) is
  defeated by the publish-side header rewrite. Gap report +
  corrected implementation-ready contract posted on the source
  issue
  (https://github.com/DankerMu/SHUD-NWM/issues/1325#issuecomment-5231753795
  — the authoritative run-2 contract). Run 1 TERMINAL; review
  history preserved in 1.2.
- Run 2 (this fixture, current): re-entry against the REPAIRED
  contract — solver-success witness + run identity + named-artifact
  integrity (no clocks); writer records
  `provenance`+`requested_checkpoint_hours`+`final_ic` with the
  two-exact-paths discovery rule; 8 typed reasons; fallback
  manifest-driven, rglob deleted. Fixture review restarts at
  round 1 (fresh contract, fresh fixture-review ledger; run-1
  history retained for audit).

## 1. Fixture

- [x] 1.1 Author proposal/design/tasks + spec delta
  (cross-cycle-warm-start-chaining: 1 MODIFIED writer requirement
  carrying all six base scenarios, 1 ADDED publish-side admission
  requirement); run 2: re-authored to the repaired contract (run-1
  r3 P1/P2/P3 findings folded: final_ic discovery rule pinned in
  D2.3+spec, `FINAL_IC_MISSING` token + D1 branch + A5(e)/A7(a2)
  anchors, A7(a) restated to the mock harness's real `demo.cfg.ic`
  output, `.cfg.ic`-only flip disclosed as behavior delta 6, A10
  sites `:1700-1774`/`:432-520` added, A8 bounded-read oracle
  `:2100-2124` + G5 read-bound ruling, G3 requires
  `requested_checkpoint_hours`, tolerance scenario lists
  `final_ic`, `array_task_id` int|null, cite drifts fixed)
- [x] 1.2 Reviewer fixture review (read-only) until clean
  (two-iteration repair bound per workflow contract)
  - Run 2 Round 3 CLEAN (0 P1, 0 P2, 3 P3 cosmetics — closed in the
    approval commit): reviewer empirically drove
    `_load_state_checkpoint_manifest` on master for all five A2c
    shapes (each RED confirmed; case iii publishes the checksum-less
    entry unchecked rather than dropping it — A2c summary
    parenthetical corrected), confirmed raw-array wording consistent
    across proposal/tasks/design/spec, all round-2 P3 closures
    landed, all cites resolve, A10 site enumeration complete
    (grep-verified no manifest fixture outside enumerated sites),
    `_find_ic_file`/`_find_state_checkpoints` deletion safe (zero
    external references). P3 closures: A2c mis-narration (iii);
    A7(a3) `:4090` attribution (assertion, not solver write);
    A10 cite `:1700`→`:1701`. Fixture APPROVED for Phase 1.
  - Run 2 Round 2 NOT CLEAN (1 P2, 5 P3) — repaired (iteration
    2/2, the bound): P2-1 G5's five malformed shapes had anchors
    for only two and proposal/tasks implementation wording still
    said "`:652-655` continue" → A2c parameterized over all five
    shapes (each RED on master), proposal What-Changes 2 + tasks
    2.2 rewritten to the raw-array judgment; P3-1 checksum
    verification pinned to the GATE (loader parse semantics
    unchanged — the loader-direct tolerance test with fake
    checksums stays green); P3-2 `:631-633` cite; P3-3 `:227`
    cite; P3-4 post-gate branch wording pinned to the VERIFIED
    root (no `_find_state_checkpoints` re-probe); P3-5 design
    header run/round labels disambiguated. Reviewer verified both
    round-1 repairs sound (A7(a3) matches the `_FAST_SOLVER_STUB`
    harness product; consumer sweep re-confirmed; G5 raw-array
    consistent with loader mechanics/A8 ordering), token list and
    anchor cross-references fully consistent, A10/A8 enumeration
    complete vs all five suites' call sites, Phase-1 startable
    without contract invention.
  - Run 2 fixture review: Round 1 NOT CLEAN (1 P1, 1 P2, 4 P3) —
    repaired (iteration 1/2): P1 writer widening inverts the #1315
    zero-hour guard `tests/test_shud_runtime.py:4067-4091` which no
    anchor enumerated and A10 forbade touching → guard REWRITTEN
    inverted as A7(a3), supersession rationale recorded in D2.1
    (sole reader is the gate; `requested_checkpoint_hours`
    discriminates the lanes), A10 oracle-integrity rule narrowed
    with this one enumerated exception; P2 G5's declared set pinned
    to the RAW `checkpoints` array — all four silent loader drops
    (`:643`, `:646-647`, `:654-655`, `:658-659`) and non-sequence
    `checkpoints` become `MANIFEST_INCOMPLETE`, A2c companion
    anchor added, spec scenario WHEN extended; P3s: mock cite
    `:65`; A5(b) same-candidate-class pin (master prefers any
    `.update` over any `.cfg.ic`); G5 unsafe-path checks precede
    checksum-presence (A8 symlink pin survives); file-shaped
    `output_uri` lane retirement disclosed (proposal delta 6, G1
    row).
  - RUN-1 HISTORY (terminal): Round 3 NOT CLEAN (1 P1, 1 P2 + P3) —
    P1 `final_ic` discovery rule unspecified (natural
    `source_path` reading falsifies A7(a) in the mock harness which
    writes `demo.cfg.ic` never `.update`; glob reading relocates
    the #1164 hole to the writer); P2 A10/A8 enumeration missed
    `:1700-1774`, `:2100-2124` (bounded-read oracle displaced),
    `:432-520`. **Third revise-class verdict → bound tripped: issue
    #1325 reclassified upstream-contract-defective; gaps reported
    on the source issue; run 1 TERMINAL.**
  - Round 2 NOT CLEAN (1 P1, 4 P2) — repaired (iteration 2/2, the
    bound): P1 the empty-`checkpoints` fallback was the analysis
    lane's NORMAL publish path (not a #1317 corner: analysis never
    sets `state_checkpoint_hours`, `chain_analysis.py:66` /
    `chain_manifests.py:483-486`), reachable through routine retry
    (deterministic run_id + never-wiped output root + in-place
    `*.cfg.ic.update` rewrites), and D6's claimed backstop could
    not fire (`_normalized_checkpoint_ic_file` force-rewrites the
    header, `state_cli.py:240-248`, so three-way consistency
    compares forged labels) → fallback made manifest-driven: writer
    records `requested_checkpoint_hours` + `final_ic`
    (path+checksum), rglob retired, total-miss trees reject
    (`CHECKPOINTS_UNCAPTURED`), witness statement re-stated
    precisely ("some attempt", named-artifact integrity); P2-1
    checksum-absent entries ruled a G5 violation; P2-2 A10 restated
    with manifest-less happy-path sites enumerated; P2-3 A6(a)
    relabeled GREEN-both-sides + genuinely-red foreign-provenance
    fall-through variant added; P2-4 symlink/escape oracle pins
    added to A8 (G5 re-raises unchanged). Notes folded: D3 citation
    fixed to `upload_results:408`; env lane single-root sentence;
    manual-repair-lane witness note in D6.
  - Round 1 NOT CLEAN (4 P1, 3 P2) — repaired (iteration 1/2):
    P1-1 `h.start_time` is simulation time, wall-clock G6 vacuously
    true on the DB path → clock anchor abandoned, witness contract
    adopted (design D1); P1-2 `run_started_at` semantics
    fork (vacuous vs killing durable-reuse retries) → field dropped
    entirely, liveness pin A6b added; P1-3 spec MODIFIED block
    dropped 3 base scenarios and spliced one → rewritten carrying
    all six verbatim with targeted edits only; P1-4 failure-lane
    manifest write would hand KILLED trees a provenance pass →
    failure lanes stay manifest-less, witness invariant made
    explicit (D2, new spec scenario), D2's wrong caller-gating claim
    fixed (`:3090-3091` internal early return, caller `:598`
    unconditional); P2-1 `tests/test_production_scheduler.py`
    regression surface added to A10 + evidence floor; P2-2
    multi-root ruling pinned (D3: enumerate → first fully-verified
    root wins; unreadable manifest keeps `Invalid state checkpoint
    manifest` hard error, A8 pin); P2-3 `run_attempt` dropped
    (no fact source) for `slurm_job_id`+`array_task_id`. Notes
    folded: G2 path level spelled out; env-var reservation moot
    (env threading dropped); state-manager spec needs no delta
    (WHEN-precondition reading confirmed); evidence-strength wording
    aligned to stderr/exit-code reality.
- [x] 1.3 `openspec validate state-save-source-freshness-gate
  --strict --no-interactive` green (re-run after every repair round)

## 2. Implementation (implementer subagent)

- [x] 2.1 Writer: remove `write_manifest` no-targets early return
  (`runtime.py:3090-3091`) + `provenance` block (incl.
  `requested_checkpoint_hours`) + `final_ic` entry (design D2);
  anchors A7(a-d) red-proofed / pinned
- [x] 2.2 Gate G1-G5 in `save_state_for_run` + typed reasons; G5
  integrity judged by the gate over the RAW `checkpoints` array —
  all four loader silent drops (`state_cli.py:642-643`,
  `:646-647`, `:654-655`, `:658-659`), non-sequence `checkpoints`
  (`:631-633`), and checksum-absent entries →
  `MANIFEST_INCOMPLETE`; hash mismatch → `CHECKSUM_MISMATCH`;
  unsafe paths re-raised unchanged; loader parse semantics
  unchanged (D1); post-gate branch per D1 (verified root's declared
  checkpoints / manifest-named `final_ic` /
  `CHECKPOINTS_UNCAPTURED` / `FINAL_IC_MISSING`); `_find_ic_file`
  deleted; multi-root ruling D3; anchors A1-A6, A8, A9 (A2c
  parameterized over the five malformed shapes) red-proofed on
  pre-change tree (`git archive` extraction protocol) / pinned per
  design D7
- [x] 2.3 Regressions A10: five evidence-floor suites green with
  witness-bearing fixtures (enumerated manifest-less and
  provenance-less sites per design D7 A10); no existing assertion
  weakened
- [x] 2.4 Evidence floor suites + ruff green; implementer reports
  deviations explicitly ("no deviations" stated if none)

## 3. PR

- [x] 3.1 Commit + push branch
  `feat/issue-1325-state-save-freshness-gate`; PR with 变更摘要 /
  偏离记录 / 测试证据 / Evidence-Floor 声明
- [x] 3.2 CI green (targeted Unit Tests)

## 4. Review loop

- [x] 4.1 Cross-review rounds per gate ledger; candidates → dedup →
  per-class verifier batches; findings verified before fix
  - Round 1 (head 164e7b3f) NOT CLEAN: 3 lenses → 13 deduped
    candidates → 3 verifier batches → 6 into the fix pass (P2
    writer→gate round-trip coverage gap A11; P3 strip
    verify/publish divergence A5(f); P3 G5 valid_time parseability
    A2c(vi); P3 post-gate empty-list guard A2d; P3 partial
    provenance anchors A4c; P3 D3.5 first-reason anchor A6(d)),
    5 REFUTED/DISCARD, 2 PLAUSIBLE-DEFER routed as #1329
    (multi-root shadow liveness) + #1330 (cross-attempt final_ic
    residue) with D6 known-limit records + D3 rule 6 (R1) ruling.
    Fix pass: all 6 landed with RED evidence for the three code
    fixes; 15 new/extended tests green; no deviations.
  - Round 2 (head e4f4806e) CLEAN focused re-review: all six
    round-1 fixes verified landed, no new defect; 4 P3 cosmetics
    closed in d8e90de8 (A5(f) unconditional success pin, docstring
    precision, design cites, D6 verify-then-publish residual with
    not-routed reason).
  - Round 3 / Phase 7 (final head d8e90de8) CLEAN: A5(f)
    mutation-tested (reverting the strip fix turns the anchor RED),
    8 replacement ACs anchored, token consistency grep across
    code/spec/design/proposal/tests, oracle integrity confirmed
    (single enumerated A7(a3) supersession).
- [x] 4.2 Phase 7 final review clean on final head (d8e90de8)

## 5. Merge (pre-authorized) and closeout

- [x] 5.0 Follow-ups routed with numbers: #1329, #1330 (round-1
  PLAUSIBLE-DEFER findings)
- [x] 5.1 Chinese work summary + evidence posted
  (PR #1328 comment 5232532257); CI green on both heads
- [x] 5.2 Merged f9f41da9 (2026-08-09T16:25:37Z); issue #1325
  auto-closed; archive + loop-log line + audit in the chores
  commit
