## 1. Fixture

- [x] 1.1 Add expanded OpenSpec proposal/design/tasks/spec delta for #626.
- [x] 1.2 Validate the fixture with `openspec validate align-display-api-port-env --strict --no-interactive`.

## 2. Port Env Alignment

- [x] 2.1 Update `scripts/ops/start-display-api.sh` to source `display.env`
  before deriving the uvicorn port, then read `NHMS_DISPLAY_API_PORT`.
- [x] 2.2 Update display env/compose defaults to `8080`.
- [x] 2.3 Update docs/tests that assert display API port variable/defaults.
- [x] 2.4 Confirm `NHMS_DISPLAY_PORT` no longer appears as an active config variable.

## 3. Verification

- [x] 3.1 `bash -n scripts/ops/start-display-api.sh` PASS.
- [x] 3.2 Shell harness test proves `NHMS_DISPLAY_API_PORT` from sourced
  `infra/env/display.env` controls the uvicorn `--port`, while
  `NHMS_DISPLAY_PORT` is ignored.
- [x] 3.3 Shell harness test proves missing required keys or invalid
  `OBJECT_STORE_ROOT` exits before stopping or relaunching uvicorn.
- [x] 3.3A Shell harness tests prove invalid `NHMS_DISPLAY_API_PORT` values
  exit before stopping, relaunching, or probing uvicorn.
- [x] 3.4 `uv run pytest -q tests/test_two_node_docker_runtime.py` PASS.
  - Local macOS run failed in unrelated Docker preflight `/scratch` and `TMPDIR`
    assumptions; focused display-port subset passed.
  - node-22 `/scratch` oracle: `uv run --no-sync pytest -q tests/test_two_node_docker_runtime.py`
    PASS, 381 passed.
- [x] 3.5 `uv run ruff check .` PASS.
- [x] 3.6 `openspec validate align-display-api-port-env --strict --no-interactive` PASS.
