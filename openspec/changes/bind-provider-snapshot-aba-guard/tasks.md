# Tasks

## 1. Implementation

- [x] T1 Rewrite
  `test_provider_snapshot_rejects_replacement_between_metadata_and_read` as the
  ABA scenario: counter-based hook on
  `provider_atomic_module.read_bytes_limited_no_follow`; on call 2 write
  `generation-b`, call the real read, then restore `generation-a` and the
  original `mtime_ns` via `os.utime(..., ns=...)`. **Order matters: the byte
  restore must precede the `os.utime` call.** Reversing them re-stamps
  `mtime_ns` to the current time, so `before != after` fires from the metadata
  disjunct instead, the test still raises `provider_preimage_changed` with call
  count 3, and the coverage gap this change exists to close is silently
  recreated.
- [x] T2 Assertions for T1: `reason == "provider_preimage_changed"`, final
  on-disk bytes are `generation-a` (the ABA restore, inverting the old
  `generation-b` assertion), and observed call count `== 3`.
- [x] T3 Add the disjunct-1 test: different-length replacement on call 2 with
  no restore, asserting `provider_preimage_changed` and the call count.
- [x] T4 In-test comment naming why the `mtime_ns` restore exists (ext4's 4 ms
  tick, design D2), so a later reader does not "simplify" it away.
- [x] T5 `packages/common/provider_atomic.py` untouched: the only non-
  `openspec/` file in the diff is
  `tests/test_scheduler_file_provider_refresh.py` (binding check is E7).

## 2. Verification (Evidence Floor)

- [x] E1 macOS: `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
      green.
- [x] E2 node-27: same command green, in a fresh detached worktree, ≥296 passed
      / 0 failed.
- [x] E3 macOS mutation receipt: delete the content-hash disjunct at
      `provider_atomic.py:142`, the ABA test goes **red**; restore; `git status`
      clean.
- [x] E4 node-27 mutation receipt: same, in the detached worktree; restore;
      `git status` clean.
- [x] E5 `uv run ruff check .` clean.
- [x] E6 `openspec validate bind-provider-snapshot-aba-guard --strict
      --no-interactive` passes.
- [x] E7 `git diff --stat origin/master...HEAD` shows no change under
      `packages/`.

## 3. Evidence receipts

- E1 macOS `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` -> `296 passed in 38.74s`
- E2 node-27 (`/home/nwm/NWM-1717`, detached at `2841e456`) same command -> `296 passed in 47.98s`
  (baseline at master `34940600`: `1 failed, 294 passed in 46.53s`)
- E3 macOS mutation: content-digest disjunct deleted -> ABA test `DID NOT RAISE`
  (`1 failed, 1 passed`); restored; `git status` shows `packages/` clean
- E4 node-27 mutation: same -> `1 failed, 1 passed`; restored; `git status --porcelain` empty;
  re-run after restore -> `2 passed in 0.66s`
- E5 `uv run ruff check .` -> `All checks passed!`
- E6 `openspec validate bind-provider-snapshot-aba-guard --strict --no-interactive` -> valid
- E7 `git diff --stat` -> only `tests/test_scheduler_file_provider_refresh.py` outside `openspec/`
