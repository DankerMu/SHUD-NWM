# journal-walk-dirfd-scan (#3138)

## Why

A full-pass py-spy profile on node-22 (pass `scheduler_2026111307_2f1fe5677458`, cycle 2026110200, 143 min) puts
49.5% of all CPU-active self time in `packages/common/safe_fs.py:825 _open_child_dir`. The caller is
`services/orchestrator/file_journal_paths.py` `_iter_discovered_files.walk`, which calls
`stat_no_follow(entry, containment_root=root)` for every directory entry. Each such call re-opens the entire
ancestor chain from `/` (the journal root's own ~7 components plus the levels below it), one NFS round trip per
component, so scanning the flat `pipeline-jobs/` directory (4k+ files) costs ~N×(D+k) directory opens. The
pre-copyback provenance publication, which is on the pass's critical path (qc aggregated → provenance published:
12.8 min IFS / 15.7 min gfs), spends 9.6 of its 10.0 active minutes in this walk; stage submit/wait spends 9.8 of 11.4;
`_discover_cycles` spends 3.6 of 9.2.

Owner request 2026-11-13: B2 (planning/journal replay cost), ship fast under standing authorization.

## What

- `packages/common/safe_fs.py` gains a directory-scan primitive that opens the target directory ONCE through the
  existing no-follow descriptor chain, enumerates names on that descriptor, and `lstat`s each name relative to the
  held descriptor (`os.stat(name, dir_fd=fd, follow_symlinks=False)`), bounded by `max_entries` plus one sentinel
  exactly like `list_directory_no_follow_limited`. `stat_no_follow` itself is unchanged.
- Non-strict `_iter_discovered_files` walks (every hot caller) use the primitive instead of one `stat_no_follow` per
  entry; error codes, evidence, disappearance handling, segment validation, limits and ordering are unchanged. Strict
  walks (reconcile authority/migration) keep the per-entry path unchanged.
- Deterministic proof: for N entries under a root of depth D, the number of `os.open` calls is bounded by the number
  of directories visited (independent of N), versus ~N×(D+k) before.

Non-goals: cheaper validation signatures (directory mtime has different semantics on NFS); memo invalidation policy;
the planning-stage `candidate_state|cycle_rows` replay (387 MB non-cached; separate issue).

Fixture level: expanded (security primitive in the shared safe filesystem layer; journal scan feeds reconcile
quiescence/inventory authority decisions).
Blast radius: every journal directory scan (scheduler planning, cohort submit/poll, provenance, gateway reconcile
inventory/quiescence). A wrong error mapping would turn an unsafe entry into a silent skip or vice versa; a lost
containment check would follow a symlinked subtree.
Selected risk packs: Security boundary (no-follow / containment), Persisted/shared state (journal inventory
authority), Error handling (error taxonomy preserved), Concurrency (entries disappearing/replaced during a scan).
Evidence floor: `uv run pytest -q tests/test_safe_fs*.py tests/test_file_journal_*.py
tests/test_file_orchestration_journal*.py tests/test_gateway_reconcile*.py tests/test_select_ci_tests.py` (node-27 + local); `uv run ruff check .`;
`openspec validate journal-walk-dirfd-scan --strict --no-interactive`; post-merge node-22 pass receipt (qc →
provenance, convert `dispatch_ms`, pass entry → convert start, pass wall vs 2026110200).
