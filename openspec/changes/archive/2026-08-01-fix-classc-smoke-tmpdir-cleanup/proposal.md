# Fix Class C smoke test: missing TMPDIR normalization + cleanup after asserts (#1128)

## Why

`tests/test_two_node_docker_runtime.py:4423`
(`test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`)
is the last `run_docker_smoke` call site in the file (1 of 8, `:4426`) whose
verdict still depends on the process ambient `TMPDIR`: `run_docker_smoke` runs
a nested `run_preflight`, which hands ambient `TMPDIR` to
`ensure_approved_evidence_root` and returns `TMPDIR_OUTSIDE_APPROVED_ROOT` →
preflight BLOCKED → smoke BLOCKED whenever `TMPDIR` sits outside
`repo_root/artifacts/**` or `/scratch/frd_muziyao/**`. Since the #1126 skipif
guard pins this test to node-22/CI, an ambient `TMPDIR=/tmp` (Slurm
allocation, exported shell) there produces a misleading `BLOCKED != PASS`
failure while the scratch-layout + nested-preflight run-id binding contract
goes unverified — the sole remaining gap in #1126's cross-platform
determinism spec (verifier-CONFIRMED, deferred out of #1126 by its fixture
boundary "Class C tests keep their full body").

Second defect: `shutil.rmtree(evidence_root.parent)` (`:4440`) sits after the
three asserts, so any assertion failure leaves stale BLOCKED evidence under
the real `/scratch/frd_muziyao/nwm-test/run-smoke-explicit/` on a shared
node, polluting later manual inspection.

## What Changes

- Add `monkeypatch: pytest.MonkeyPatch` to the test signature and open the
  body with the Class A normalization line, verbatim from `:4276`:
  `monkeypatch.setenv("TMPDIR", str(tmp_path / "artifacts" / "tmp"))` —
  bringing the file to 8/8 `run_docker_smoke` call sites normalized.
- Wrap the test body's smoke run + asserts in `try/finally` with
  `shutil.rmtree(evidence_root.parent)` in the `finally`, so cleanup runs on
  assertion failure too. Deletion target and semantics unchanged (same path,
  no `ignore_errors`).
- Nothing else: assertions, scratch evidence-root path, and the skipif guard
  stay byte-identical; `scripts/validate_two_node_docker_runtime.py` stays
  untouched (whitelist/fallback must not be relaxed for test convenience).

## Impact

- Affected specs: `two-node-docker-runtime` — extends the determinism
  requirement to cover host-contract call sites' TMPDIR independence and
  failure-path cleanup; amends the frozen "running unmodified" wording that
  would otherwise contradict this fix.
- Affected code: `tests/test_two_node_docker_runtime.py`, one test function
  (~6 lines + indentation). No production code.
- macOS: still skipped, reason unchanged. CI: TMPDIR unset → behavior
  unchanged (fallback already approved); with the fix the verdict no longer
  depends on that luck. node-22: survives ambient `TMPDIR=/tmp`.

## Design exemption

Fixture level compact: `design.md` exempt — prescribed ~6-line test-only
change, in-file precedent pattern (7 sibling call sites), zero production
surface.
