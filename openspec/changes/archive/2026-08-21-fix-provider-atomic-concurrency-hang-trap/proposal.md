# Make the provider_atomic concurrency test fail instead of hang

## Why

`tests/test_scheduler_file_provider_refresh.py:835-844` starts a writer
thread whose body has no `try/finally`:

```python
def writer() -> None:
    for index in range(40):
        atomic_replace_provider_bytes(destination, new if index % 2 else old, max_bytes=4096)
    finished.set()

thread = threading.Thread(target=writer)
thread.start()
while not finished.is_set():
    observed.append(destination.read_bytes())
```

If `atomic_replace_provider_bytes` raises, `finished.set()` is skipped and
the main thread's `while not finished.is_set()` busy-loop never exits. Any
failure on the writer path therefore surfaces as a **hang**, not a failure.

The cost is measured, not hypothetical. During #1513, on a host with umask
002, this suite did not go red — it did not finish. It burned a 10-minute
local timeout and the pre-fix baseline for the file was abandoned; once the
trigger condition was fixed the same suite completed in 6.7 seconds.
`pyproject.toml` declares no `addopts` and no `pytest-timeout`, so there is
no backstop: locally the whole pytest session hangs until a human sends
`Ctrl-C`, and in CI the job burns its full `timeout-minutes: 35`.

#1513 fixed only the trigger (it pinned `SHARED_PROVIDER_MODE` on the seed
so `provider_destination_access_invalid` stops firing). The trap itself was
left standing as a declared report-don't-fix (archived
`openspec/changes/archive/2026-08-20-fix-permissive-umask-dir-mode/tasks.md`
§6 item 1 — which, notably, proposes exactly the insufficient fix D2 refutes). This change
closes the trap.

## What Changes

- Factor the target test's signalling/waiting logic into one test-local shared
  harness. Borrow only the safe pieces of nearby concurrency tests: capture
  worker exceptions, use a bounded join, and assert errors/liveness before the
  caller's data oracle. The nearby Barrier setups are not wholesale exemplars;
  #1645 tracks their separate stranded-peer risk.
- Set the dedicated completion sentinel from `finally` and bound the spin-loop
  with its own deadline.
- Start the harness-owned worker as a daemon. Python cannot cancel a blocked
  thread; daemon status is the last-resort guarantee that a permanently blocked
  writer cannot strand `threading._shutdown()` after the deadline assertion.
  This is acceptable only because this harness is test-local and the real body
  writes exclusively below its `tmp_path`.
- Add distinct injected raising-worker and blocked-worker tests. Add a bounded
  subprocess proof that the permanently blocked path makes the whole run exit,
  not merely the main test thread assert.
- Preserve the real 40-write atomic-replace oracle byte-for-byte and correct the
  now-false anchor comment at `:827-830`.

## Non-Goals

- No production code changes. `packages/common/provider_atomic.py` and every
  other non-test module are untouched.
- No repo-wide test-infrastructure change (`pytest-timeout`,
  `filterwarnings = error`, global `addopts`). See design D4 — routed to a
  follow-up issue, not ridden in on a size-S fix.
- Not re-litigating #1513's mode-pinning fix; the trigger stays fixed.
