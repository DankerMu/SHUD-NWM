## 1. Restart script

- [ ] 1.1 Create `scripts/ops/start-display-api.sh` with the following contract:
  - Bash strict mode (`set -euo pipefail`), shellcheck-clean
  - Resolve repo root via `git rev-parse --show-toplevel` fallback to script-relative path (so it works from any cwd on node-27)
  - Source `${REPO_ROOT}/infra/env/display.env` using `set -a; . ...; set +a` (export all keys); abort with non-zero exit + clear error if file missing
  - Guard required env keys: assert `DATABASE_URL` and `NHMS_ENABLE_LIVE_POSTGIS_MVT` present, else fail with explicit list of missing vars (do NOT leak values)
  - Identify prior uvicorn via `pgrep -f '\.venv/bin/python -m uvicorn apps\.api\.main:app'` (exact pattern from issue evidence); if found, SIGTERM, wait up to 10s, SIGKILL fallback
  - Relaunch via `setsid` + `nohup` so process survives SSH disconnect (PPID becomes 1 intentionally — that part of the orphan shape is correct; the bug was env, not detachment); stdout/stderr → `/tmp/display-api.log` (or `${NHMS_DISPLAY_LOG_PATH:-/tmp/display-api.log}`)
  - Wait up to 20s for port 8080 to bind (`curl -fsS http://127.0.0.1:8080/api/v1/health` retry loop with 1s sleep)
  - Smoke check: `curl -fsS 'http://127.0.0.1:8080/api/v1/models?limit=1' | jq -e '.data.items[0].basin_id != null'` — non-zero exit on null/missing/parse error; print pid + basin_id sample on success
- [ ] 1.2 Make script executable (`chmod +x scripts/ops/start-display-api.sh`); add file mode line in proposal evidence

## 2. Runbook update

- [ ] 2.1 [docs/runbooks/display-readonly-live-mvt.md:41](docs/runbooks/display-readonly-live-mvt.md#L41): replace the parenthetical `/tmp/start_display.sh` reference with `scripts/ops/start-display-api.sh`; include the new script's contract one-liner (sources display.env + smoke-checks basin_id)
- [ ] 2.2 (defer) Check `scripts/diagnostic/display-cold-waterfall.sh` for inlined `setsid python ...` patterns: **VERIFIED PRESENT** at line 103 inside `launch_uvicorn()`; also `/healthz` 404 bug at lines 20/25/143/165 (real defect — `/healthz` does not exist; only `/health` does per `apps/api/main.py:1947`). Refactor + fix tracked in follow-up [#612](https://github.com/DankerMu/SHUD-NWM/issues/612) to preserve PR #611's operator-restart-wrapper single-responsibility scope.

## 3. Local verify

- [ ] 3.1 `shellcheck scripts/ops/start-display-api.sh` → 0 warnings (install if missing via `brew install shellcheck`)
- [ ] 3.2 `bash -n scripts/ops/start-display-api.sh` → syntax-clean
- [ ] 3.3 `openspec validate add-display-api-restart-script --strict --no-interactive` → PASS

## 4. node-27 live receipt (REQUIRED — display deploy oracle)

- [ ] 4.1 ssh node-27 + `cd /home/nwm/NWM && git pull --ff-only` to land the new script
- [ ] 4.2 Capture baseline: current uvicorn pid + `/proc/<pid>/environ | tr '\0' '\n' | grep -E 'DATABASE_URL|NHMS_' | sort` (redact secret value of DATABASE_URL to scheme+host+db only)
- [ ] 4.3 Run `bash scripts/ops/start-display-api.sh` and capture stdout + exit code
- [ ] 4.4 Post-restart: new uvicorn pid + `/proc/<new-pid>/environ` confirms DATABASE_URL present + same env shape; smoke check exit 0 with basin_id non-null sample
- [ ] 4.5 Write receipt at `docs/runbooks/receipts/issue-597-display-api-restart-script-2026-06-21.md` with all evidence; include "Operator can reproduce restart from single command" statement
- [ ] 4.6 markdown-lint receipt clean

## 5. PR / merge hygiene

- [ ] 5.1 PR body `Closes #597`, Chinese 工作总结 with receipt link
- [ ] 5.2 Append review-loop log entry
- [ ] 5.3 OpenSpec archive after merge: `openspec archive add-display-api-restart-script --yes`
