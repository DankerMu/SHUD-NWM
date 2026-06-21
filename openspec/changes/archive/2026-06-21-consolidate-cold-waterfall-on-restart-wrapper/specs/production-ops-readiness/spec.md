## ADDED Requirements

### Requirement: Diagnostic restart paths defer to the canonical wrapper and probe /health

Any repo-committed shell or Python script that restarts the node-27 hand-launched display API uvicorn for diagnostic, measurement, or smoke purposes SHALL defer to `scripts/ops/start-display-api.sh` for the relaunch rather than inlining its own `setsid python -m uvicorn ...` launcher. Diagnostic scripts MAY add additional pre/post measurement logic around the wrapper call, but the act of stopping the prior uvicorn, sourcing `infra/env/display.env`, and relaunching MUST go through the canonical wrapper.

Any repo-committed script that probes the display API health endpoint as part of a wait-loop, sanity check, or measurement table SHALL probe `/health` (root path, registered by `apps/api/main.py:1947` `_register_static_and_health_routes`). Scripts MUST NOT probe `/healthz`, `/api/v1/health`, or other non-existent variants that return 404 on a healthy uvicorn (which would silently degrade health-wait logic and pollute measurement evidence with 404 dispatch overhead).

#### Scenario: Diagnostic script consolidates relaunch on wrapper

- **WHEN** `scripts/diagnostic/display-cold-waterfall.sh` (or any equivalent diagnostic script) needs to relaunch the display API uvicorn between measurement passes
- **THEN** its relaunch function MUST call `bash "${NWM_ROOT}/scripts/ops/start-display-api.sh"` instead of inlining `setsid .venv/bin/python -m uvicorn apps.api.main:app ...`
- **AND** the wrapper's preflight + env-sourcing + graceful SIGTERM + smoke check are inherited automatically (no parallel hand-launch shape)

#### Scenario: Diagnostic health probe uses /health root

- **WHEN** a diagnostic or measurement script waits for the display API to become healthy after a restart
- **THEN** the probe URL is `${BASE}/health` (root)
- **AND** the probe is NOT `${BASE}/healthz` or `${BASE}/api/v1/health` (both 404 on healthy uvicorn — would silently fail the wait-loop or measure dispatch overhead instead of health-check TTFB)
- **AND** any ENDPOINTS array, docstring, output table, or sequence text in the same script that references the health endpoint uses `/health` consistently
