# Tasks — shud-preflight-multicall-stub (#3153)

- [x] 1.1 Rewrite `test_shared_preflight_rejects_realpath_stub_symlink`. The target is `tmp_path/"bin"/"true"`
  (`#!/bin/sh\nexit 0`, 0755), with `tmp_path/"shud"` symlinked to it. Assert `SHUD_EXECUTABLE_STUB_REJECTED`.
- [x] 1.2 `_is_stub_basename`: a name equal to `"gnu" + <stub name>` counts as a stub. Add `coreutils`, `busybox` and
  `toybox` to `STUB_EXECUTABLE_BASENAMES`, and update the comment (cite the ubuntu-26.04 `gnutrue` observation).
- [x] 1.3 Drop the bare `"shud"` from `SHUD_IDENTITY_TOKENS`, and update its comment and the test comment at
  `tests/test_shud_runtime.py` ~:6998.
- [x] 1.4 New tests. Each must be red on the old code; record the red run.
  - Parametrize the leaf over `gnutrue`, `gnufalse`, `coreutils`, `busybox` and `toybox`. The target is a tmp script
    with that name that models the 26.04 behaviour faithfully: silent with no args, and `--help` prints
    `Usage: $0 [ignored command line arguments]`. `tmp_path/"shud"` is symlinked to it. Assert `ok is False`, an
    error code of `SHUD_EXECUTABLE_STUB_REJECTED`, and `"version_signal" not in result.checks`. On the old code this
    yields `ok=True`/`present`, so it is red.
  - An argv[0]-echo script named `shud` (not a symlink; `--help` prints `Usage: $0 ...`, everything else silent and
    exit 0) gives `version_signal == "inconclusive"`, `ok is True` and no blockers. On the old code it gives
    `present`, so it is red.
- [x] 1.5 Protection, not new: the existing real-banner tests (`tests/test_shud_runtime.py` ~:6950 and the fixtures
  at ~:4638, plus `test_production_slurm_validation.py:173` and `test_production_scheduler.py:680`) stay `present`
  and green.

See proposal.md Evidence Floor E0–E7.
