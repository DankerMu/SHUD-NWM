# Tasks

Fixture level: expanded · repair intensity: high · issues: #1941 #1942
(one PR; #1941 is the only code change, #1942 is a recorded ruling).
Line cites are against `services/orchestrator/file_orchestration_journal.py`
at `origin/master` `9785e52d` (14,999 lines); symbol names are authoritative.

## 0. Evidence Floor

Oracle is local pytest on two filesystem semantics **plus node-27** for the
journal suite (#1941 acceptance: `pytest tests/test_file_orchestration_journal.py -q`
在 node-27 通过; the new tests land in `tests/test_file_orchestration_journal_read_cache.py`,
so both files run there). Sync discipline: `git status --porcelain` first,
`git pull --ff-only`, never stash-pop, `export PATH=$HOME/.local/bin:$PATH`.

- [x] `uv run pytest tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py -q` green on the default (case-insensitive) macOS volume
- [x] The same two files green with `--basetemp` on a case-sensitive APFS volume (`hdiutil` dmg; the two filesystem-branching pins from PR #1939 run their other branch here)
- [x] The same two files green on node-27 (`/home/nwm/NWM`, ff-only sync, `uv run pytest … -q`), receipt (host, HEAD SHA, pass line) in the PR evidence
- [x] `uv run pytest tests/test_orchestration_chain.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_scheduler_journal_retention_archive.py tests/test_scheduler_journal_retention_planning.py -q` green (no chain / scheduler / retry / retention consumer regresses on the direct-cache change; the two retention files drive `open_retention_cycle`, matrix row 6)
- [x] `uv run ruff check .` clean
- [x] Red proof against pre-change source: the flipped hard-variant marker test (1.3), the owner test (1.5) and the retention-inspection test (1.7) shown red on master `9785e52d` and green after; 1.4 and 1.6 are pins expected green on master and are stated as such; `git stash list` holds no `red-proof` entry afterwards
- [x] Direct-cache syscall delta measured (one warm cache-hit `list_stage_statuses` read, before vs after, same tree and root depth) and written into design D1 "Measured"
- [x] `openspec validate direct-jobs-cache-containment-and-owner-leaf-limit --strict --no-interactive`

## 1. #1941 — direct-jobs cycle cache under containment (D1)

- [x] 1.1 `_direct_pipeline_job_records_for_cycle_cached` (`:5972`): both signature legs use `_containment_stat_signature`; compute `faulted = _signature_has_containment_fault(signature)`; the lookup requires `not faulted`; the store runs only when `not faulted`. No change to the cache type (`:1101`), key, eviction, or the recompute call.
- [x] 1.2 Update the function docstring: the signature is containment-aware and a faulted signature is never stored; keep the `#1734 D11` comment.
- [x] 1.3 **Flip, don't duplicate**: in `tests/test_file_orchestration_journal_read_cache.py::test_fingerprint_that_observed_a_containment_fault_is_never_stored` (`:1247`) remove the `if not hard_variant` guards (`:1296`, `:1312`) so the `by_cycle_direct_partition` leg asserts `_FINGERPRINT_PARENT_TOKENS["by_cycle_direct_partition"]` for `model_a` and `model_id=None`, plus `after == before` for `_direct_jobs_cycle_cache` (no marker in any stored signature). Rewrite the docstring paragraph at `:1267` that names the residual.
- [x] 1.4 Fresh-instance cold-side pin (green on master by construction): tamper the by-cycle partition (hard variant, no `<cycle>` child either side) *before* the first read on a new repository; assert the public token and that `_direct_jobs_cycle_cache` is empty afterwards. This is the cold answer 1.3/1.5/1.7 compare against.
- [x] 1.5 Owner lane: inside `_locked_cycle_write(gfs, cycle)`, first read populates; swap `pipeline-jobs/by-cycle/gfs` for the hard-variant decoy; second in-window read raises `file_journal_unsafe_scanned_entry` (was `[]`). Keep `test_cycle_write_window_owner_keeps_fingerprint_free_fast_path` (`:434`) green.
- [x] 1.6 Cache-hit guard: untouched empty `by-cycle/gfs/<cycle>` directory; two public reads that both miss `_cycle_rows_cache` but share the direct key — `model_id="model_a"` then `model_id=None` (`_cycle_rows` keys on `model_id` `:5734`; the direct cache keys on `(source_id, cycle_segment)` `:5986`); `_iter_direct_pipeline_job_records_for_cycle` called exactly once (monkeypatch counter), rows `[]` both times. Two same-`model_id` reads would return at `:5790` before reaching `:5835` and prove nothing.
- [x] 1.7 Retention-inspection row: one `open_retention_cycle` window (`:1146`; its entry wipe at `:1190` clears all three caches, so the warm cell must be built inside the window): `inspect()` on an empty tree → swap `pipeline-jobs/by-cycle/gfs` for the hard-variant decoy → `inspect()` again on the same window object. Assert `status == "blocked"` and `reason == "file_journal_unsafe_scanned_entry"`, field-for-field equal to a fresh instance's `inspect()` on the same tree — taken after the first window has exited (a second repository opening the window while the first is still open gets `status == "busy"`, see `tests/test_file_orchestration_journal.py:415-418`), or by calling `_inspect_retention_cycle_unlocked` directly. Red on master: the second inspection is `eligible` (the `:10099` `_cycle_rows` read hits the warm direct cache, no blocking row). `inspect()` itself never calls `remove_members`, so "no rollback ran" is the status assertion, not a side-effect check.
- [x] 1.8 Syscall delta script (scratch, read-only; not committed): `os.*` interception around one warm `list_stage_statuses` hit, master vs branch, same realpath root; number reported in the implementer report → design D1.

## 2. #1941 sibling copy (D2)

- [x] 2.1 No code change to `_cycle_job_records_signature` / `_cycle_job_records_memoized` (`:6707-6870`); `git diff` empty there. D2 table and mechanism recorded in design and in the PR 偏离记录.

## 3. #1942 — ruling B (D3)

- [x] 3.1 Reword the docstring of `test_cycle_write_window_owner_hit_does_not_see_a_leaf_swap_stated_limit` (`:1344` on master, `:1670` at head): drop "must be FLIPPED when the residual is closed"; state the limit is ruled permanent by #1942 with the cost reason (probe 191 / fingerprint 414 / hit 422 vs 20 syscalls; a leaf probe is the fingerprint). Assertions unchanged.
- [x] 3.2 No change to `_cycle_directories_probe_faulted` (`:9701`) or the probe list.

## 3b. Round-1 review repairs (test-oracle, verified)

- [x] 3b.1 Signature-only fault test (matrix row 2b): on a clean `_empty_cycle_tree`, monkeypatch `repository._containment_stat_signature` to return `_FINGERPRINT_CONTAINMENT_FAULT` for the `pipeline-jobs` path only; two reads (`model_a`, then `None`) both recompute (counter == 2), `_direct_jobs_cycle_cache` stays empty; remove the patch, next read recomputes then hits. Mutation "drop `if not faulted`" must go red.
- [x] 3b.2 Parametrize 1.6 over `_empty_cycle_tree` and `_hard_variant_tree` (spec scenario "including a real by-cycle partition with no `<cycle>` child").
- [x] 3b.3 Replace the vacuous no-marker assertion after `direct_after == direct_before` with a comment, or drop it.
- [x] 3b.4 owner leaf-swap pin docstring (`:1670` at head): "any leaf-level change (symlink swap, plain-file add/replace/remove)" and the depth caveat on the 191/414/422 figures (14-component root; this PR's 334 warm hit is at 9).

## 3c. Round-2 review repairs (spec-contract, verified)

- [x] 3c.1 Owner-requirement delta: the window-boundedness clause scoped to changes that move a probed directory's stat identity; an in-place rewrite of a direct-job leaf is named as the direct cache's pre-existing directory granularity (r2-cand-01).
- [x] 3c.2 "Permanent" restored in design D3 heading/ruling and proposal, consistent with #1942's definition of option B; option C stays priced-and-rejected, no reopen condition (r2-cand-02).
- [x] 3c.3 D3 provenance: APFS measured, ext4 reasoned (r2-cand-03).
- [x] 3c.4 Docstrings falsified by the diff — `_cycle_job_records_signature` (`:6743`) and `tests/test_file_orchestration_journal.py:15515` — name `_containment_stat_signature`; the owner pin docstring's "exposure bounded to the window" sentence scoped like 3c.1 (r2-cand-04 + verifier note).

## 4. Spec + docs

- [x] 4.1 `specs/pipeline-job-persistence/spec.md` delta: MODIFIED "Journal existence probes SHALL enforce filesystem containment before declaring absence" — name the direct-jobs cycle cache, drop the recompute-reads-tampered-path bound from the warm/cold scenario, add the by-cycle hard-variant scenario, widen the never-stored scenario to both caches.
- [x] 4.2 Second MODIFIED block for the owner fast-path requirement (`:796`): the stated-limit sentence widened from "a leaf file swapped for a symlink" to any leaf-level change beneath the probed directories, with the window-bounded exposure (review round 1, cand-02). The MODIFIED containment requirement names the owner only for the parent-component swap and cross-references that limit.
- [x] 4.3 #1941 acceptance item 4 (rewrite the archived change's `design.md:390-391` / `:80-93`): not done — archived changes are immutable; the closure is carried by design D1 / matrix rows 2-4 and the spec delta. Recorded in `proposal.md` "Deviations recorded up front" and repeated in the PR 偏离记录.

## Risk packs

- **File IO/path safety** (selected): rows 1-4, 7 of the matrix.
- **Concurrency/shared state** (selected): rows 2, 5, 10 — store/lookup under
  `_cache_lock`, owner path untouched.
- **Error handling/rollback/partial outputs** (selected): row 6 — the
  destructive-operation predicate fails closed with the cold-read fate.
- Not selected: Resource limits/discovery (no enumeration change), Legacy
  compatibility (no format/writer change), Security/authz (no boundary change).

## Non-goals

- #1942 option A (leaf probing on the owner path) and option C (comparing the owner probe's directory tuples) — C priced and not adopted in design D3.
- Any other `_stat_signature` caller; `_cycle_job_records_cache` (D2).
- Reducing `_open_directory_no_follow`'s from-root re-walk cost (fd-reusing
  `stat_no_follow`) — separate perf item, out of scope here as in PR #1939.
