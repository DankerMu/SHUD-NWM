# Design: output-dir-attempt-hygiene (#1330)

Contract source: issue #1330 (implementation-ready; the single open
call — 清空 vs quarantine — is ruled in proposal.md: quarantine via
rename-aside, retention exactly one). Explorer facts (2026-08-09
sweep) cited inline.

## D1. Hygiene hook (execute() attempt start)

Placement (round-1 P1-2 repair): the hook runs INSIDE `execute()`'s
existing `try` (`runtime.py:400`), as its first step — BEFORE
`generate_cfg_para` and the solve — so a hygiene failure flows
through the existing `except` collateral (`:416-433`):
`_write_failure_log`, `_write_task_outcome_receipt` (load-bearing
for array accounting; call `:421`, def `:1958`), `repository.mark_failed`,
`upload_logs`. The three-directory `_ensure_directory` loop at
`:392-393` is split: `input_dir` and `log_dir` stay created BEFORE
the `try` (the receipt/log writers need `log_dir`); `output`
creation moves into the hook (step 5). Scope: `execute()` ONLY —
`run_shud` is untouched, so the single residue-seeding
direct-`run_shud` test keeps its geometry (round-1 P3-3 count
correction: 18 `.execute(` sites in `tests/test_shud_runtime.py`
plus `tests/test_e2e.py:944`, `tests/test_direct_grid_e2e.py:250`;
exactly ONE direct-`run_shud` test pre-seeds `output`,
`tests/test_shud_runtime.py:4191-4199`).

Algorithm (all steps under `workspace_root` containment):

1. Probe `runs/<run_id>/output`. `FileNotFoundError` → absent →
   SKIP DIRECTLY TO STEP 5 (recreate) — nothing to quarantine, but
   `output` must still be created since step 5 is its only creator
   after the loop split (run-2 r1 P3-1; the absence itself is
   defensive: with the split loop, `input_dir`/`log_dir` creation
   has already materialized `runs/<run_id>`, so only `output`
   itself can be missing — round-2 P3-6 rewording). Present but NOT
   a plain directory (symlink, file, device) → typed hard error
   (`WORKSPACE_PATH_UNSAFE` family) — never rename-follow, never
   proceed. Present, a plain directory, and EMPTY (zero entries via
   a dir-fd no-follow listing) → reuse it as-is and the hook is
   DONE (steps 2-5 skipped): no quarantine, no `output_residue`
   creation, and — critically — the existing `previous` residue is
   NOT evicted (round-2 P2-1: an early-failing retry, e.g. a
   `prepare_workspace` staging failure at `runtime.py:401`, leaves
   an empty `output`; unconditional quarantine would evict the only
   real residue and retain an empty husk — a retention regression
   vs master). Present and NON-EMPTY → continue with steps 2-5.
2. Ensure `runs/<run_id>/output_residue` exists
   (`ensure_directory_no_follow`).
3. Remove the single `output_residue/previous` entry if present,
   with the NEW `remove_tree_allow_symlinks` primitive
   (`missing_ok` — first retry has no residue; D2.2; round-1 P1-1
   repair — the existing `rmtree_no_follow` refuses symlink
   entries: root-entry refusal `safe_fs.py:424-425`, entry-refusal
   raise `:450-451` (run-2 r2 cite consolidation), which would
   permanently lock a run_id whose quarantined residue contains a
   symlink). Retention = exactly one tree, the one about to be
   created (round-2 P3-5: one named-entry call, not a directory
   sweep — nothing else ever writes into `output_residue`).
4. `rename_entry_no_follow(runs_dir, "output", residue_dir,
   "previous")` — new safe_fs primitive (D2). Label is the LITERAL
   `previous` (round-1 P2-1 repair: identity-derived names would
   stamp the CURRENT attempt's job id onto the PREVIOUS attempt's
   tree — forensic mis-attribution; the residue's own witness
   manifest provenance, when present, already records the true
   producer's `slurm_job_id`/`array_task_id`, `runtime.py:3112-3120`,
   and is the authoritative attribution). The destination name is
   free by construction (step 3 removed any prior `previous`).
5. Create `output` fresh via `ensure_directory_no_follow` DIRECTLY
   (the safe_fs primitive), NOT via the runtime's `_ensure_directory`
   wrapper (run-2 r1 P1): `_ensure_directory`
   (`runtime.py:1999-2003`) pre-types EVERY `SafeFilesystemError` —
   including `kind="io"` mkdir failures (ENOSPC/EIO,
   `safe_fs.py:50-54`) — as `WORKSPACE_PATH_UNSAFE`, which would
   make the recreate lane permanent and defeat the split below;
   and because `SHUDRuntimeError` and `SafeFilesystemError` are
   SIBLING `RuntimeError` subclasses (`runtime.py:44`,
   `safe_fs.py:10`), a hook-level `except SafeFilesystemError`
   around a `_ensure_directory` call could never re-classify it.
   The hook calls the raw primitive and classifies per the shape
   rule below.

Failure semantics — ERROR-CODE SPLIT BY FAILURE SHAPE (run-2
contract ruling, closing run-1 defect 4; refined in run-2 r1 —
P2-1 — from the contract's step-index wording to the contract's own
stated principle, 篡改形 vs I/O 形, using the discriminator
`SafeFilesystemError.kind` that safe_fs already carries,
`safe_fs.py:13`): all hygiene failures raise FROM INSIDE the `try`
(full accounting: failure log, task-outcome receipt, `mark_failed`;
no dirty-tree solve, no partial state), and the receipt-borne error
code is decided by the failure's SHAPE, uniformly across ALL steps
1-5:

- TAMPER-SHAPED — step 1's non-directory guard (`output` is a
  symlink/file/device) AND any `SafeFilesystemError` with
  `kind="unsafe"` from ANY step (e.g. a pre-existing
  `output_residue` sibling that is itself a symlink: step 2's
  `ensure_directory_no_follow` rejects it via the `O_NOFOLLOW`
  ELOOP lane, `safe_fs.py:59-61` — a geometry this change newly
  creates and retry can never fix) → `WORKSPACE_PATH_UNSAFE`. Not
  in `TRANSIENT_ERROR_CODES` (`services/orchestrator/retry.py:23-35`)
  ⇒ permanent — CORRECT. (Master's behavior for the step-1 geometry
  was a no-receipt death outside the `try`, which the array reader
  back-filled as transient `NODE_FAILURE`,
  `chain_array_accounting.py:38`/`:880-890` — a pseudo-transient
  misclassification this change fixes; disclosed in proposal delta
  3b.)
- I/O-SHAPED — any `OSError` or `SafeFilesystemError` with
  `kind="io"` from ANY step, INCLUDING step 1's probe/emptiness
  listing (`stat_no_follow` raises `kind="io"` at
  `safe_fs.py:265-266`, `list_directory_no_follow` at `:369-370` —
  an ESTALE/EIO NFS glitch while probing is exactly as transient as
  one while renaming) as well as steps 2-5's residue clear, rename,
  and recreate (ENOSPC/EIO/ESTALE/EACCES on NFS) →
  `SHUDRuntimeError("STORAGE_WRITE_FAILED", ...)`.
  `STORAGE_WRITE_FAILED` is ALREADY in `TRANSIENT_ERROR_CODES`
  (`retry.py:28`) ⇒ automatic retry with backoff is PRESERVED for
  transient NFS glitches — zero orchestrator change.

The hook MUST wrap raw safe_fs errors itself — `except (OSError,
SafeFilesystemError)` around the WHOLE step 1-5 sequence,
dispatching on `isinstance`/`kind` — because a bare
`SafeFilesystemError` escaping to `_as_runtime_error`
(`runtime.py:3801-3804`) becomes generic `RUNTIME_ERROR`, which is
not in `TRANSIENT_ERROR_CODES` either (a silent permanent). Two
dispatch precisions (run-2 r2):

- ABSENT-BRANCH PRECEDENCE: `FileNotFoundError` is an `OSError`
  subclass, and safe_fs re-raises it BARE (`safe_fs.py:261-262`,
  `:367-368`, `:497-498` — never wrapped as `kind="io"`). The step-1
  absent branch is therefore an INNER `except FileNotFoundError` on
  the probe call itself, resolved BEFORE the step 1-5 wrapper can
  see it; a wrapper-level catch would turn every fresh first
  attempt into a spurious `STORAGE_WRITE_FAILED`.
- DISPATCH TOTALITY (fail-closed): the classification is binary on
  `kind == "io"` → `STORAGE_WRITE_FAILED` — and a BARE `OSError`
  (no `kind`; reachable as a race surfacing through
  `safe_fs.py:497-498`/`:367-368` after the probe) rides the SAME
  io lane, per the shape rule's "any `OSError`"; EVERYTHING ELSE —
  `kind="unsafe"`, `kind="indeterminate"` (`safe_fs.py:115`,
  `:122`, `:139`; unreachable from the hook's primitives today),
  and any future kind — → `WORKSPACE_PATH_UNSAFE`. Indeterminate
  safety is treated as unsafe, never as transient, and no kind can
  fall through to the generic-`RUNTIME_ERROR` silent permanent.

This is stronger than `_clear_recovery_scratch_root`'s in-band
refusal (`runtime.py:2199-2249`) and is anchored as A7 (both lanes,
with retry-classification assertions).

Why rename beats scan-and-unlink here: the residue tree may contain
subdirectories (`state_checkpoints/`), symlinks, or anything a killed
solver left; a rename moves the tree as an opaque unit without
following or enumerating it (no half-clean window), while the
flat-regular-files-only discipline of `_clear_recovery_scratch_root`
would have to reject legitimate residue shapes.

## D2. New safe_fs primitives (two)

`packages/common/safe_fs.py`:

1. `rename_entry_no_follow(parent: Path, name: str, dest_parent:
   Path, dest_name: str, *, containment_root: Path)`. Both parents
   opened `O_DIRECTORY|O_NOFOLLOW` component-walked from
   `containment_root` (same walk as `ensure_directory_no_follow`,
   `safe_fs.py:32-69`); then `os.rename(name, dest_name,
   src_dir_fd=src_fd, dst_dir_fd=dst_fd)`. `renameat` operates on
   the final-component link itself — a symlink at `name` is moved,
   not followed (and the D1 step-1 guard already hard-errors that
   case for `output` specifically; the primitive stays generic).
   Same-filesystem guarantee holds (`output` and `output_residue`
   share the `runs/<run_id>` parent). No fallback copy path:
   cross-device rename failure is a hard error (cannot occur in the
   shipped layout; anchored only as the generic failure lane).
   ERROR CONTRACT (run-2 r2 — `kind` is now the permanent/transient
   discriminator, so it is normative, not house style): walk-stage
   refusals keep the kinds the shared walk helpers assign
   (`kind="unsafe"` for symlink/non-dir/containment shapes); an
   `OSError` from the final `os.rename` call is wrapped as
   `SafeFilesystemError(..., kind="io")` EXPLICITLY — the class
   default is `kind="unsafe"` (`safe_fs.py:13`), so a default-kind
   wrap would silently make ENOSPC/ESTALE/EIO on the quarantine
   rename permanent under D1's dispatch. (`EXDEV` rides the io lane
   with the other `OSError`s; unreachable in the shipped
   same-parent layout, recorded, not pinned.)
2. `remove_tree_allow_symlinks(parent: Path, name: str, *,
   containment_root: Path, missing_ok: bool = True)` (round-1
   P1-1): like `rmtree_no_follow` but SYMLINK ENTRIES ARE UNLINKED
   (the link itself, via dir-fd `os.unlink` — inherently no-follow)
   instead of refused. Directories are entered only through
   `O_DIRECTORY|O_NOFOLLOW` dir fds (a symlinked "directory" entry
   is unlinked as a link, never traversed); regular files, links,
   and other non-directory entries (FIFO/socket/device) are
   unlinked via dir-fd with no `open()` on them. `missing_ok=True`
   makes an absent `name` a no-op (first retry; round-2 P3-5). The
   ROOT entry itself: if `name` is a symlink rather than a
   directory it is unlinked as a link (unreachable in the shipped
   flow — step 1 guarantees `previous` came from a real directory —
   but specified so the primitive is total; round-2 P3-5). Scope
   note in the docstring: this primitive is for QUARANTINE/residue
   trees whose contents are untrusted by construction;
   `rmtree_no_follow`'s refuse-symlinks policy remains correct for
   trees where a symlink is evidence of tampering. ERROR CONTRACT
   (run-2 r2, same rationale as D2.1): traversal/unlink/rmdir
   `OSError`s are wrapped `kind="io"` explicitly; walk-stage
   safety refusals keep `kind="unsafe"`. Existing
   `rmtree_no_follow` is untouched.

## D3. Authorship invariant (what this buys)

After D1, every byte under `output` at `write_manifest` time
(`runtime.py:3085-3110`, called at `:598` post-solve) was produced by
THIS attempt — solve output, or this attempt's own
`install_recovered` writes. `_final_ic_entry` (`:3122-3144`) is
UNCHANGED; its existence check now proves authorship by construction.
The #1325-rejected clock/mtime predicate stays rejected.

Non-interference pins (explorer facts 2, 6, 7; round-1 P2-2
correction):
- The publish gate enumerates TWO candidate root families — the
  workspace root at the literal `runs/<run_id>/output`
  (`state_cli.py:649`) and the `output_uri` object-store root
  (`:652-657`), per the base spec's multi-root requirement. The pin
  here is only that `output_residue` is never a candidate root —
  the gate's root set is unchanged by this change.
- `upload_results` walks strictly under `output` (`runtime.py:1051`,
  `_upload_directory:1910-1925`); residue is never uploaded.
- The durable-reuse/state_save_qc restart lane never re-enters
  `execute()` (`scheduler_state_failure.py:222-263`
  `native_shud_resubmitted: False`; `chain_forecast_execution.py:140`
  resumes from `restart_stage`) — the hook cannot destroy a
  witnessed tree that lane still needs.

## D4. Anchors (A1-A7; RED on master 55c020de unless marked)

- **A1 stale-residue-not-blessed** (issue AC-1, the harm): build
  `runs/<run_id>/output` with a stale `demo.cfg.ic.update` (+ stale
  witness manifest); run `execute()` with ZERO checkpoint hours and
  a solver stub that writes the REQUIRED chain outputs
  (`demo.rivqdown` with the expected rows — `verify_output`,
  `runtime.py:1011-1047`, must pass so the anchor exercises the
  success lane; round-1 P3-6) but NO final IC ⇒ post-run manifest
  has NO `final_ic` entry (and the stale bytes are not at
  `output/demo.cfg.ic.update`). RED on master: the writer blesses
  the residue (`final_ic` names it with fresh provenance).
- **A2 residue locatable** (issue AC-quarantine): same geometry ⇒
  the stale tree sits complete at `output_residue/previous`
  (contents byte-identical, including its `state_checkpoints/`
  subtree and any symlink moved un-followed). RED on master (no
  quarantine exists). Forensic attribution note: the directory name
  claims nothing about the producer; the residue's own manifest
  provenance is the attribution record (D1 step 4).
- **A3 no-regression pins** (issue AC-2, GREEN both sides): (a) the
  18 existing `execute()` sites in `tests/test_shud_runtime.py`
  (plus the two e2e sites) and the #1325 A7/A11 writer +
  round-trip anchors pass unchanged; (b) fresh-first-attempt lane:
  no `output_residue` is created when `output` did not pre-exist;
  (c) empty-reuse lane (round-2 P2-1 pin, guts-level GREEN on new
  code): pre-existing EMPTY `output` alongside an existing
  `output_residue/previous` residue ⇒ the empty dir is reused, no
  quarantine happens, and the residue tree is retained untouched —
  teeth against the naive unconditional-quarantine implementation
  that would evict the only real evidence.
- **A4 symlink guard** (issue AC-3 adapted — recorded deviation
  below; SPLIT color, round-2 P2-2 relabel): `output` itself a
  pre-existing symlink at `execute()` start ⇒ typed
  `WORKSPACE_PATH_UNSAFE`, link NOT followed, target untouched —
  the TYPED-ERROR half is GREEN both sides (master already types
  this via `ensure_directory_no_follow`'s `O_NOFOLLOW`
  final-component open, `safe_fs.py:18`, `:59-61`, merely
  unanchored). The ACCOUNTING half is RED on master: master dies at
  the `:393` dir loop OUTSIDE the `try`, so no failure log, no
  task-outcome receipt, no `mark_failed`; the new code raises
  inside the `try` and all three fire (disclosed as a behavior
  delta in the proposal). Additional differential teeth: assert NO
  `output_residue` is created (the error precedes any quarantine
  mutation — RED against a naive quarantine-before-validate
  implementation).
- **A5 bounded retention**: two successive retries with residue ⇒
  `output_residue` contains ONLY the latest residue tree at
  `previous` (the older one was removed — including when the older
  residue contains a SYMLINK entry, pinning
  `remove_tree_allow_symlinks` against the P1-1 lockout). RED on
  master.
- **A6 non-interference**: after a successful attempt that
  quarantined residue, (a) uploaded object keys are exactly the
  `output` tree (no `output_residue` key); (b) `save_state_for_run`
  against the workspace root publishes the fresh attempt's artifacts
  (gate never sees residue). GREEN-both-sides pin for (b) semantics,
  RED for the combined geometry (master would leave residue inside
  `output` where the two-exact-paths writer can bless it — same red
  as A1 viewed from the publish side).
- **A7 hygiene failure lanes** (round-1 P1-2 accounting pin +
  run-2 error-code split BY SHAPE, TWO sub-anchors):
  (a) I/O lane — THREE forced-failure points, ALL required, each
  the tooth for a distinct run-2 defect (r2 P2-1: conjunctive, not
  a pick-one disjunction): (i) the step-4 rename or step-3 clear,
  (ii) the step-5 RECREATE (the `_ensure_directory` mis-typing
  lane, r1 P1), and (iii) the step-1 probe/emptiness listing (the
  step-index escape lane, r1 P2-1). Injection is AT THE OS LEVEL
  BENEATH the safe_fs primitive (monkeypatch e.g. `os.rename` /
  `os.mkdir` / `os.scandir` to raise a plain `OSError` such as
  ENOSPC/ESTALE) so each primitive's REAL `kind="io"` wrap (D2
  error contracts) is on the assertion path — monkeypatching the
  primitive itself to raise a hand-built `kind="io"` error would
  leave a wrong in-primitive kind unobserved (r2 P2-2) ⇒ each
  point terminates the attempt BEFORE any solve spawn with
  `STORAGE_WRITE_FAILED`. For point (ii) the `os.mkdir` patch MUST
  be discriminated to the `output` target (step 2's
  `output_residue` mkdir reaches the same syscall — an
  undiscriminated patch fires at step 2 and leaves the recreate
  lane silently untested, r3 P3), and the anchor asserts the
  quarantined tree already sits at `output_residue/previous` when
  the failure fires, proving steps 2-4 completed. For all points,
  assert
  `services.orchestrator.retry.is_retryable_failure(
  "STORAGE_WRITE_FAILED")` is True (the retry-classification pin
  that run-1 lacked), and the failure collateral all fires: failure
  log written, task-outcome receipt written (array-accounting
  channel; call `:421`, def `:1958`) carrying that code,
  `repository.mark_failed` called.
  (b) tamper lane: `output` is a symlink (A4 geometry) ⇒
  `WORKSPACE_PATH_UNSAFE` receipt + same three-piece accounting;
  assert `is_retryable_failure("WORKSPACE_PATH_UNSAFE")` is False —
  permanent by intent. Sibling-geometry tooth: a pre-existing
  `output_residue` that is itself a symlink (kind="unsafe" from
  step 2) lands in the SAME permanent lane, pinning the shape rule
  against a steps-2-5-are-transient implementation. Guts-level pins
  (GREEN by construction on the new code; the accounting +
  classification assertions are the teeth).

Oracle-integrity rule: no existing assertion is weakened; all
existing `execute()`/`run_shud` tests keep verbatim assertions
(explorer fact 5: none pre-seed `output` via `execute()`, so no
fixture rework is expected — any needed adjustment is a reported
deviation, not a silent edit).

**Recorded deviation from issue AC-3 wording**: the AC asks that
symlink/subdir/non-regular entries INSIDE the dir make "清理拒绝且不
跟随". Under rename-aside there is no per-entry clearing to refuse:
inside-tree entries are moved wholesale without following (A2), and
the refusal case narrows to `output` ITSELF being non-directory (A4)
or the rename failing (A7). The AC's intent — no-follow, no
half-clean, explicit termination — is fully covered; the letter
(per-entry rejection) is intentionally not implemented. This is a
consumed-seam adaptation, reported upstream in the PR's 偏离记录.

## D5. Known limits

- Retention-one policy loses residue older than the latest retry.
  Forensic caveat (round-1 P3-4): per-attempt LOGS are truncated by
  the next attempt (`_open_log_file_no_follow` uses `O_TRUNC`,
  `runtime.py:2098`; `upload_logs` rewrites the same object keys),
  so the retained residue tree plus task-outcome receipts are the
  durable per-attempt evidence — the "logs persist" claim holds
  only until the next attempt.
- Residue is never garbage-collected (round-1 P3-5, explicit
  accepted cost): every run that ever retried keeps EXACTLY ONE
  extra output tree on the node-22 NFS indefinitely (bounded per
  run, unbounded across runs). Successful-completion cleanup is
  deliberately out of scope (non-goal 4) — deleting evidence on
  success would erase exactly the tree an operator needs when the
  published result is questioned.
- Root-collision precondition (round-1 residual risk): when
  `OBJECT_STORE_ROOT` is unset it defaults to the workspace root
  (`runtime.py:122`), collapsing the object-store
  `runs/<id>/output` onto the workspace tree — attempt-start
  quarantine then relocates the "published" object copy. Production
  configures disjoint roots (`infra/README.two-node-docker.md:129`);
  in the collapsed dev geometry the re-executed attempt's
  `upload_results` re-publishes the tree, so the window is
  dev-only and self-healing. Recorded, not guarded — a root-layout
  guard is out of scope.
- The object-store copy of an OLDER attempt is untouched (multi-root
  identity questions remain #1329's contract).
- Quarantine happens only through `execute()`; hand-driven
  `run_shud` invocations (tests, hypothetical operator use) get no
  hygiene — the production entry point is `execute()`
  (`infra/sbatch` lane), recorded here.
