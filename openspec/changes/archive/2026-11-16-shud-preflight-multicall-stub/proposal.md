# shud-preflight-multicall-stub (#3153)

## Why
The master full regression has been red since b2fc70547 ("Unit Tests (full) (2)"). The failing test is
`tests/test_shud_runtime.py::test_shared_preflight_rejects_realpath_stub_symlink`, which symlinks `shud` to the
host's `/bin/true`. The `ubuntu-latest` runner moved to ubuntu-26.04.

**The 26.04 layout was observed, not inferred** (`docker run --platform linux/amd64 ubuntu:26.04`, `26.04.1 LTS`,
2026-11-16):
- `/bin/true -> gnutrue`, so the realpath is `/usr/bin/gnutrue`. GNU true is kept but renamed.
- `false`, `echo` and `cat` resolve to `/usr/lib/cargo/bin/coreutils/<util>` (uutils; the leaf is the util name).
- Running the `shud -> /bin/true` symlink gives:
  - no args: silent;
  - `--version`: `true (GNU coreutils) 9.7`;
  - `-v` and `-h`: silent;
  - **`--help`: `Usage: /tmp/…/shud [ignored command line arguments]`**, which echoes argv[0].

So on 26.04:
- `_is_stub_basename(real)` sees `gnutrue` and lets it through.
- The no-arg probe is silent, `--version` is non-empty with no identity, and `--help` contains the bare `shud` token
  through the argv[0] echo, so the result is `version_signal=present` and `ok=True`.

This matches the CI failure exactly: `checks` has `regular_file`, `executable_bit` and `shared_libraries_checked`, and
`version_signal` is `present`.

The test was never host-independent. The product guard weakens on any host that renames or multicalls its stubs:
Ubuntu 26.04's `gnu*` names, busybox, toybox, and a uutils `coreutils` multicall binary. node-22 runs Ubuntu 24.04 and
is not affected today; it will be after an upgrade.

## What
1. **Test**: the realpath test builds its own stub target under `tmp_path`, so it is host-independent.
2. **Product: the leaf-name guard is the actual rejection.** `_is_stub_basename` also treats a `gnu`-prefixed stub
   name as a stub (`gnutrue`, `gnufalse`, `gnuecho`, `gnucat`), and `STUB_EXECUTABLE_BASENAMES` gains the multicall
   names `coreutils`, `busybox` and `toybox`. A `shud` that resolves to any of these is rejected with
   `SHUD_EXECUTABLE_STUB_REJECTED` before any probe runs. The configured-basename check uses the same predicate.
   No real SHUD binary carries these names.
3. **Product: identity tokens (truthfulness, not the rejection).** Remove the bare `"shud"` from
   `SHUD_IDENTITY_TOKENS`.
   - Without this change, any binary that echoes argv[0] in usage text reports `present`.
   - With it, such output is non-empty and unrecognized, so it reports `inconclusive`, which is still `ok=True` under
     the unchanged never-break-userspace policy. The token change therefore changes no pass/fail outcome on its own;
     item 2 does the blocking.
   - The real node-22 binary prints `Simulator for Hydrologic Unstructured Domains v2.0` on a no-arg run (verified
     2026-11-16, banner line 25), so it stays `present`.

## Non-goals
- Pinning CI `runs-on` to ubuntu-24.04.
- Changing the inconclusive-is-tolerated policy.

## Triage
- Issue type: bug, a host-dependent test plus guard hardening.
- Fixture level: **standard**: a small product change on a production safety guard with never-break-userspace exposure.
- Risk packs: Legacy compatibility (real SHUD banner and real binary name), Test determinism.
- Blast radius: callers `services/orchestrator/scheduler_preflight.py:74`, `workers/shud_runtime/runtime.py:987` and
  `services/production_closure/slurm_validation.py:716`. Each reads only `ok`/`blockers`; `version_signal` has no
  production consumer. The failure to rule out is real SHUD being rejected.

## Evidence Floor
- [x] E0 The ubuntu-26.04 layout observation above is recorded in this proposal. — done above (`docker run --platform linux/amd64 ubuntu:26.04`, 2026-11-16).
- [x] E1 `uv run ruff check --no-cache .` — `All checks passed!`.
- [x] E2 Local selection for the diff, plus `tests/test_shud_runtime.py`, `tests/test_production_scheduler.py`,
  `tests/test_production_slurm_validation.py` and `tests/test_path_canonicalization_family_guard.py`, all green. — the four named files: 2537 passed. Rest of the selector set: 2107 passed, 1 skipped. Red-on-old (only `shud_preflight.py` reverted): the 5 parametrized leaves and the argv0-echo test fail (`6 failed, 1 passed`; the passing one is the rewritten host-independent 1.1 test, which the old leaf `true` guard also rejects).
- [x] E3 The same set passes in a node-27 temporary worktree. — node-27 at 104e9dc50, under umask 0002: 18 files, 4656 passed, 0 leftover `nhms_it_*` DBs.
- [x] E4 node-22 live, read-only, pre-merge, production checkout untouched:
  - `git -C /scratch/frd_muziyao/NWM show origin/<branch>:packages/common/shud_preflight.py > ~/tmp/3153/packages/common/shud_preflight.py`,
    plus `redaction.py` and the package `__init__` files exported the same way;
  - run with `/scratch/frd_muziyao/NWM/.venv/bin/python -I`, `sys.path.insert(0, ~/tmp/3153)`;
  - `check_shud_executable(<real SHUD_EXECUTABLE>)` returns `ok=True` and `version_signal=present`, and the real
    binary's realpath leaf is not a stub name;
  - no `uv sync`, no bare `uv run`. — node-22 live, with the branch copy loaded from `~/tmp/3153` via `.venv/bin/python -I`: `ok=True`, `version_signal=present`, `blockers=[]`, real leaf is not a stub. The production checkout has 0 tracked changes, HEAD unchanged.
- [x] E5 Pre-merge 26.04 proof: the PR "Unit Tests" log shows `Image: ubuntu-26.04` and that
  `tests/test_shud_runtime.py` actually executed (not the `--collect-only` smoke). — PR #3169 'Unit Tests' job 133497396793 ran on `Image: ubuntu-26.04`. The selector picked `tests/test_shud_runtime.py` and executed it for real: `4561 passed, 1 skipped in 781.24s`, not the collect-only smoke.
- [x] E6 After merge, master "Unit Tests (full)" is green on every shard (run URL). — master run 43926300522 (1bd970431): `Unit Tests (full)` shards 1–4 all success. This is the first fully green full regression since b2fc70547. node-22 ff-pulled a910932c8 -> 1bd970431 at a pass boundary (18:12:53Z) with the lease/journal guard modes unchanged and the timer restored.
- [x] E7 `openspec validate shud-preflight-multicall-stub --strict --no-interactive`. — valid.
