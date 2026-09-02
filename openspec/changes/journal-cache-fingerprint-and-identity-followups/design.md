# Design

Line numbers reference `services/orchestrator/file_orchestration_journal.py`
at `origin/master` `4f0ff53f` (14,713 lines) unless another file is named.
Symbol names are authoritative; a line number is a locator only.

## Risk triage

```text
Issue type: bugfix (x3) + perf (x1), all in one shared helper root
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium — one file, but it is the journal every orchestration read/write passes through
Fixture level: expanded
Upstream suggested level: absent (hand-written issues) — expanded is mandatory: `symlink`, `path`,
  `writer`, cache/shared-state, concurrency triggers all present
Repair intensity: high — file IO/path safety + shared cache state + write boundary in one helper root
Why:
- #1567 is a path-safety hole (symlinked parent) reachable through a long-lived cache
- #1658 edits the invalidation of a cache with a documented correctness precondition next to it
- #1760 adds a fail-closed gate on a production write lane
- #1761 changes which directories a read enumerates
Selected risk packs: File IO/path safety; Concurrency/shared state; Resource limits/discovery;
  Legacy compatibility; Error handling/rollback/partial outputs
OpenSpec change: journal-cache-fingerprint-and-identity-followups (generated)
Evidence floor: tasks.md §0
```

## D1 — #1567: one containment-aware signature helper for the whole fingerprint family

**Ruling: non-raising fault marker; the raise stays where it is.**

`_cycle_rows_source_fingerprint` (`:5962`) stats five kinds of path per source
segment: `latest/<seg>/<cycle>/*.json` via `os.scandir` (`:5982`), the
`journal/<seg>` and `pipeline-events/<seg>` segment slots via
`_cycle_segment_signatures` (`:6001`, `:6007` → def `:10220`, bare
`_stat_signature` at `:10228`), the by-cycle direct partition (`:6015`) and the
flat `pipeline-jobs` root (`:6025`). **All five follow symlinked parents
today** — the issue names only the segment slots, but patching one call site
would leave four sibling holes in the same fingerprint.

Rule: every stat in that family goes through one helper that resolves the path
with `stat_no_follow(path, containment_root=self.root)` (the same primitive
`_probe_stat_mode` `:9504` uses) and returns one of three values:

| outcome | return | cache effect |
|---|---|---|
| real entry | `(st_mtime_ns, st_size, st_ino)` | normal |
| genuine absence (`FileNotFoundError` under real directories) | `None` | normal — an empty directory stays a cacheable legal `[]` |
| containment fault (`SafeFilesystemError` / other `OSError`) | a dedicated **fault marker** object, never `None` | the whole fingerprint is **non-cacheable**: `_cycle_rows` neither hits nor stores |

Why a marker and not a raise: the fingerprint is computed *before* the cache
lookup on every read (`_cycle_rows` `:5725-5730`); raising there would move the
containment fault to a new frame ahead of the `_read_cycle_segments` contract
frame (`:10231-10240`, "this frame defines the public contract for a probe
containment fault"). A marker forces a miss; the recompute goes through
`_read_cycle_segments` → `_cycle_segment_paths` → the existing
`_probe_containment_failure`, so the exception type, code
(`file_journal_unreadable`) and lane fate are byte-identical to a cold instance.

Why non-cacheable and not merely unequal: if a marker-carrying fingerprint
were stored, the next read would compute the same marker, compare equal, and
serve the tampered rows — the exact hole in a new shape. `None` cannot be the
marker: `fingerprint=None` is already the owner-window signal (`:5725-5732`).

The `latest/<seg>/<cycle>` scandir is the one place the helper cannot be a
drop-in: probe the directory itself through the helper first; a fault marker
short-circuits the listing. Entries inside a probed-real directory keep their
`entry.stat(follow_symlinks=False)`.

Symlink-leaf coverage needs no new work: `follow_symlinks=False` already changes
the signature for a leaf symlink, and a leaf symlink present at cache time
cannot have produced a cached `[]`; the ancestor swap is the hole.

### D1b — the owner fast path probes containment, keeps skipping the fingerprint

The owner hit (`in_write_window`, `:5711-5732`) computes no fingerprint. Ruling:
**add the cheap probe, do not pin the residual.** On an owner hit, run the same
helper over the *directories* that feed the cycle (`journal/<seg>`,
`pipeline-events/<seg>`, `latest/<seg>/<cycle>`,
`pipeline-jobs/by-cycle/<seg>/<cycle>`, `pipeline-jobs`) — a handful of
`lstat`s, no file-level fingerprint; a fault marker turns the hit into a
recompute, which fails loud through the frame above. This keeps
`test_cycle_write_window_owner_keeps_fingerprint_free_fast_path`
(`tests/test_file_orchestration_journal_read_cache.py:434`) true as written —
the probe is not the source-file fingerprint — while closing the second bypass
the issue names. Both the code comment at `:5716-5724` (it names #1567 as open
scope) and the spec sentence "the owner's own fast path still performs no
tamper detection" are updated by this change (spec delta), so cold, warm, and
owner reads share one judgement.

Rejected: pinning the residual with a comment. The residual is real
(in-window read populates → external tamper → in-window hit with no append
between) and the probe costs less than one journal segment read.

## D2 — #1658: exit clear scoped by `(source_id, cycle_segment)` prefix; entry clear untouched

- `_locked_cycle_write` (`:10069`): the `finally` clear at `:10089` evicts only
  keys whose `key[0] == source_id and key[1] == cycle_segment`, where
  `source_id` is the **normalized** id the owner marker already carries
  (`:10080-10084`; cache keys are normalized in `_cycle_rows`, key built at
  `:5699`). Both the 4-tuple `_cycle_rows` keys and the base key
  `(source, cycle, None, None)` are swept — the stale-key sweep in
  `_apply_record_to_cycle_rows_cache` (`:9662`, `key != base_key`) deliberately
  *excludes* the base key because it updates it in place; the exit sweep must
  **include** it.
- The entry clear at `:10072` (`self._cycle_rows_cache.clear()` immediately
  under `with self._cache_lock:` at the top of `_locked_cycle_write`) is **not**
  touched. Its correctness argument (owner bypasses fingerprint on every hit; a
  pre-window entry another process invalidated would be trusted) is restated in
  the spec delta and remains a hard constraint for reviewers.
- What the narrowing actually preserves: only entries populated **during** the
  window, because the entry wipe is global. The acceptance test therefore must
  populate cohort Y's entry *after* X's window opens (same thread reading a
  different cycle inside the window revalidates and stores, `:5726`; or a
  non-owner thread). A test that populates Y before the window goes red for the
  wrong reason, and the tempting "fix" — narrowing the entry clear — is the one
  thing this issue forbids. `MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES = 512`
  (`:172`), so LRU eviction cannot confound the test.
- `_direct_jobs_cycle_cache` (key at `:5926`) and the #1734 memo
  (`:6635-6690`) are out of scope (already cycle-scoped). The two other
  `_cycle_rows_cache.clear()` sites (`:1153`, `:1167`) are not window clears
  and are untouched.

## D3 — #1761: identity dedup on both remaining sites

- `_merge_cycle_source_discovery(sources, source, *, root: Path | None = None)`
  (`:13504`; string check at `:13514`): when `root` is given, a candidate
  segment is dropped iff `_names_same_directory(root, segment,
  identities_of_a_kept_segment)` (`:13558`); when `root is None` the string
  dedup is kept unchanged. Both callers (`:5644`, `:5669`, inside the
  cycle-source discovery method) are methods and pass `self.root`.
- `_cycle_read_source_segments` (`:13581`) overrides branch (`:13588-13599`,
  string check at `:13595`): keep the per-item `_cycle_read_source_segment`
  call (`:13522`; source-mismatch validation → `file_journal_source_mismatch`)
  and the "empty after dedup → `file_journal_missing_identity`" fail-closed
  (`:13598`), and dedupe by identity when `root` is provided, mirroring the
  primary branch (`:13600-13618`).
- Symlinked aliases keep a distinct inode under `follow_symlinks=False` and
  stay in the list (`_names_same_directory` docstring `:13565-13575`) — do not
  weaken.
- The existing assertion `[("IFS", ("IFS", "ifs"))]`
  (`tests/test_file_orchestration_journal.py:5298`) becomes
  filesystem-dependent after the fix. It is **re-pinned** with the
  `_filesystem_is_case_sensitive` (test file `:14914`) branch/skip shape of the
  existing pin at test file `:14966` (test file `:14920` is filesystem-agnostic — it asserts no duplicated
  `(st_dev, st_ino)` on either semantics — and is a second shape to copy for
  agnostic assertions) — a recorded contract change, not an oracle edit. The sibling assertion at `:5236` is per-source singular-segment and is
  not re-pinned.

## D4 — #1760: fail-closed gate at the write boundary, before the first byte

- Gate: `_cycle_scope_from_job_id(job_id)` (`:12323`); when it returns a pair
  and `(normalized source, format_cycle_time(cycle))` differs from the row's
  own `(_source_id_from_job, format_cycle_time(_cycle_time_from_job))`, raise
  `FileOrchestrationJournalError("file_journal_job_id_scope_mismatch",
  field="job_id", evidence={"expected": ..., "actual": ...})` with the pair
  strings bounded the way sibling errors bound theirs (`[:80]`). `None` from
  the parser passes (fall-open, D1a/D4 of #1734 unchanged; production holds 2
  unparseable names).
- **Seam: `_validate_outgoing_record` (`:9419`), for
  `record_type == "pipeline_job"`.** It is the one write-side validator every
  pipeline-job lane calls *before* its first byte, at eight call sites:
  the five batch lanes (`:3724`, `:3829`, `:4064`, `:4255`, `:5131`, each
  validating every record ahead of `_append_journal_records_unlocked` at
  `:3732`, `:3837`, `:4072`, `:4263`, `:5139`), the single-row lane
  `_write_pipeline_job_unlocked` (`:9023`, ahead of the append at `:9060` and
  the master section `_write_current_master_unlocked` append at `:9106`), the
  repair lane `_restore_derived_master_direct_unlocked` (`:9203`, ahead of the
  direct write at `:9213`), and `_append_validated_record_unlocked` (`:9276`,
  validate at `:9306`, append at `:9320`) — which today carries only
  `forecast_cycle` / `hydro_run` / `pipeline_event` records, so the gate is
  inert there but present by construction should a caller ever route a
  `pipeline_job` through it. One definition, no per-lane copy; `_write_pipeline_job_direct_unlocked` (`:9133`) itself is
  **not** the seam because its callers (`:3737`, `:3842`, `:4077`, `:4279`,
  `:5166`, `:5168`, `:8923`, `:9074`, `:9120`, `:9213`) run after journal
  records may already be appended.
- The two lanes that do not derive `(source, cycle)` from the row:
  - `_restore_derived_master_direct_unlocked` (`:9157`) takes them as kwargs
    and appends no journal record. The gate fires inside its
    `_validate_outgoing_record` call (`:9203`) → repair fails closed before the
    direct write, and per its docstring the anchor sync never runs and the
    stale anchor is **kept**. Regression row: divergent canonical row →
    `file_journal_job_id_scope_mismatch`, no direct file, anchor still present.
  - `_project_committed_pipeline_job_write` (`:8897-8930`) wraps the direct
    write in `except Exception` → bounded warning + committed result. It runs
    only *after* the append (`:9060` / `:9106`, entered at `:9066` / `:9112`),
    which itself runs after the gate at `:9023`; a divergent row never reaches
    it. Regression row: through a public single-row writer **that enables the
    containment** — `reserve_pipeline_job` (`:2619`, passes
    `_committed_projection_containment=True` at `:2704`) or
    `reclaim_pipeline_job_reservation` (`:2707`, flag at `:2906`) — a divergent
    row raises the code, no journal record exists in any segment, no direct
    file exists, and no `committed_projection_fault` event was emitted
    (proving the gate fired before the append, not inside the swallow).
    `upsert_pipeline_job` / `append_historical_pipeline_job` are **not** valid
    vehicles for this assertion: they call `_write_pipeline_job_unlocked` with
    the default `_committed_projection_containment=False`, so the projection
    wrapper is never entered and the no-event assertion would be a tautology.
  - Historical import lane (Legacy compatibility pack):
    `append_historical_pipeline_job` (`:2603`) reaches the same gate via
    `_write_pipeline_job_unlocked`; its only non-test caller is
    `import_historical_scheduler_state`
    (`services/orchestrator/file_orchestration_migration.py:1240`, job loop at
    `:1278-1284`), which has no per-row containment: the only skip mechanism
    is the `_unsupported_job_reason` prefilter, and any
    `FileOrchestrationJournalError` a row raises today (`file_journal_run_mismatch`,
    `file_journal_authority_transition_requires_typed_api`, …) already aborts
    the import at that row, leaving earlier rows appended. The gate adds one
    more such error and does not change that shape. Ruling: **keep the
    abort-at-row semantics, do not add a prefilter** (0/4309 divergent rows
    measured; a prefilter would silently drop a row the operator should see).
    Regression row: an import whose job list holds a divergent row raises
    `file_journal_job_id_scope_mismatch` at that row; rows imported before it
    are present; the divergent row is neither appended nor written as a direct
    file; re-running the import after correcting the row is idempotent for the
    already-imported rows (`existing is not None` short-circuit at `:2615`).
- Comparison is normalized-to-normalized on both sides.
- Read-side `_validate_pipeline_job_identity` (`:14028`; called from the
  replay at `:6102`/`:6150` and from `:8722`/`:8880`) is **not** changed —
  #1760's rejected alternative (blast radius: replay becomes poison). Note
  `_validate_outgoing_record` reaches `_apply_journal_record` (`:6119`) and
  thus the read-side validator today; the new gate is added *beside* that
  call inside `_validate_outgoing_record`, not inside `_apply_journal_record`.

## Invariant Matrix

```text
Governing invariant: Every judgement the journal makes about a cycle's on-disk state — the
  cache fingerprint that says "unchanged", the directory identity that says "same source
  directory", and the (source, cycle) a file name claims — is made under the same containment
  and identity discipline as the hardened readers and writers, so a cold instance, a warm
  instance, and the write-window owner give one answer for one tree, and no writer can mint a
  file whose name disagrees with its content.
Source-of-truth identity/contract: no-follow containment stat (`stat_no_follow` with
  `containment_root`); `(st_dev, st_ino)` directory identity; `_cycle_scope_from_job_id` as
  the single name→(source, cycle) parser; error tokens `file_journal_unreadable`,
  `file_journal_job_id_scope_mismatch`, `file_journal_source_mismatch`,
  `file_journal_missing_identity`.
Surfaces:
- Producers: every pipeline-job write lane — batch lanes appending at `:3732`, `:3837`,
  `:4072`, `:4263`, `:5139`; single-row `_write_pipeline_job_unlocked` (append `:9060`) and
  `_write_current_master_unlocked` (append `:9106`); `_append_validated_record_unlocked`
  (append `:9320`, no pipeline_job caller today); repair lane
  `_restore_derived_master_direct_unlocked` (`:9157`); historical import
  `append_historical_pipeline_job` (`:2603`) ← `import_historical_scheduler_state`
  (`file_orchestration_migration.py:1240`); the direct-write callers `:3737`,
  `:3842`, `:4077`, `:4279`, `:5166`, `:5168`, `:8923`, `:9074`, `:9120`, `:9213`;
  `_locked_cycle_write` (`:10069`) as the window/cache producer.
- Validators/preflight: `_validate_outgoing_record` (`:9419`, hosts the new D4 gate);
  `_cycle_read_source_segment` source-mismatch check (`:13522`); the containment-aware
  signature helper (D1).
- Storage/cache/query: `_cycle_rows_cache` (`_cycle_rows` hit path `:5699-5732`, append hook
  `_apply_record_to_cycle_rows_cache` `:9662`, wipes `:10072`/`:10089`);
  `_cycle_rows_source_fingerprint` (`:5962`); `_cycle_segment_signatures` (`:10220`).
- Public routes/entrypoints: `list_stage_statuses` → `_list_stage_statuses_for_source` →
  `_cycle_rows`; `query_pipeline_jobs_by_cycle` (`:1822`) / `_by_run` (`:1835`); public
  writers `upsert_pipeline_job` (`:2472`), `reserve_pipeline_job` (`:2619`),
  `reclaim_pipeline_job_reservation` (`:2707`), `commit_pipeline_job_submit_attempt`
  (`:2952`), `transition_pipeline_job_*`, `mark_pipeline_job_permanently_failed` (`:3747`),
  `record_pipeline_job_reconciliation` (`:3845`), `update_pipeline_job_status` (`:5324`),
  `append_historical_pipeline_job` (`:2603`) and, one layer up,
  `import_historical_scheduler_state` (`file_orchestration_migration.py:1240`) —
  signatures unchanged.
- Frontend/downstream consumers: none — scheduler-internal file journal; no display/API
  surface reads it directly.
- Failure paths/rollback/stale state: `_read_cycle_segments` contract frame (`:10231`);
  `_probe_containment_failure`; zero-bytes-written on a rejected write; the restore lane's
  kept-anchor fail-closed; `_project_committed_pipeline_job_write` (`:8897`) never reached by
  a divergent row.
- Evidence/audit/readiness: none — no receipts; local + CI pytest on two filesystem
  semantics is the oracle.
Regression rows:
- warm instance, legal `[]` cached, then `journal/<src>` swapped for symlink → decoy
  -> `list_stage_statuses` returns the `file_journal_unreadable` blocked row, not `[]`
- same tampered tree, cold instance vs warm instance -> identical result
- tamper under `latest/<src>` (the scandir parent) instead -> same fail-loud (sibling stat covered)
- untouched empty directory, warm instance -> legal `[]`, still a cache hit on the second read
- fingerprint that observed a fault -> no entry stored (cache dict inspected)
- owner inside its window, parent swapped for symlink between two owner reads -> second read
  fails loud with `file_journal_unreadable`
- owner inside its window, untouched tree -> hit served without a source-file fingerprint
- cohort X window exits after cohort Y populated its entry inside the window -> Y's entry
  still hits (no disk re-read); X's own prefix (incl. base key) is gone
- entry clear disabled, owner reads a pre-window entry invalidated by another process ->
  stale (documented precondition; the pinned test stays as is)
- case-insensitive volume, `latest/IFS` + `journal/ifs` discovered -> each directory read
  once, no `(st_dev, st_ino)` opened under two spellings
- case-sensitive volume, `journal/gfs` and `journal/GFS` both real -> both read
- overrides `("IFS", "ifs")` on a case-insensitive volume -> one segment; override naming
  another source -> `file_journal_source_mismatch`; overrides empty after dedup ->
  `file_journal_missing_identity`
- symlink alias `journal/ifs -> elsewhere` -> kept as a distinct segment, read fails closed
- public single-row write with `job_id` cycle ≠ row cycle -> `file_journal_job_id_scope_mismatch`,
  no journal record in any segment, no direct file (flat or by-cycle), no
  `committed_projection_fault` event
- public single-row write with `job_id` source ≠ row source -> same
- batch lane (a public writer that appends a record batch) with one divergent row -> same, and
  the sibling records of the batch are not appended either
- repair lane with a divergent canonical row -> `file_journal_job_id_scope_mismatch`, no direct
  file restored, reconcile-inventory anchor still present
- `import_historical_scheduler_state` with one divergent job row -> raises
  `file_journal_job_id_scope_mismatch` at that row; earlier rows imported; the divergent row
  has no journal record and no direct file; a re-run after correcting the row is idempotent
  for the already-imported rows
- write with unparseable `job_id` -> accepted, fall-open unchanged
- every existing writer in the journal, chain, warm-start and scheduler suites -> no writer
  trips the gate (full suites green)
```

## Boundary-surface checklist

- Shared helper roots: `file_orchestration_journal.py` (only source file
  changed); `packages/common/source_identity.py` read, not changed.
- Public entrypoints: the readers and writers listed under "Public
  routes/entrypoints" above — signatures unchanged.
- Read surfaces: `_cycle_rows` hit/miss/store, `_cycle_rows_source_fingerprint`,
  `_cycle_segment_signatures`, `_cycle_read_source_segments`,
  `_merge_cycle_source_discovery` and its discovery-method callers.
- Write/delete/overwrite surfaces: all pipeline-job write lanes (gate in
  `_validate_outgoing_record`, before the first byte); no delete/overwrite
  semantics change.
- Staging/publish/rollback: none.
- Producer/consumer evidence boundaries: error tokens above; no receipts.
- Stale-state/idempotency boundaries: `_cycle_rows_cache` entry/exit wipes,
  owner marker; `_restore_derived_master_direct_unlocked` repair path (anchor
  kept on rejection).
- Unchanged downstream consumers: `_validate_pipeline_job_identity` (read
  side), `_apply_journal_record`, `_direct_jobs_cycle_cache`, #1734 memo, all
  other `_stat_signature` callers (non-goals), the non-window
  `_cycle_rows_cache.clear()` sites `:1153`/`:1167`.

## Review focus

1. D1: is every stat feeding the fingerprint routed through the helper, and is
   a marker-carrying fingerprint provably never stored and never a hit?
2. D1b vs `:10072`: the owner probe must not be mistaken for a reason to touch
   the entry clear.
3. D2: the exit sweep includes the base key; the entry clear is byte-identical.
4. D4: the gate lives in `_validate_outgoing_record` and fires on all eight
   call sites before any append — prove no journal record and no direct file
   after a rejected write on a containment-enabled single-row lane
   (`reserve_pipeline_job` / `reclaim_pipeline_job_reservation`), a batch lane,
   the repair lane and the historical-import lane, not one.
5. D3: symlinked aliases still kept; the re-pinned `("IFS","ifs")` test branches
   on filesystem semantics rather than deleting the assertion.
