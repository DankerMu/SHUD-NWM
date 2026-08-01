# Tasks: fix-classc-smoke-tmpdir-cleanup

Fixture level: compact
Upstream suggested level: none declared (issue from PR #1126 review side-scan,
verifier-CONFIRMED deferred item); compact mirrors the sibling fixes
`2026-07-25-fix-docker-smoke-tests-tmpdir-hermetic` and
`2026-08-01-fix-source-trust-test-platform-guard` (same family, compact). The
`path` / `delete` / `temp` expanded-triggers match only textually: change
surface is a test-only env normalization + cleanup re-ordering with zero
production path/write semantics — recorded as the divergence-from-trigger
reason.

Change surface:
- `tests/test_two_node_docker_runtime.py` — one test
  (`test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`,
  `:4423`): signature gains `monkeypatch: pytest.MonkeyPatch` — rewritten to
  the in-file multi-line signature form (`:4270-4273` pattern) because the
  current single-line `def` is already 113 chars and would exceed ruff's
  `line-length = 120` — body gains the `:4276`-verbatim TMPDIR setenv as first
  statement, smoke-run + asserts wrapped in `try/finally` with the existing
  `shutil.rmtree(evidence_root.parent)` moved into `finally`. No other
  function, no other file.

Must preserve:
- `scripts/validate_two_node_docker_runtime.py` byte-identical — the
  evidence-root whitelist (`ensure_approved_evidence_root:1058`) and the
  `_approved_preflight_tmpdir:5286` fallback are production contracts (issue
  out-of-scopes relaxing them).
- The skipif guard `:4419-4422` byte-identical (#1126 final; #1127 parity
  family depends on the exact wording).
- All three assertions unchanged (status PASS, payload run-id, nested
  preflight run-id); the scratch evidence-root path unchanged (it is the
  node-22 host-contract declaration — NOT rewritten to hermetic `tmp_path`,
  else the scratch-branch path assembly loses its only coverage).
- The other 7 `run_docker_smoke` call sites and their normalization lines
  (`:4276 :4324 :4361 :4381 :4458 :4484 :4506`) untouched.
- Cleanup deletion target unchanged: `evidence_root.parent`
  (`run-smoke-explicit/`), never wider (not `nwm-test/`, not the scratch
  root).

Must add/change:
- `monkeypatch: pytest.MonkeyPatch` parameter.
- First body statement:
  `monkeypatch.setenv("TMPDIR", str(tmp_path / "artifacts" / "tmp"))`
  (verbatim in-file Class A pattern).
- `try:` around the smoke run + asserts; `finally: shutil.rmtree(evidence_root.parent)`.

Seams under test:
- None new. Reuses the in-file TMPDIR-normalization seam (process env via
  monkeypatch, established by #1126 Class A) and the existing host-contract
  seam (skipif on scratch writability).

Risk packs:
- Public API / CLI / script entry: not selected — tests-only.
- File IO / path safety / overwrite: selected — the test writes and recursively
  deletes a real fixed host path on shared node-22; the change must (a) keep
  the deletion scope exactly `run-smoke-explicit/` and (b) extend deletion to
  failure paths without ever running when the guard skipped (skip happens at
  collection, before the body — no `finally` executes). Evidence: §2.5
  failure-path experiment proves cleanup fires and scope stays; §2.7 diff-stat
  proves no production write-path change.
- Config / project setup: not selected — no config change.
- Concurrency / ordering: not selected — pre-existing fixed-path concurrent
  pytest collision is recorded in the issue as out-of-scope observation.
- Schema / columns / units / field names: not selected — evidence payload
  format untouched.
- Backward compatibility / legacy: not selected — CI (TMPDIR unset) verdict
  provably unchanged; macOS still skips.
- Other packs (auth/secrets, resource limits, error handling/rollback,
  release/packaging, documentation/migration): not selected — ~6-line
  test-only change touches none of these surfaces.

Non-goals:
- Relaxing any `scripts/validate_two_node_docker_runtime.py` logic.
- Rewriting the test to hermetic `tmp_path` (kills scratch-branch coverage).
- Touching the skipif guard.
- The sibling Class C static-report test `:3761-3776` (`written_path.unlink()`
  after asserts, no preflight → no TMPDIR sensitivity): issue records it as
  optional same-shape observation, not an acceptance item — left untouched
  here to keep the diff single-function. Same-shape cleanup-after-asserts in
  `tests/test_two_node_docker_source_trust.py:76` (cleanup loop after all
  asserts) is untracked → routed to issue-scribe as a follow-up.
- The fixed-path concurrency collision (recorded observation only).
- `tests/test_two_node_docker_source_trust.py` (#1127, already merged).

## 1. Implementation

- [x] 1.1 Add `monkeypatch` param + verbatim `:4276` TMPDIR setenv as first
  body statement; wrap smoke run + asserts in `try/finally` moving the
  existing `rmtree` into `finally`.

## 2. Verification (evidence mapping)

- [x] 2.1 Normalization coverage proof: grep shows all 8 `run_docker_smoke(`
  call sites in the file have an in-function
  `monkeypatch.setenv("TMPDIR", ...)` (acceptance criterion 1).
- [x] 2.2 macOS: `uv run pytest -q tests/test_two_node_docker_runtime.py` →
  no new failures; the target test still skips with unchanged reason
  (acceptance criterion 4).
- [x] 2.3 `uv run ruff check .` clean.
- [x] 2.4 `openspec validate fix-classc-smoke-tmpdir-cleanup --strict
  --no-interactive` passes.
- [x] 2.5 node-22 (separate throwaway checkout — the production worktree at
  `/scratch/frd_muziyao/NWM` is pinned to the #1164 replay branch with a live
  driver and MUST NOT be switched): `TMPDIR=/tmp uv run pytest -q
  tests/test_two_node_docker_runtime.py::test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`
  → `1 passed` (not skipped). The nested `preflight["status"] == "PASS"` half
  of acceptance criterion 2 is discharged derivationally: the `finally`
  cleanup deletes the evidence tree (including
  `preflight/docker-preflight.json`) before it can be read post-run, and
  `run_docker_smoke` (`scripts/validate_two_node_docker_runtime.py:1252-1272`)
  can only reach `status == "PASS"` through a PASS preflight (non-PASS
  preflight writes `DOCKER_PREFLIGHT_BLOCKED` and early-exits BLOCKED) — so
  `1 passed` entails nested preflight PASS. Optionally capture the payload
  live with a temporary `print` + `pytest -s` inside the same throwaway
  experiment as 2.6 (never committed).
- [x] 2.6 node-22 failure-path cleanup experiment (local edit, never
  committed; SAME throwaway checkout as 2.5 — the production worktree at
  `/scratch/frd_muziyao/NWM` is pinned to the live #1164 replay branch and
  MUST NOT be edited or switched; restore the experiment edit with
  `git checkout -- tests/test_two_node_docker_runtime.py` afterwards): force
  one assert to fail, run, then `ls /scratch/frd_muziyao/nwm-test/` shows no
  `run-smoke-explicit/` residue; record command + ls output in the PR
  (acceptance criterion 3). Do not run concurrently with any other pytest on
  node-22 — 2.5/2.6 share the fixed path
  `/scratch/frd_muziyao/nwm-test/run-smoke-explicit/`.
- [x] 2.7 `git diff --stat origin/master` contains only
  `tests/test_two_node_docker_runtime.py` (plus this openspec change);
  `scripts/validate_two_node_docker_runtime.py` zero diff (acceptance
  criterion 5).
