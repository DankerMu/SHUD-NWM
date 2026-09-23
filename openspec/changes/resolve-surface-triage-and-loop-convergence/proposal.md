# `.resolve()` surface triage, scheduler root-check loop convergence, census `--output` typing

## Why

Three out-of-scope findings from #1627 (ADR 0009, merged), batch F. All three are the ADR 0009 path-canonicalization family, on the surfaces the ADR's guard does not reach.

- **#2452** — ADR 0009 "已知限制 2": the `.resolve()` surface (≈152 `(module, function)` pairs / ≈223 call sites across `services/ workers/ packages/ apps/`) was never classified against the ADR's three clauses. It also has a failure mode orthogonal to the loop predicate: on the 3.11 production pin (`.python-version`), `Path.resolve()` raises an errno-less `RuntimeError` on a symlink loop in **both** its strict and non-strict forms, while 3.13+ folds (non-strict) or raises `OSError(ELOOP)` (strict). Four `strict=True` sites wrap the call in a handler that catches `OSError` but not `RuntimeError`, so that handler is dead code on the pin: `chain_runtime_utils.py::_python_runtime_export_lines`, `slurm_gateway/real_backend.py::_python_runtime_export_lines`, `file_orchestration_migration.py::_prepare_rollback_retention_root`, `production_closure/slurm_validation.py::_bind_slurm_log_path`. The existing guard's `strict=True` criterion cannot be reused on this surface — there, `strict=True` is exactly the unsafe form.
- **#2453** — `scheduler_runtime_roots.py::_scheduler_root_check` calls `path.resolve(strict=False)` and its `except RuntimeError:` arm returns early with a separately assembled 8-key `check`. Same loop root, same interpreter-independent verdict (`blocked`, 36/36), but different blocker code (`LOCK_ROOT_UNSAFE_PATH` on 3.11 vs `LOCK_ROOT_SYMLINK` on 3.13) and different key set (8 vs 10 keys). The same function's second `.resolve(strict=False)` on `workspace_root` drifts `under_workspace` the same way.
- **#2454** — `journal_scope_census.py::_require_output_outside_root` calls `os.path.realpath` bare. A relative `--output` under a deleted cwd raises `FileNotFoundError`, an embedded NUL raises `ValueError`; neither entrypoint (click / argparse) catches them, so a command documenting "1 on a typed failure" leaks a traceback. The statement immediately above it already fixed the same shape for `expanduser` (#1955).

## What Changes

- **#2452** — a one-shot triage of every `.resolve()` pair (AST census, same method as the issue) against ADR 0009's three clauses, recorded in this change's `design.md` §Triage (not a shipped registry: ADR 0009 §权衡 rejects a per-site registry + two-way pin). Sites that fall outside every clause are dispositioned one by one (changed, or accepted with a written reason); an empty set is stated as "零错位站点". The four dead-handler `strict=True` sites get a handler that also catches `RuntimeError` and maps it onto the same outcome as the `OSError` arm. A new, **independent** mechanical guard for the `.resolve()` surface: the set of **strict** `.resolve()` calls that sit inside a `try` whose handlers catch `OSError` itself but nothing that catches `RuntimeError` is empty. Non-strict calls are excluded (on 3.13+ they fold without raising, so a `RuntimeError` arm would create divergence); they are triage objects. ADR 0009 "已知限制 2" is rewritten to point at the triage and the new guard.
- **#2453** — `_scheduler_root_check` stops using `Path.resolve()` as its canonicaliser at both sites (the `path` and the `workspace_root` anchor); both go through the module's existing strict-then-non-strict `os.path.realpath` helper (house paradigm, ADR 0009 clause 1), and the `except RuntimeError` early-return arm is deleted, so loop and non-loop inputs share one assembly line and the kernel `lstat` decides `SYMLINK` / `UNSAFE_PATH` / `NOT_FOUND`. A 36-cell parametrised test pins code and key-set equality across interpreters.
- **#2454** — both `os.path.realpath` calls in `_require_output_outside_root` are wrapped in `except (OSError, ValueError)` raising a new BEFORE-census typed code `CENSUS_OUTPUT_UNRESOLVABLE` (constant message, no path/traceback, `error_type`/`output` in details), listed in `CENSUS_JOB_ID_SCOPE_HELP` and `_OUTPUT_HELP`. The ADR 0009 clause-2 exemption entry for this function is **not** modified.

## Triage

```text
Issue type: bugfix + refactor(triage) + test
Fixture level: expanded
Upstream suggested level: absent (issues carry no Suggested fixture level)
Blast radius: scheduler preflight evidence (blocker codes / check keys), sbatch export-line generation, rollback-retention and slurm-log binding error paths, census CLI exit contract, ADR 0009 guard.
Selected risk packs: File IO / path safety; Public API / CLI / script entry; Error handling / partial outputs; Schema / field names (evidence check keys); Legacy compatibility; Documentation
Evidence floor: tests/test_path_canonicalization_family_guard.py + new resolve-surface guard; tests/test_production_scheduler.py -k "root_preflight or runtime_roots or allowed_roots"; tests/test_scheduler_journal_scope_census.py; cross-interpreter receipts (3.11 + 3.14); ruff; node-27 real-DB pytest receipt at the PR head.
```

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `safe-filesystem-primitive-contract`: adds the `.resolve()`-surface handler requirement and its guard.
- `runtime-evidence-and-operations`: the scheduler root check attributes a symlink-loop root identically on every supported interpreter.
- `pipeline-job-persistence`: the job-id scope census refuses an unresolvable `--output` with its own typed code before the census runs.

## Impact

- Code: `services/orchestrator/scheduler_runtime_roots.py`, `services/orchestrator/journal_scope_census.py`, the four dead-handler sites (`services/orchestrator/chain_runtime_utils.py`, `services/slurm_gateway/real_backend.py`, `services/orchestrator/file_orchestration_migration.py`, `services/production_closure/slurm_validation.py`), plus whatever the triage finds outside every clause (possibly `services/orchestrator/scheduler_evidence.py`).
- Tests: `tests/test_path_canonicalization_family_guard.py` (or a sibling guard module), `tests/test_production_scheduler.py`, `tests/test_scheduler_journal_scope_census.py`, tests for the four sites.
- Docs: `docs/adr/0009-path-canonicalization-dereference-doctrine.md` 已知限制 2 (and 3's last sentence, now closed by #2454).
- Commit gate: files over 1000 lines that are staged and absent from `.large-file-guard.json` `exclude` must be added there.
- Runtime: no schema/migration. Evidence payload of the loop-root blocker changes on the 3.11 pin (code + keys converge onto today's 3.13 shape). No consumer reads those keys (issue #2453 §影响定价).
