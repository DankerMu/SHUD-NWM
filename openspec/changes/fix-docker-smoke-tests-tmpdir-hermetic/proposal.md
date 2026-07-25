# Fix docker-runtime self-tests: environment-sensitive failures bypass or mask the smoke FAIL contract (#1106)

## Why

`tests/test_two_node_docker_runtime.py` is red on master on macOS:
`uv run pytest -q tests/test_two_node_docker_runtime.py` → **13 failed, 413
passed** (fixture-review verified 2026-07-25), including all 4 parametrizations
of `test_docker_smoke_required_probe_failure_never_passes` — the
anti-regression guarantee that "required probe failure MUST yield FAIL, never a
silent PASS". The 13 reds split into three distinct root-cause classes:

**Class A — ambient TMPDIR sensitivity (11 test nodes).** `run_preflight` calls
`_approved_preflight_tmpdir(repo_root)`
(`scripts/validate_two_node_docker_runtime.py:5286`), which reads the process
`TMPDIR` and requires it under `repo_root/artifacts` or `/scratch/frd_muziyao`.
On macOS the ambient `TMPDIR=/var/folders/...` is outside both →
`TMPDIR_OUTSIDE_APPROVED_ROOT` → preflight BLOCKED → `run_docker_smoke`
early-exits before any probe, so `assert result.status == "FAIL"` receives
`BLOCKED`. On Linux CI `TMPDIR` is typically unset (defaults to
`repo_root/artifacts/tmp`) → passes. Affected:
- 8 smoke test nodes: `test_docker_smoke_records_fail_and_replaces_stale_pass_when_build_fails`,
  `test_docker_smoke_records_blocked_when_build_is_network_blocked`,
  `test_docker_smoke_required_probe_failure_never_passes` (4 params),
  `test_docker_smoke_image_inspect_failure_never_passes`,
  `test_docker_smoke_display_startup_cleanup_failure_never_passes`
- 2 preflight tests failing the same way (verified same fix applies):
  `test_preflight_records_blocked_when_docker_is_unavailable` (:3650),
  `test_preflight_records_blocked_when_space_is_low` (:3676)
- 1 vacuously-green test:
  `test_docker_smoke_records_blocked_and_replaces_stale_pass_when_preflight_blocks`
  (:4278) currently passes **for the wrong reason** — preflight is blocked by
  TMPDIR before `_docker_unavailable_runner` is ever called, so its oracle is
  dead. TMPDIR normalization revives it; the nested preflight blocker set must
  then be asserted to pin the live oracle.

**Class B — macOS path canonicalization (1 test).**
`test_preflight_blocks_explicit_tmpdir_outside_approved_roots` (:3791) sets
`TMPDIR=/tmp` and asserts `payload["tmpdir"] == "/tmp"`; macOS canonicalizes to
`/private/tmp`. The assertion must compare the resolved path, keeping the
blocked-path oracle intact on both platforms.

**Class C — node-22 host-contract tests (2 tests).**
`test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`
(:4383) and
`test_static_report_explicit_evidence_run_id_overrides_scratch_path_inference`
hardcode `evidence_root=/scratch/frd_muziyao/...`; `mkdir` fails with
`OSError: Read-only file system: '/scratch'` on macOS **before preflight** —
TMPDIR cannot fix them, and `/scratch/frd_muziyao` is an inline literal in
`ensure_approved_evidence_root`
(`scripts/validate_two_node_docker_runtime.py:1061`) with no injectable seam.
These tests bind the node-22 scratch layout contract; they get an explicit
platform guard (`pytest.mark.skipif` on `/scratch/frd_muziyao` not being
writable) so they stay live on node-22 and skip honestly elsewhere — this is a
host-contract declaration, not a lamp-off of a hermetic guarantee (contrast the
#1106 备选 it rejects: skipping the probe-failure tests, which stubs make
runnable everywhere).

The issue's suggested fix direction (a new injectable host-probe seam in
`run_preflight`) is unnecessary for Class A: the seam already exists — `TMPDIR`
itself — and the same file already uses it correctly at
`tests/test_two_node_docker_runtime.py:4345`
(`monkeypatch.setenv("TMPDIR", str(tmp_path / "artifacts" / "tmp"))`).

## What Changes

- `tests/test_two_node_docker_runtime.py` only:
  - Class A: apply the in-file TMPDIR normalization pattern to the 11 affected
    tests (adding the `monkeypatch` parameter where absent). In
    `test_docker_smoke_required_probe_failure_never_passes`, additionally
    assert nested `preflight/docker-preflight.json` `status == "PASS"` — the
    negative-path proof #1106 acceptance requires (preflight PASS + probe
    non-zero → FAIL genuinely reached). In the :4278 test, assert the nested
    preflight blocker codes (the docker-unavailable family) so the revived
    oracle is pinned.
  - Class B: compare `payload["tmpdir"]` against the resolved form of `/tmp`.
  - Class C: add a `skipif` guard (writable `/scratch/frd_muziyao`) with an
    explicit reason string naming the node-22 host contract.
- No production source change: `run_preflight`, `_approved_preflight_tmpdir`,
  `ensure_approved_evidence_root`, `_docker_smoke_status`, and
  `run_docker_smoke` are untouched.

## Out of Scope

- `_docker_smoke_status` BLOCKED/FAIL decision logic (issue explicit non-goal;
  root cause is not there).
- #1090 RECORD/EXEC docker argv split — same file, independent contract; this
  change touches no argv recording path.
- Real docker smoke evidence generation on node-22 (hermetic self-test contract
  only; Class C tests still execute there un-skipped).
- A new `run_preflight` host-probe injection seam — rejected as unnecessary;
  recorded here as the deviation from the issue's recommended direction.
- Making `ensure_approved_evidence_root`'s scratch root injectable — would
  change production path-approval semantics for a test convenience.

Design exemption: compact fixture — `design.md` omitted per fixture-level rules
(tests-only change, no runtime behavior).
