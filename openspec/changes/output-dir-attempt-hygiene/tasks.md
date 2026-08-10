# Tasks: output-dir-attempt-hygiene (#1330)

Issue: #1330 (S). Branch:
`feat/issue-1330-output-dir-attempt-hygiene`. Depends on merged
#1325 (PR #1328, witness/two-exact-paths writer).

## 0. Run ledger

- RUN 1 (issue-text contract): TERMINAL. Three fixture-review
  rounds (r1: 2 P1+3 P2+6 P3; r2: 2 P2+6 P3; r3: 2 P2+2 P3)
  tripped the two-iteration repair bound — issue reclassified
  upstream-contract-defective. Six contract defects (unimplementable
  per-entry clearing vs symlink residue; unspecified try placement
  losing accounting; unconditional-quarantine empty-eviction
  regression; UNANALYZED retry-classification flip — receipt-borne
  `WORKSPACE_PATH_UNSAFE` is permanent while master's no-receipt
  lane fell back to transient `NODE_FAILURE`; AC-3 per-entry
  wording mismatch; node-22 locatability AC without a local
  oracle). Gap report + corrected contract posted:
  https://github.com/DankerMu/SHUD-NWM/issues/1330#issuecomment-5234513389
- RUN 2 (corrected contract, authoritative = that comment): fresh
  fixture-review ledger, rounds recount from 1. Key additions over
  run 1: error-code split ruling (tamper-shaped guard →
  `WORKSPACE_PATH_UNSAFE` permanent; hygiene I/O failures →
  `STORAGE_WRITE_FAILED`, already in `TRANSIENT_ERROR_CODES`,
  `retry.py:23-35` — zero orchestrator change), spec delta carries
  the non-empty qualification, retry-classification anchor added
  to A7 and the evidence floor. Run-2 r1 refined the contract's
  step-index split wording to shape-based (the
  `SafeFilesystemError.kind` discriminator) per the contract's own
  篡改形/I/O 形 principle; corrigendum posted on #1330.

## Evidence Floor

- `uv run pytest -q tests/test_shud_runtime.py
  tests/test_warm_start_chaining.py tests/test_state_manager.py`
  (writer + gate + round-trip regression surface; the known
  pre-existing macOS symlink-errno failure in
  `test_production_scheduler.py` is outside this floor)
- Retry-classification anchors (A7's `is_retryable_failure`
  assertions — run inside the floor suites, importing
  `services.orchestrator.retry`)
- `uv run ruff check .`
- `openspec validate output-dir-attempt-hygiene --strict
  --no-interactive`

## Deviations (recorded up front)

1. Issue AC-3's per-entry rejection wording is adapted: rename-aside
   moves inside-tree entries wholesale without following; refusal
   narrows to `output` itself non-directory (A4) or hygiene failure
   (A7). Intent (no-follow, no half-clean, explicit termination)
   fully covered — design D4 deviation note; goes in PR 偏离记录.
   (Superseded in run 2 by the corrected contract's replacement AC,
   which writes the split explicitly.)
2. Archived `state-save-source-freshness-gate` design D6 is not
   edited (archives immutable); closure recorded on issue #1330 and
   in this change's proposal instead of the D6 entry the issue AC
   names.
3. Node-22 实机 locatability (issue AC): discharged locally by the
   tmp_path A2 anchor only; the on-host receipt arrives with the
   first natural production retry after merge, not as a merge
   blocker (run-1 r3 P3-2; corrected contract records this as a
   deviation, syscall-divergence risk noted — macOS oracle for
   `renameat`/dir-fd semantics, Linux/NFS unverified until then).

## 1. Fixture

- [x] 1.1 proposal/design/tasks + spec delta authored (this commit)
- [x] 1.2 Reviewer fixture review (read-only) until clean
  (two-iteration repair bound per workflow contract; RUN-2 ledger —
  rounds recount from 1)
  - RUN-2 Round 3 EFFECTIVELY CLEAN (0 P1, 0 P2, 1 P3) — reviewer
    verdict: all five round-2 repairs sound, all cited master
    anchors accurate, "not revise-class; the fixture is
    implementable as written". Cosmetic closure applied same-round:
    A7(a)(ii)'s `os.mkdir` injection discriminated to the `output`
    target (undiscriminated patch would fire at step 2's
    `output_residue` mkdir and leave the recreate lane silently
    untested) + assert quarantine already at `previous` when the
    failure fires; totality bullet gains "bare `OSError` (no kind)
    rides the io lane" (race via safe_fs.py:497-498/:367-368
    post-probe). Fixture APPROVED for implementation.
  - RUN-2 Round 2 NOT CLEAN (0 P1, 2 P2, 3 P3) — repaired
    (iteration 2/2, the bound; all round-1 repairs verified sound,
    all load-bearing cites re-verified accurate): P2-1 A7(a)'s
    second forced-failure point was a pick-one disjunction, so the
    just-repaired recreate lane could ship unanchored → A7(a) now
    THREE os-level forced-failure points, ALL required (rename/
    clear, recreate, probe/listing), tasks 2.3 propagated; P2-2 the
    two NEW primitives never specified their `kind` while `kind` is
    the sole permanent/transient discriminator (class default
    "unsafe", safe_fs.py:13 — house-style wrapping would make
    quarantine ENOSPC/ESTALE permanent), and A7(a)(i)
    monkeypatched the primitive so a wrong real kind was
    unobservable → D2.1/D2.2 gain explicit ERROR CONTRACTS
    (walk refusals keep "unsafe"; os-call OSErrors wrap "io"
    explicitly; EXDEV recorded), A7(a) injection moved to os level
    beneath the primitives, tasks 2.1 gains kind-contract unit
    coverage, and D1 dispatch gains TOTALITY (binary on
    kind=="io"; "unsafe"/"indeterminate" :115/:122/:139/any future
    kind → WORKSPACE_PATH_UNSAFE — fail-closed, no generic-error
    escape). P3s: spec delta tamper arm generalized from geometry
    enumeration to any filesystem-safety refusal (+ no-untyped-
    escape clause) matching D1's shape rule; absent-branch
    precedence stated (FileNotFoundError ⊂ OSError, re-raised BARE
    by safe_fs :261-262/:367-368/:497-498 — inner catch on the
    probe, resolved before the step 1-5 wrapper); stale :451-452
    cite consolidated to :424-425 root/:450-451 entry.
    (iteration 1/2): P1 D1 step 5 prescribed `_ensure_directory`,
    which pre-types kind="io" mkdir failures as
    `WORKSPACE_PATH_UNSAFE` (permanent) and — being a sibling
    `RuntimeError` subclass — cannot be re-caught as
    `SafeFilesystemError`, silently defeating the split on the
    recreate lane → step 5 now calls raw
    `ensure_directory_no_follow`, A7(a) extended to force-fail the
    recreate/probe lanes; P2-1 the split was specified by STEP
    INDEX not failure SHAPE (step-1 probe/listing kind="io" NFS
    failures would escape to permanent RUNTIME_ERROR; steps-2-5
    kind="unsafe" tamper shapes — e.g. symlinked `output_residue`
    sibling — would be blanket-typed transient) → split rewritten
    shape-based on `SafeFilesystemError.kind` (safe_fs.py:13)
    across all steps, propagated to proposal ruling 5/delta 3b,
    spec delta, tasks 2.2, A7(b) sibling tooth; the step-index
    wording was INHERITED from the gap-report contract — refined
    per the contract's own 篡改形 vs I/O 形 principle, corrigendum
    posted on #1330; P2-2 proposal ruling 1 still mandated
    unconditional quarantine → non-empty/empty/absent trichotomy
    made normative in the ruling itself. P3s: absent branch now
    explicitly falls through to the recreate step (D1 step 1,
    tasks 2.2); run-1 history wrap-requirement line marked
    superseded; cite fixes (safe_fs entry-refusal :450-451;
    membership decided in `is_transient_error` :123-124 via
    `is_retryable_failure` :187-188).
  - RUN-1 HISTORY (terminal): Round 3 NOT CLEAN (2 P2, 2 P3) —
    P2-1 delta 3b's "benign-to-positive" inverted (membership-based
    classifier: `WORKSPACE_PATH_UNSAFE` ∉ `TRANSIENT_ERROR_CODES`
    ⇒ attempt-start NFS glitches flip from auto-retry to
    permanent); P2-2 spec delta still mandated unconditional
    quarantine, contradicting the round-2 empty-reuse exemption.
    **Third revise-class verdict → bound tripped; gaps reported on
    the source issue; run 1 TERMINAL.**
  - Round 2 NOT CLEAN (0 P1, 2 P2, 6 P3) — repaired (iteration 2/2,
    the bound; both round-1 P1 repairs verified sound, A4 teeth
    confirmed RED-capable): P2-1 empty-`output` eviction regression
    (early-failing retry leaves an empty dir; unconditional
    quarantine would evict the only real residue) → D1 step 1 gains
    the EMPTY-reuse exemption (dir-fd listing, no quarantine, no
    eviction), A3(c) pin added, proposal delta 1 discloses; P2-2
    A4's accounting half was mislabeled GREEN (master dies OUTSIDE
    the try at :392-393 — no receipt/mark_failed) → A4 relabeled
    SPLIT color (typed-error half GREEN pin, accounting half RED on
    master), proposal delta 3b discloses the new receipt-emitting
    failure class (incl. ENOSPC/EACCES). P3s: `<attempt-label>`/
    `<label>` leftovers → `previous`; cites corrected (except block
    :416-433, receipt call :421/def :1958, _final_ic_entry
    :3122-3144, verify_output :1011-1047, _manifest_provenance
    :3112-3120); "17 sites" → 18 + 2 e2e in A3/risk-triage; D1
    failure semantics required the hook to wrap raw safe_fs
    errors into WORKSPACE_PATH_UNSAFE (bare SafeFilesystemError →
    generic RUNTIME_ERROR via _as_runtime_error :3801-3804 — the
    wrap-target code is SUPERSEDED in run 2 by the shape-based
    split, design D1); D2.2
    signature gains missing_ok + single-named-entry semantics +
    root-symlink clause, D1 step 3 reworded to one named call;
    step-1 FileNotFoundError justification reworded (defensive —
    runs/<run_id> already materialized by the split loop).
  - Round 1 NOT CLEAN (2 P1, 3 P2, 6 P3) — repaired (iteration
    1/2): P1-1 `rmtree_no_follow` refuses symlink entries → residue
    with a symlink would permanently lock the run_id at the hygiene
    hook → new `remove_tree_allow_symlinks` primitive (D2.2), A5
    extended to pin it; P1-2 hook placement outside `execute()`'s
    `try` would lose failure log / task-outcome receipt /
    `mark_failed` (and `log_dir` wouldn't exist yet) → hook moved
    INSIDE the try as first step, dir loop split, A7 extended with
    accounting assertions; P2-1 identity-derived quarantine label
    mis-attributes the previous attempt's tree → literal `previous`,
    attribution via the residue's own manifest provenance; P2-2
    "gate probes only the workspace root" was false (gate
    enumerates workspace + output_uri roots) → D3 pin and spec
    scenario corrected to "residue is never a candidate root";
    P2-3 A4 was GREEN on master (ensure_directory already types
    symlink output) → relabeled GREEN-both-sides pin with
    ordering/accounting teeth, proposal delta 3 corrected. P3s:
    dir-loop fact, FileNotFoundError=absent exemption spelled out,
    site counts corrected (18 execute sites + 2 e2e; ONE
    residue-seeding run_shud test), log-truncation caveat (O_TRUNC)
    recorded in D5, residue-never-GC'd made an explicit accepted
    cost, A1 stub must write `demo.rivqdown` so verify_output
    passes. Residual-risk note folded: root-collision precondition
    (OBJECT_STORE_ROOT default = workspace root) recorded in D5.
- [x] 1.3 `openspec validate output-dir-attempt-hygiene --strict
  --no-interactive` green (re-run after every repair round)

## 2. Implementation (implementer subagent)

- [ ] 2.1 `packages/common/safe_fs.py`: TWO primitives per design
  D2 — `rename_entry_no_follow` (dir-fd `renameat`, both parents
  O_DIRECTORY|O_NOFOLLOW walked from containment root) and
  `remove_tree_allow_symlinks` (quarantine removal; unlinks symlink
  entries via dir-fd instead of refusing; `rmtree_no_follow`
  untouched) + focused unit coverage (rename moves symlink
  un-followed; missing source → error; removal deletes a tree
  containing symlink/subdir entries without following; AND — kind
  contract, run-2 r2 — a plain `OSError` forced beneath each new
  primitive at the os level surfaces as `SafeFilesystemError`
  `kind="io"`, per design D2 error contracts)
- [ ] 2.2 `workers/shud_runtime/runtime.py` `execute()`:
  attempt-start hygiene hook per design D1, FIRST step inside the
  existing `try` (dir loop split: input/log before, output owned by
  the hook) — probe with FileNotFoundError=absent → skip straight
  to the recreate step (output must still be created) → non-dir
  hard error (`WORKSPACE_PATH_UNSAFE`, permanent) → EMPTY dir
  reused as-is, hook done (no quarantine, no residue eviction) →
  NON-EMPTY: clear `output_residue/previous` via
  `remove_tree_allow_symlinks` → rename to literal `previous` →
  recreate fresh via raw `ensure_directory_no_follow` (NOT
  `_ensure_directory` — design D1 step 5). Error-code split BY
  SHAPE across all steps: `kind="unsafe"`/non-dir guard →
  `WORKSPACE_PATH_UNSAFE` (permanent); `OSError`/`kind="io"`
  anywhere incl. the probe/listing → `STORAGE_WRITE_FAILED`
  (transient, design D1)
- [ ] 2.3 Anchors A1-A7 per design D4 (A1/A2/A5/A6 RED-proofed on
  master 55c020de; A3(a-c) pins green incl. the empty-reuse
  no-eviction pin; A4 SPLIT color — typed-error half green pin,
  accounting half RED on master; A7 both lanes with
  `is_retryable_failure` classification assertions — the I/O lane
  with THREE os-level forced-failure points, all required, per
  design D4 A7(a)); no existing assertion weakened
- [ ] 2.4 Evidence floor + ruff green; deviations reported
  explicitly ("no deviations" stated if none)

## 3. PR

- [ ] 3.1 Commit + push; PR with 变更摘要 / 偏离记录 / 测试证据 /
  Evidence-Floor 声明
- [ ] 3.2 CI green (targeted Unit Tests)

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; candidates → dedup →
  per-class verifier batches; findings verified before fix
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [ ] 5.0 Follow-ups routed with numbers (if any arise in review)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final
  head
- [ ] 5.2 Merge; archive change; loop-log line + audit; close issue
  #1330
