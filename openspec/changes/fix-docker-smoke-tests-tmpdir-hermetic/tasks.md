# Tasks: fix-docker-smoke-tests-tmpdir-hermetic

Fixture level: compact
Change surface:
- `tests/test_two_node_docker_runtime.py` — 14 tests across three root-cause
  classes (Class A: 11 TMPDIR nodes incl. the vacuously-green :4278; Class B:
  1 canonicalization assert; Class C: 2 node-22 host-contract guards); no other
  file
Must preserve:
- Production `scripts/validate_two_node_docker_runtime.py` byte-identical
- Green TMPDIR-contract tests untouched: :3771 (delenv default derivation),
  :3824 (setenv under artifacts), :3845 / :3873 (delenv run_id / symlink)
- Already-green sibling
  `test_docker_smoke_passes_with_expected_role_boundary_probe_results` (:4341)
  unchanged
- All existing assertions in the fixed tests stay (status, blocker codes,
  payload fields); Class B's `/tmp` assertion is the single allowed assertion
  edit, and only to resolved-path comparison — no oracle weakened
- Class C tests keep their full body and assertions; the only addition is the
  skipif guard, so node-22 runs them exactly as before
Must add/change:
- Class A: `monkeypatch.setenv("TMPDIR", str(tmp_path / "artifacts" / "tmp"))`
  (in-file pattern from :4345) in each of the 11 tests, adding the
  `monkeypatch` fixture parameter where absent
- `test_docker_smoke_required_probe_failure_never_passes`: assert nested
  `preflight/docker-preflight.json` `status == "PASS"` before the FAIL asserts
- :4278 `..._when_preflight_blocks`: assert the nested preflight blocker codes
  contain the docker-unavailable family (oracle no longer dead)
- Class B :3791: assert `payload["tmpdir"] == str(Path("/tmp").resolve())`
  (or equivalent canonicalization-safe comparison)
- Class C: `pytest.mark.skipif` guarding on writable `/scratch/frd_muziyao`
  with a reason string naming the node-22 host contract
Seams under test:
- `run_docker_smoke` / `run_preflight` public kwargs + process `TMPDIR` env
  (existing seams; no new seam introduced)
Risk packs:
- Public API / CLI / script entry: not selected - no production entrypoint or
  signature changes; tests-only
- Config / project setup: not selected - no config change
- File IO / path safety / overwrite: selected - Class C tests write and
  `shutil.rmtree` real host paths under `/scratch/frd_muziyao` when writable;
  the guard must ensure hosts without a writable scratch root never attempt
  the write, and the rmtree scope stays within the test's own evidence dir
- Schema / columns / units / field names: not selected - evidence payload
  schema unchanged
- Auth / permissions / secrets: not selected - not touched
- Concurrency / shared state / ordering: not selected - `monkeypatch.setenv` is
  per-test scoped; `_temporary_tmpdir_env` already restores env
- Resource limits / large input / discovery: not selected - not touched
- Legacy compatibility / examples: selected - full-file verdict must be
  deterministic on macOS and Linux; no existing assertion weakened; node-22
  keeps Class C live
- Error handling / rollback / partial outputs: selected - the BLOCKED-vs-FAIL
  contract must be genuinely exercised: probe-failure tests prove preflight
  PASS; the :4278 blocked-path test proves its blocker really comes from
  docker unavailability, not TMPDIR
- Release / packaging / dependency compatibility: not selected - none
- Documentation / migration notes: not selected - hermetic test fix; issue and
  OpenSpec change are the record
Required evidence:
- `uv run pytest -q tests/test_two_node_docker_runtime.py` on macOS: 0 failed
  (Class C = 2 skipped with the host-contract reason)
- `uv run ruff check .` clean
- node-27 (Linux, no `/scratch/frd_muziyao`): same full-file pytest 0 failed,
  Class C skipped (Phase 8 / merge evidence)
Non-goals:
- No `run_preflight` source seam; no `_docker_smoke_status` change; no #1090
  argv-path edits; no injectable scratch root in
  `ensure_approved_evidence_root`

## 1. Test hermeticity fix

- [x] 1.1 Class A: add TMPDIR normalization to the 11 affected tests (8 smoke
  nodes, 2 preflight tests at :3650/:3676, and the vacuously-green :4278),
  adding the `monkeypatch` parameter where missing. Existing assertions stay
  verbatim.
  Evidence floor: the 10 previously-failing Class A nodes pass on macOS;
  :4278 still passes.
- [x] 1.2 Probe-path proof: in
  `test_docker_smoke_required_probe_failure_never_passes`, load
  `evidence_root / "preflight" / "docker-preflight.json"` and assert
  `payload["status"] == "PASS"`; in :4278, assert the nested preflight
  blocker codes contain the docker-unavailable family.
  Evidence floor: 4/4 parametrizations assert preflight PASS + smoke FAIL
  with expected blocker code; :4278 asserts live docker-unavailable blockers.
- [x] 1.3 Class B: change the :3791 assertion to a canonicalization-safe
  comparison (`str(Path("/tmp").resolve())`), keeping the
  `TMPDIR_OUTSIDE_APPROVED_ROOT` blocked-path assertions intact.
  Evidence floor: test passes on macOS; blocked-path oracle unchanged.
- [x] 1.4 Class C: add `pytest.mark.skipif` (unwritable `/scratch/frd_muziyao`)
  with a node-22 host-contract reason string to the two scratch tests; bodies
  and assertions untouched.
  Evidence floor: both report SKIPPED with the reason on macOS; test source
  shows no body change.
- [x] 1.5 Red-proof: run the affected tests against pre-change source state
  capturing the `BLOCKED != FAIL` / `TMPDIR_OUTSIDE_APPROVED_ROOT` /
  `'/private/tmp' != '/tmp'` / `OSError: Read-only file system` failures, then
  green (or reasoned-skip) after the fix.
  Evidence floor: red output + green output both recorded in the
  implementation report.

## 2. Change-level verification floor

- [x] 2.1 `uv run pytest -q tests/test_two_node_docker_runtime.py` on macOS:
  0 failed, exactly 2 skipped (Class C), no other test regresses.
- [x] 2.2 `uv run ruff check .` clean.
- [x] 2.3 `openspec validate fix-docker-smoke-tests-tmpdir-hermetic --strict
  --no-interactive` PASS.
