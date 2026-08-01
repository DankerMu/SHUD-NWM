# Tasks: fix-source-trust-test-platform-guard

Fixture level: compact
Upstream suggested level: none declared (issue from PR #1126 review side-scan);
compact chosen to mirror the sibling fix `2026-07-25-fix-docker-smoke-tests-tmpdir-hermetic`
(same root cause, same guard, compact). The `path` / `delete` / `file output`
expanded-triggers all match only textually: the change surface is a test-only
skip guard with zero production path/write semantics — recorded here as the
divergence-from-trigger reason.

Change surface:
- `tests/test_two_node_docker_source_trust.py` — one test
  (`test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`,
  `:70`) gains the skipif decorator; file gains `import pytest`. No other file.

Must preserve:
- `scripts/validate_two_node_docker_source_trust.py` byte-identical — the
  evidence-root whitelist (`:26,298`) is a production contract, not a test
  convenience knob (issue explicitly out-of-scopes relaxing it).
- The guarded test's full body and assertions unchanged: role-scoped report
  (`two-node-docker-source-trust-compute.json`), `evidence_run_id` binding,
  absence of the aggregate `two-node-docker-source-trust.json`. Only the
  decorator is added, so node-22/CI run it exactly as before.
- The other 11 tests in the file untouched, including the string-only
  whitelist-message assertion at `:131` (no disk write → no guard).
- Guard wording verbatim-identical to `tests/test_two_node_docker_runtime.py:3758-3761`
  so the two files stay greppable as one guard family.

Must add/change:
- `import pytest` in the import block.
- `@pytest.mark.skipif(not os.access("/scratch/frd_muziyao", os.W_OK),
  reason="requires writable /scratch/frd_muziyao (node-22 host contract)")`
  on the one affected test.

Seams under test:
- None new. The guard reuses the existing host-contract seam (`os.access`
  writability probe) established by #1126.

Risk packs:
- Public API / CLI / script entry: not selected — tests-only, no production
  entrypoint touched.
- File IO / path safety / overwrite: selected — the guarded test writes to and
  deletes a real host path (`:92-97`: `unlink` + `rmdir` × 2 on the evidence
  root and its parent); the guard must ensure non-contract hosts never attempt
  that write/delete, and on contract hosts the write/delete scope stays
  exactly the test's own evidence directory (precedent fixture selected this
  pack for the same Class C guards). Evidence: §2.1 (skip on macOS → no write
  attempted), §2.6 (contract host runs full body unchanged), §2.7 (diff-stat
  proves no write-path code changed).
- Config / project setup: not selected — no config change.
- Concurrency / ordering: not selected — single-process pytest decorator.
- Schema / columns / units / field names: not selected — report payload and
  filenames untouched.
- Backward compatibility / legacy: not selected — node-22/CI verdicts
  provably unchanged (guard is false there).
- Other packs (auth/secrets, resource limits, error handling/rollback,
  release/packaging, documentation/migration): not selected — a 4-line
  test-only decorator touches none of these surfaces.

Non-goals:
- Relaxing the validator's evidence-root whitelist.
- #1106's preflight BLOCKED/FAIL early-exit root cause.
- The two #1126-fixed guards in `tests/test_two_node_docker_runtime.py`.
- String-only `/scratch/frd_muziyao` usages that never write
  (`tests/test_orchestration_chain.py:9218`,
  `tests/test_real_slurm_gateway.py:1974`,
  `tests/test_scheduler_file_provider_refresh.py:2245`, and `:131` in this file).

## 1. Implementation

- [x] 1.1 Add `import pytest` and the verbatim #1126 skipif guard to
  `test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`.

## 2. Verification (evidence mapping)

- [x] 2.1 macOS: `uv run pytest -q tests/test_two_node_docker_source_trust.py`
  → `11 passed, 1 skipped` (0 failed); skip reason names the node-22 host
  contract. (Acceptance criterion, issue #1127.)
- [x] 2.2 Red-first proof: on master the same command shows
  `1 failed, 11 passed` with `[Errno 30] Read-only file system` in the
  failure output.
- [x] 2.3 `uv run ruff check .` clean.
- [x] 2.4 `openspec validate fix-source-trust-test-platform-guard --strict
  --no-interactive` passes.
- [x] 2.5 Guard-parity check: `grep -A3 "skipif" tests/test_two_node_docker_source_trust.py`
  output matches the runtime-file guard byte-for-byte (condition + reason).
- [ ] 2.6 Writable-scratch host: the guarded test still runs with full body —
  evidence is either node-22 `uv run pytest -q
  tests/test_two_node_docker_source_trust.py` → `12 passed`, or the PR's CI
  `unit-test-targeted` run (CI provisions writable `/scratch/frd_muziyao`,
  and `scripts/select_ci_tests.py` selects a changed test file as itself) —
  proving the guard is a skip on non-contract hosts, not a lights-out.
- [x] 2.7 `git diff --stat origin/master` contains only
  `tests/test_two_node_docker_source_trust.py` (plus this openspec change) —
  acceptance criterion 4 and File-IO-pack evidence that no write-path code
  changed.
