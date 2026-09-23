# Provider-mode test hygiene, umask-0002 marker receipt, ACL-mask ruling, journal-root convergence (batch H: #2403 #1632 #1631 #2384)

## Why

- **#2403** — `tests/test_scheduler_backfill.py::test_db_free_unreadable_index_is_not_memoized_and_resolves_once_repaired` pre-creates the provider lock parent with a bare `mkdir(parents=True)` and the destination with a bare `write_text`; under node-27's default `umask 0002` the parent is `0775` and the gate raises `provider_lock_parent_unsafe` (then the destination is `0664` → `provider_destination_access_invalid`). Red on the backend oracle, green elsewhere.
- **#1632** — #1513 measured the provider_atomic reverse-import closure under `umask 0002` only for UNMARKED tests. The `e2e` / `grib` / `integration` marker suites in that closure skip locally and were never measured under `umask 0002`. "Not measured" is not "known green".
- **#1631 (adjudication)** — the provider lock-parent gate (`provider_atomic.py` rejects any `0o022` bit on the lock's direct parent) is structurally incompatible with cross-uid sharing through a POSIX default-ACL mask (the mask IS the group bits); `safe_fs.ensure_directory_no_follow`'s explicit `0o755` clamps an inherited mask. Inert today on two mutable external facts.
- **#2384 (adjudication)** — two independent journal-root authorities: `verify_journal_root_authority` (`FILE_JOURNAL_INVALID_ROOT`) and retention/restore's `_safe_existing_directory(..., field="journal_root")` (`RetentionFailure("journal_root_*")`). Behaviour differs: a `~nosuchuser/...` root raises a bare `RuntimeError` traceback out of both retention and restore entries instead of a typed blocker. The seam-adoption detection grep of the archived matrix row 4 is blind to authority #2.

## What Changes

- **#2403** — the test creates the lock parent via `tests.provider_mode_helpers.make_directory_with_explicit_mode` and the corrupt index via `write_provider_destination`; no gate relaxed.
- **#1632** — node-27 receipt covering the 21 marker-carrying test files of today's closure. The file set comes from a recorded closure script. Each file runs once under an explicit `umask 002`, and the per-file outcomes are recorded. Opt-ins are enumerated per file (design D2); anything left un-run is reported as "not measured, reason". Any provider-mode failure found is fixed in this PR with the same helpers.
- **#1631** — ruling: **deliberate, keep both** (the gate and the `0o755` pin), with the tension's exact scope, the re-verified inert facts, the reopen trigger, the convergence direction to take when triggered, and a re-check receipt. Code: comment-only cites of #1631 at the gate and the pin.
- **#2384** — ruling: **converge**. Retention's and restore's journal-root check delegates the accept/reject decision to `verify_journal_root_authority`; the retention adapter only translates a refusal into the existing `journal_root_*` vocabulary (unchanged for every shape it could already produce). `~nosuchuser/...` becomes the typed blocker `journal_root_not_absolute`. `_safe_existing_directory` / `_safe_archive_root` also stop leaking `RuntimeError` for their other fields. Detection successor recorded.

## Triage

```text
Issue type: test fix (#2403) + receipt (#1632) + ruling (#1631) + bugfix/convergence (#2384)
Fixture level: standard
Upstream suggested level: absent
Risk axes: Error handling (typed blocker instead of traceback); Legacy compatibility (blocker vocabulary is a receipt contract); File IO / permissions (provider gate untouched)
```

## Impact

- `tests/test_scheduler_backfill.py`; `services/orchestrator/scheduler_journal_retention.py`, `services/orchestrator/scheduler_journal_restore.py`; comment-only in `packages/common/provider_atomic.py`, `packages/common/safe_fs.py`; tests in `tests/test_scheduler_journal_retention_archive.py` / `tests/test_scheduler_journal_retention_planning.py` (no new module).
- Spec: `runtime-evidence-and-operations` ADDED requirement for the retention/restore journal-root check.
- No SQL, API, or node-22 runtime change.
