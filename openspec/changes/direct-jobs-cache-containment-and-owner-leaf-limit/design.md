# Design

Line numbers reference `services/orchestrator/file_orchestration_journal.py`
at `origin/master` `9785e52d` (14,999 lines) unless another file is named.
Symbol names are authoritative; a line number is a locator only.

## Risk triage

```text
Issue type: bugfix (#1941) + design ruling with zero code change (#1942), one shared helper
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium — one file; the direct-jobs cache feeds every cycle-rows read and the
  rollback-quiescence predicate
Fixture level: expanded
Upstream suggested level: absent (follow-ups filed by hand from PR #1939) — expanded is
  mandatory: `symlink`, `path`, shared cache state and a destructive-operation predicate
  are all touched
Repair intensity: high — file IO/path safety + shared cache state in one helper root
Why:
- #1941 is the same path-safety hole PR #1939 closed on the cycle-rows cache, one cache over
- the third caller of the cache (`:10106`) gates a destructive rollback decision
- #1942 is a ruling on a stated limit of a fingerprint-free fast path; the ruling must be
  recorded where the next reader looks (design, test docstring), not left in an issue thread
Selected risk packs: File IO/path safety; Concurrency/shared state; Error handling/rollback/
  partial outputs
Not selected: Resource limits/discovery (no enumeration change); Legacy compatibility (no
  on-disk format or writer change); Security/authz (no public boundary changes)
OpenSpec change: direct-jobs-cache-containment-and-owner-leaf-limit
Evidence floor: tasks.md §0
```

## D1 — #1941: the direct-jobs cycle cache signs under containment and never stores a fault

### Today

`_direct_pipeline_job_records_for_cycle_cached` (`:5972-6020`):

```text
signature = (_stat_signature(root/"pipeline-jobs"),
             _stat_signature(root/"pipeline-jobs"/"by-cycle"/<seg>/<cycle>))   # :5988-5996
cached = _direct_jobs_cycle_cache.get(key); hit iff cached[0] == signature       # :5997-6000
jobs = list(_iter_direct_pipeline_job_records_for_cycle(...))                    # :6006-6013
store (signature, jobs) unconditionally                                          # :6014-6019
```

`_stat_signature` returns `None` for any `OSError`, so a by-cycle partition
parent swapped for a symlink to a decoy that also lacks `<cycle>` signs
`None` before and after the swap. The lookup compares equal, the warm instance
returns the cached `[]`, and `_iter_direct_pipeline_job_records_for_cycle`
— which would raise `file_journal_unsafe_scanned_entry` on that tree — never
runs. Measured by PR #1939's verifier (cand-02): identical for `model_a` and
`model_id=None`.

### Change

Both legs route through `_containment_stat_signature` (`:9671`), which
returns `(mtime_ns, size, ino)`, `None` for genuine absence under real
directories, or `_FINGERPRINT_CONTAINMENT_FAULT` (`:711`) on
`SafeFilesystemError | OSError`. Then, KISS:

```text
signature = (_containment_stat_signature(pipeline-jobs),
             _containment_stat_signature(by-cycle/<seg>/<cycle>))
faulted = _signature_has_containment_fault(signature)          # :714, recursive over tuples
hit only if not faulted and cached[0] == signature
jobs = recompute (raises on the tampered tree exactly as a cold read does)
store only if not faulted
```

- **No cache-type widening.** `_direct_jobs_cycle_cache` (`:1101`) keeps its
  `tuple[tuple[Any, ...], list[dict]]` shape; the marker never enters it. A
  stored signature therefore can never compare equal to a faulted one, and the
  explicit `not faulted` on the lookup is belt-and-braces, not load-bearing.
- **Fail-closed direction, unchanged token.** The recompute reads the tampered
  path, so the observable fate of a faulted read is the raise the recompute
  already produces for a cold instance (`file_journal_unsafe_scanned_entry`
  from the scanned-entry discipline inside
  `_iter_direct_pipeline_job_records_for_cycle`). D1 introduces no new token
  and no new exception type. Where the containment stat faults but the
  recompute does not (a transient `EIO` / `PermissionError` on the stat
  alone), the rows are returned and simply not cached — the next read
  recomputes.
- **Absence still caches.** `_containment_stat_signature` returns `None` for
  a missing `<cycle>` child under a real `by-cycle/<seg>` directory, so an
  untouched empty partition signs `(sig, None)` twice and the second read is a
  hit. This is the regression guard for the performance property the cache
  exists for.

### Callers and their fate after D1

| Caller | Lane | Before (warm, hard variant) | After |
|---|---|---|---|
| `_cycle_rows` miss path `:5835` | `model_a` **and** `model_id=None` (both lanes reach the same call; `_cycle_rows` keys on `model_id` `:5734`, the direct cache keys on `(source_id, cycle_segment)` `:5986`) | `[]` served | raises `file_journal_unsafe_scanned_entry` |
| owner fast path → `_cycle_directories_probe_faulted` (by-cycle dir is in the probe list `:9732`) → `_cycle_rows` recompute → `:5835` | write-window owner | probe forces recompute, recompute hits warm direct cache, `[]` served | same raise |
| `_cycle_rows_by_model_unlocked` `:5907` (`include_direct_jobs=True` branch) | none live — all six call sites (`:2404 :3697 :4059 :4222 :4914 :9936`) pass `include_direct_jobs=False` | dead code today | same helper, no test row |
| retention inspection: `open_retention_cycle` `:1146` → `FileJournalRetentionCycle.inspect()` `:939` → `_inspect_retention_cycle_unlocked` `:10074`, which reads `_cycle_rows(model_id=None)` at `:10099` **before** the direct call at `:10106` and catches `FileOrchestrationJournalError` at `:10126` into `status="blocked", reason=error.reason` | retention / destructive-operation decision | second in-window `inspect()` after the tamper: `:10099` hits the warm direct cache → `[]` → no blocking row → `status="eligible"` | `:10099` raises inside `_cycle_rows`, `:10106` is never reached, `inspect()` returns `status="blocked"`, `reason="file_journal_unsafe_scanned_entry"` — field-for-field what a fresh instance returns on the same tree |

The last row is the one reviewers should read first: it gates a destructive
rollback (`remove_members`, which `inspect()` itself never calls), and the
only outcome D1 removes is "declare a slice eligible on a listing the tree no
longer backs". The lane does not raise — it never did for a cold read either;
it reports the blocked row with the cold-read reason. Note the window entry at
`:1190` wipes all three caches, so the warm cell exists only *inside* one
window: first `inspect()` populates, tamper, second `inspect()` on the same
window object.

### Cost

PR #1939 measured `_containment_stat_signature` at roughly the per-directory
cost of `_open_directory_no_follow` re-walking from `/` (linear in absolute
root depth; the five-directory owner probe was 191 Python-level `os.*` calls
at a 14-component root). D1 adds two such stats per direct-cache validation on
top of the 414 the cycle-rows fingerprint already spends per non-owner read.
The number is to be **measured, not estimated**: tasks.md §1 requires the
implementer to record the syscall delta for one warm cache-hit public read
(`list_stage_statuses`) before/after on the same tree and root depth, and the
figure is written back into this section by the orchestrator before the PR is
opened.

Measured (Phase 2, implementer scratch script, Python-level interception of
`os.{stat,lstat,open,close,fstat,scandir,listdir,readlink,read,dup}`, one
empty cycle tree at a 9-component realpath root, master `9785e52d` vs branch):

| `list_stage_statuses` read | before | after | delta |
|---|---|---|---|
| same-`model_id` warm hit (answered by `_cycle_rows_cache`, direct cache not consulted) | 334 | 334 | 0 |
| cross-model read (cycle-rows miss, direct-cache hit) | 628 | 688 | +60 |
| cold first read | 747 | 807 | +60 |

The two containment stats cost about 30 `os.*` calls each at that depth and
land only on reads that reach the direct cache. Depth caveat: the D3 figures
(191 / 414 / 422) were taken at a 14-component root and are not comparable
across depths.

## D2 — #1941 sibling copy: `_cycle_job_records_cache` has no warm/cold split

#1941 asks for one sentence on the sibling. Probed (scratch script, realpath
tmp root, empty cycle tree, public entry
`_iter_pipeline_job_records_for_cycle(source_id, cycle_time)` `:6788`) with
each parent — `latest/gfs`, `journal/gfs`, `pipeline-jobs`,
`pipeline-jobs/by-cycle/gfs` — swapped for a symlinked decoy after a warm
read:

| Parent swapped | warm second read | cold read |
|---|---|---|
| `latest/gfs` | raises `file_journal_unsafe_scanned_entry` | same |
| `journal/gfs` | same | same |
| `pipeline-jobs` | same | same |
| `pipeline-jobs/by-cycle/gfs` | `[]` | `[]` |

Mechanism, which is what makes this hold for populated trees and not only for
the probed empty one: `_cycle_job_records_signature` (`:6707`) takes its
per-file bare stats only *after* enumerating through `_iter_regular_json_files`,
`_iter_jsonl_files` and `_flat_direct_pipeline_job_paths_for_cycle`, all of
which open their directories under the no-follow containment discipline and
raise before any bare stat is reached. The by-cycle partition is not an input
to that lane at all, so its swap is invisible to warm and cold alike — a
non-difference, not a hole. **No code change.** The conclusion is recorded
here and in the PR's 偏离记录; it rests on the mechanism plus the empty-tree
probe.

## D3 — #1942: the owner leaf-swap is a permanent stated limit (option B)

### The two options as priced by PR #1939's verifier (cand-03, cand-04)

- **A — probe leaves too.** The owner fast path exists to skip the source-file
  fingerprint. The five leaf cells that can be swapped during a window
  (`journal/<src>/<cycle>.jsonl`, `pipeline-events/<src>/<cycle>.jsonl`,
  `latest/<src>/<cycle>/<model>.json`, `pipeline-jobs/by-cycle/<src>/<cycle>/*.json`,
  `pipeline-jobs/*.json`) are exactly the files the fingerprint stats. A leaf
  probe is therefore the fingerprint under another name: the directory probe
  already costs 191 syscalls for five directories (about 4× one segment read),
  the full fingerprint 414, and a warm cache-hit public read 422 versus 20
  before PR #1939. Option A would take the owner from "probe only" to "probe +
  fingerprint" on every in-window hit, i.e. collapse the fast path into the
  non-owner path while keeping the probe's cost on top.
- **B — accept as stated limit.** The owner is the thread holding the cycle
  flock for that cycle; a leaf swapped under it during the window is an
  out-of-band tamper concurrent with the owner's own write. Its exposure is
  bounded to the window: the owner's next append invalidates every reachable
  key for the pair (spec `:796`), and the first read after the window
  revalidates under the full containment-aware fingerprint (D1 of PR #1939),
  which sees the swapped leaf and fails loud. The non-owner and cold lanes
  never had the hole.

### Ruling

**B.** The limit is permanent, not "until the residual is closed". What
changes on disk:

- `test_cycle_write_window_owner_hit_does_not_see_a_leaf_swap_stated_limit`
  (`tests/test_file_orchestration_journal_read_cache.py:1344`) keeps pinning
  the observable behavior. Its docstring currently says the test "must be
  FLIPPED" when the residual is closed; that sentence is now false and is
  replaced by the ruling, citing #1942 and the cost figures above.
- The `_cycle_directories_probe_faulted` docstring / the D1b comment near
  `:5757` keep saying "directories only"; no wording promises a future flip
  there, so they are unchanged.
- Spec `pipeline-job-persistence` `:798` already states "a leaf file swapped
  for a symlink during the window is a stated limit of the fingerprint-free
  owner path, not a promise of this requirement". That sentence is the
  normative record; no delta is needed for the ruling.

### Owner lane and the by-cycle hard variant

Not to be confused with #1942: the owner's *directory* probe does see a
swapped `pipeline-jobs/by-cycle/<src>` (it is in the probe list) and forces a
recompute — which, before D1, hit the warm direct cache and served `[]`. D1
closes that cell for the owner lane too; tasks.md §1 carries a test row for
it. After this PR the owner path's only stated limit is the leaf swap D3
rules on.

## Invariant Matrix

```text
Governing invariant: Every cache the cycle-rows recompute consults — the cycle-rows cache and
  the direct-jobs cycle cache alike — judges the identity of its sources under the containment
  discipline, so a cold instance, a warm instance and the write-window owner give one answer
  for one tree for a parent component swapped for a symlink; the sole remaining exception is the
  ruled, spec-stated owner leaf-swap limit, bounded to the window that grants the fast path.

| # | Invariant | Where enforced | Test / evidence | Status |
|---|---|---|---|---|
| 1 | Both direct-cache signature legs resolve through `_containment_stat_signature` | `_direct_pipeline_job_records_for_cycle_cached` | grep: no `_stat_signature(` remains in that function; flipped marker test | new |
| 2 | A faulted direct-cache signature is never stored and never a hit | same, store + lookup short-circuit | `test_fingerprint_that_observed_a_containment_fault_is_never_stored` hard variant asserts the public token for `model_a` and `None` and `after == before` for `_direct_jobs_cycle_cache` | flipped |
| 3 | Warm == cold on the by-cycle hard variant, both model lanes | recompute raises inside `_iter_direct_pipeline_job_records_for_cycle` | same test + a fresh-instance cold-side pin (tamper before the first read, cache stays empty; green on master by construction, it pins the cold answer the warm cell is compared to) | new |
| 4 | Owner in-window by-cycle hard variant raises, not `[]` | owner probe → recompute → D1 | new owner test row | new |
| 5 | An untouched empty by-cycle partition still hits the cache on the second read | `None` for genuine absence | two reads that both miss `_cycle_rows_cache` but share the direct key (`model_a` then `None`), `_iter_direct_pipeline_job_records_for_cycle` called exactly once | new |
| 6 | Retention inspection reports the tamper as a blocked row, never `eligible` | `_inspect_retention_cycle_unlocked` unchanged; behaviour moves at `:5835` via `:10099` | one `open_retention_cycle` window: `inspect()` → hard-variant tamper → `inspect()` == `blocked`/`file_journal_unsafe_scanned_entry`, equal to a fresh instance's inspection; master gives `eligible` | new |
| 7 | No new exception type at any public boundary | tokens unchanged | tests assert `file_journal_unsafe_scanned_entry` only | pinned |
| 8 | Sibling `_cycle_job_records_cache` unchanged | no diff | D2 table + `git diff` empty at `:6707-6870` | recorded |
| 9 | Owner leaf swap remains observable as the stated limit | no code change | `:1344` pin still green; docstring cites #1942 | reworded |
| 10 | `test_cycle_write_window_owner_keeps_fingerprint_free_fast_path` (`:434`) stays green | owner path untouched | existing test | pinned |
| 11 | Direct cache cost delta measured, not estimated | — | syscall delta recorded in D1 "Measured" | evidence |
```

## Boundary-surface checklist

- Shared helper roots: `file_orchestration_journal.py` (only source file
  changed).
- Public entrypoints: unchanged signatures; behaviour change only where a
  warm read previously served `[]` across a tampered by-cycle partition.
- Read surfaces: `_direct_pipeline_job_records_for_cycle_cached` hit/miss/store;
  its live callers `:5835` and `:10106` (the latter reached only after `:10099`
  survives), plus dead `:5907`.
- Write/delete/overwrite surfaces: none changed; retention `inspect()` is a
  *predicate* feeding `remove_members` and now reports the tampered tree as
  blocked instead of eligible.
- Staging/publish/rollback: rollback quiescence (row 6 of the matrix).
- Producer/consumer evidence boundaries: error token unchanged; node-27
  receipt for the journal suite (tasks.md §0).
- Stale-state/idempotency boundaries: direct cache eviction and key shape
  unchanged; marker never stored.
- Unchanged downstream consumers: `_cycle_job_records_cache` (D2), the
  owner probe list, every other `_stat_signature` caller.

## Review focus

1. D1: is every stat in `_direct_pipeline_job_records_for_cycle_cached`
   routed through the helper, and is the faulted signature provably neither
   stored nor a hit — including under the `_cache_lock` ordering already there?
2. Row 6: the warm second `inspect()` must equal the fresh instance's
   inspection field for field (`status`, `reason`), and the test must build
   the warm cell inside one window (the `:1190` entry wipe otherwise makes it
   a cold read).
3. Row 5: the cache-hit guard must count recompute calls across two reads
   that both *miss* `_cycle_rows_cache` (different `model_id`), otherwise the
   outer hit at `:5790` returns before `:5835` and an always-miss direct cache
   would still count 1.
4. D2: agree or refute the mechanism claim (enumerators raise before any bare
   stat) by reading `:6707-6790`, not by re-running the empty-tree probe.
5. D3: the reworded docstring must not soften the pin — the test still asserts
   the owner *does not* see the leaf swap.
