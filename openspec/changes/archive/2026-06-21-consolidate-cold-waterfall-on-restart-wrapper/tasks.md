## 1. Refactor diagnostic script

- [ ] 1.1 `scripts/diagnostic/display-cold-waterfall.sh:90-108` `launch_uvicorn()`:
  替换内联 `setsid .venv/bin/python -m uvicorn ...` block 为单条
  `bash "${NWM_ROOT}/scripts/ops/start-display-api.sh"` 调用；
  保留 env-file-missing 早返回的 guard（让 cold-waterfall 在 wrapper 之前给出脚本特定 error message）。
- [ ] 1.2 `scripts/diagnostic/display-cold-waterfall.sh:110-138` `restart_uvicorn()`:
  既然 wrapper 已经做 SIGTERM-then-SIGKILL，
  把 cold-waterfall 的内联 kill 逻辑（lines 117-133）删除，
  `--skip-restart` no-op 分支保留，
  非 --skip-restart 路径直接 `launch_uvicorn`（即调用 wrapper）；
  保留 launch_uvicorn 非 0 返回时 exit 2 的语义。
- [ ] 1.3 `scripts/diagnostic/display-cold-waterfall.sh:140-150` `wait_for_health()`:
  把 `/healthz` 改为 `/health`（root）；
  注释加一行："/health is registered at root by apps/api/main.py:1947 _register_static_and_health_routes; /healthz 404s on healthy uvicorn"。
- [ ] 1.4 `scripts/diagnostic/display-cold-waterfall.sh:164-172` `ENDPOINTS` 数组:
  `/healthz` → `/health`。
- [ ] 1.5 `scripts/diagnostic/display-cold-waterfall.sh:20,25` docstring + `:249` waterfall-sequence 输出文本:
  全部 `/healthz` → `/health`。
- [ ] 1.6 `scripts/diagnostic/display-cold-waterfall.sh:74-77` 工具检查:
  保留 `pgrep setsid` 在 REQUIRED_TOOLS（wrapper 依赖），
  但 cold-waterfall 自己不再直接 pgrep——评估后保留断言不动以避免改面变宽。

## 2. Local verify

- [ ] 2.1 `shellcheck scripts/diagnostic/display-cold-waterfall.sh` → 0 warnings
- [ ] 2.2 `bash -n scripts/diagnostic/display-cold-waterfall.sh` → syntax clean
- [ ] 2.3 `openspec validate consolidate-cold-waterfall-on-restart-wrapper --strict --no-interactive` → PASS
- [ ] 2.4 `markdownlint` on touched docs + new receipt → 0 errors

## 3. node-27 live receipt (REQUIRED — measurement evidence)

- [ ] 3.1 ssh node-27 + `cd /home/nwm/NWM && git pull --ff-only` to land refactored script
- [ ] 3.2 `bash scripts/diagnostic/display-cold-waterfall.sh --runs 3` 跑完一轮 cold passes；
  捕获 stdout markdown 表 + raw tsv 路径
- [ ] 3.3 验证：
  - 7 个 endpoint 都返回非 0 ms（`/health` 不再 404）
  - layers 端点 ≈ post-PR-5/7 baseline（hundreds of ms 而非 21.8 s）
  - script exit 0
- [ ] 3.4 写 `docs/runbooks/receipts/issue-612-cold-waterfall-rerun-2026-06-21.md`
  包含：HEAD SHA + 实跑 markdown 表 + raw tsv 路径 +
  对照 PR #592 prior receipt 的 layers 数值 + 结论"defer to wrapper + /health 修复 verified"
- [ ] 3.5 `docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md`
  顶部加 History note：
  "**History note (2026-06-21)**: 表中 `/healthz` 列是 404 dispatch overhead
  （真实端点 `/health` root，见 [apps/api/main.py:1947](../../../apps/api/main.py#L1947)），
  非真实 health-check TTFB；
  issue [#612](https://github.com/DankerMu/SHUD-NWM/issues/612) 已修复脚本端点漂移，
  见 [issue-612-cold-waterfall-rerun-2026-06-21.md](issue-612-cold-waterfall-rerun-2026-06-21.md)。
  其他 endpoint（layers、runs、basins 等）路径正确，测量值不受影响。"

## 4. PR / merge hygiene

- [ ] 4.1 PR body `Closes #612`, Chinese 工作总结
- [ ] 4.2 review-loop log append
- [ ] 4.3 OpenSpec archive after merge:
  `openspec archive consolidate-cold-waterfall-on-restart-wrapper --yes`
