## Risk packs

- Error handling: the fd cleanup never masks or changes the fail-closed error (code and message unchanged); the gateway proof maps never-running to BLOCKED through the existing catch.
- Resource limits: no fd survives a post-open rejection.
- Test oracle: the byte and row legs prove saturation, and a forced timeout fails them.
- Legacy compatibility: the error codes and messages, the no-follow/containment semantics, the receipt schema and the `wall_time` leg are unchanged.

## Must-preserve

- `_open_runtime_prefix_dir` success still returns a live fd; the callers (`object_store_validation_runtime.py:470-492`, `:833-837`) close it in their existing `finally`, with no double close.
- The two owner copies stay byte-identical.
- `services/orchestrator/reconcile.py` is unchanged, and so is the `wall_time` leg.
- The existing gateway-proof tests stay green: `test_all_three_stages_pass_produces_valid_live_proof_receipt` and `test_terminal_and_cancel_are_two_independent_stages`.
- No skip, xfail or retry is added.

## 1. #1922 fd ownership

- [ ] 1.1 Both owners use the D1 shape, and their bodies are byte-identical.
- [ ] 1.2 Tests, for each owner:
  - A real `stat_no_follow()` raises `SafeFilesystemError` after the open: the owner opens the repo root and uses a `containment_root` that does not contain it. The existing `ProductionObjectStoreValidationError` code and message are raised, and the captured fd is closed (`os.fstat(fd)` → `EBADF`). Capture the fd by wrapping `os.open`.
  - The forced not-a-directory branch leaves the fd closed. The test asserts the exact message `Runtime staging prefix is not a directory: <path>`, which proves the helper's own `S_ISDIR` branch ran.
  - A post-open `OSError` from the helper's first `os.fstat` leaves the fd closed. The test asserts the `Failed to open runtime staging prefix directory` message with an `OSError` `__cause__`.
  - The success path returns an fd that is still live, and the caller closes it.
  - A test asserts that the two function bodies are identical (`inspect.getsource`).

  Test rules (all apply to every case above):
  - Fault injection targets only the helper's fd, never a blanket `os.fstat`. `stat_no_follow` calls `os.fstat` and `os.open` internally (`packages/common/safe_fs.py:823-896`), so a blanket patch reaches the wrong branch. Either substitute or raise only for the fd opened on `path` with `RUNTIME_DIR_FLAGS`, on the helper's own first `fstat`, or patch the owner-module name `stat_no_follow`.
  - Capture the fd for `path` opened with `RUNTIME_DIR_FLAGS` (or assert that every captured fd is closed), because `stat_no_follow` opens parent fds too.
  - Wrap `os.close` and assert the captured fd was closed exactly once (the spec says "exactly once").
  - The `EBADF` check uses the saved real `os.fstat`.
  - Every `os.*` patch is scoped with `monkeypatch.context()`, per `pyproject.toml:88-96`: `tmp_path` teardown walks the filesystem through `os.*`.
  - Import each owner module directly. The facade re-exports only the runtime copy.
- [ ] 1.3 Red proof: with the old body, the new cleanup tests fail. Record this.

## 2. #2478 saturation oracle

- [ ] 2.1 Byte and row legs:
  - a 30 s safety-net deadline;
  - `caplog` asserts `exceeded bounded output (bytes|rows)` and no `timed out`, placed **before** the marker assertion (right after the `action == "query_unavailable"` check), so that a timeout fails on the reason and not on the missing marker;
  - the marker, durable-write, cohort and reap assertions are kept.
- [ ] 2.2 Red proof: with a forced timeout (the fake sleeps before output, `COMMENT_SACCT_TIMEOUT_SECONDS` small for the probe only), the byte and row legs fail on the reason assertion. Record this, then restore.
- [ ] 2.3 Load run (issue Verification): with about 2× cores of `yes > /dev/null` plus parallel workers and an isolated `TMPDIR`, `[byte]` and `[row]` each pass ≥100 consecutive times, driven by a shell loop (there is no repeat plugin). Then the whole file passes.

  **Stop rule:** if a load run shows the signature `saturated` warning present, marker missing, `returncode == -9`, stop and report it. That is the `_terminate_and_reap` 1 s SIGTERM grace expiring under starvation. `reconcile.py` is out of scope, and accepting `rc < 0`, retries or a weaker marker check are all forbidden, so the orchestrator escalates instead.

## 3. #2476 cancel-while-active

- [ ] 3.1 The D3 check comes after a successful cleanup DELETE, and the blocker includes the last status. The docstring says RUNNING is required.
- [ ] 3.2 `_FakeClient` gets a never-running mode. The test asserts:
  - the submit_cancel stage is `BLOCKED` and the top level is `BLOCKED`;
  - `live_proof_accepted is False`;
  - `dependency_blocker` contains `'pending'`;
  - a DELETE was issued;
  - the long-job GET count equals `CANCEL_WAIT_MAX_ATTEMPTS`;
  - `validate_receipt(receipt)` accepts the receipt.

  The existing PASS tests stay green.
- [ ] 3.3 Red proof: the new test fails against the old code.

## 4. Verification (local)

- [ ] 4.1 `uv run pytest -q tests/test_production_object_store_validation.py tests/test_object_store_validation_facade_contract.py tests/test_gateway_reconcile_comment_sacct_bounds.py tests/test_m24_gateway_proof.py` plus any new test file.
- [ ] 4.2 `uv run ruff check .`
- [ ] 4.3 `openspec validate batch-r-fd-ownership-saturation-oracle-cancel-active --strict --no-interactive`

## Evidence Floor

1. #1922: both owners close the fd on every post-open rejection; the error contract is unchanged; the success fd is live; red proof.
2. #2478: the saturation reason is asserted before the marker; a forced timeout fails the legs on the reason (red proof); each leg passes ≥100 consecutive times under load; then the whole file passes.
3. #2476: a never-running job gives BLOCKED with `live_proof_accepted=false` and the cleanup DELETE issued; the docstring requires RUNNING; red proof; the existing PASS tests are green.
4. The pytest files in §4.1 and ruff pass locally.
