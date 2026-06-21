## Why

Issue [#612](https://github.com/DankerMu/SHUD-NWM/issues/612)（PR [#611](https://github.com/DankerMu/SHUD-NWM/pull/611) Phase 4 cross-review F1 派生 follow-up）：
`scripts/diagnostic/display-cold-waterfall.sh` 当前有两处与 PR #611 同一类的运维卫生缺陷。

### 缺陷 1 — duplicate inline launcher

[scripts/diagnostic/display-cold-waterfall.sh:90-108](scripts/diagnostic/display-cold-waterfall.sh#L90)
`launch_uvicorn()` 内联：

```bash
setsid .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 \
  >"$UVICORN_LOG" 2>&1 </dev/null &
```

这是 PR #611 之外的第二条 hand-launch 路径，
没有 `scripts/ops/start-display-api.sh` 的安全网
（env-keys 断言、basin_id contract smoke check、SIGTERM-then-SIGKILL graceful fallback）。
未来这条路径上的 env-drift 不会被 #611 的合同检查捕获——
两条 hand-launch shape 漂移就成为时间问题。

### 缺陷 2 — `/healthz` 404（与 PR #611 Phase 1 dry-run 同一漏洞类）

[scripts/diagnostic/display-cold-waterfall.sh:143](scripts/diagnostic/display-cold-waterfall.sh#L143)
`wait_for_health()` 与 lines 20/25/143/165/249（ENDPOINTS + 文档 + 报告输出）共 5 处
都引用 `/healthz`，
而真实健康端点是 `/health`（root，
[apps/api/main.py:1947](apps/api/main.py#L1947) `_register_static_and_health_routes`）。
PR [#592](https://github.com/DankerMu/SHUD-NWM/pull/592) 的 cold-waterfall receipt
[receipts/display-bootstrap-decoupling-20260620.md](docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md)
显示 `/healthz` 4ms 跨 3 pass——
这是 404 dispatch overhead，
不是真实 health-check 路径的 TTFB。
基于此 receipt 的 21.8 s → 213 ms cold-warm-up 推论不依赖 `/healthz`
（layers 端点的测量是分开的），
但 receipt 的 `/healthz` 列本身是 misleading。

## What Changes

- **MODIFIED**: `scripts/diagnostic/display-cold-waterfall.sh`
  - `launch_uvicorn()`（line 90-108）body 简化为
    `bash "${NWM_ROOT}/scripts/ops/start-display-api.sh"`——
    把 env source + setsid + smoke check 全部 defer 给 canonical wrapper。
    `restart_uvicorn()` 保留 `--skip-restart` no-op 分支，但 SIGTERM/SIGKILL 子逻辑也归并到 wrapper
    （wrapper 自己做这一段）。
  - 5 处 `/healthz` → `/health`（lines 20, 25, 143, 165, 249）。
    ENDPOINTS 数组里 `/healthz` 改 `/health`，
    `wait_for_health()` 改 `/health`，
    docstring + 输出 markdown 里的 sequence 文本也对应更新。
  - 工具断言（line 76 `REQUIRED_TOOLS+=(pgrep setsid)`）：
    pgrep 仍是 wrapper 依赖（间接），但 cold-waterfall 自己不再直接 pgrep——
    评估后保留断言（wrapper 跑时也需要），不做减法以免改动面变宽。

- **NEW**: `docs/runbooks/receipts/issue-612-cold-waterfall-rerun-2026-06-21.md`
  ——node-27 实跑 refactored 脚本，
  捕获 `/health`（替代 `/healthz`）+ 7 个真实端点的 cold TTFB（3 passes），
  并对照 PR #592 prior receipt 的 21.8 s baseline 验证 layers 端点恢复行为
  （应该≈cold 几百 ms 而非 21.8 s，因为 PR 5/7-7/7 已经 land 了 water-level 删除 + discharge decoupling）。

- **MODIFIED**: `docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md`
  ——加 History note 指向 #612 receipt，
  明确 `/healthz` 4ms 列是 404 dispatch 而非真实 health TTFB；
  layers / runs 等端点的测量值不受影响（这些端点路径正确）。

**Out of scope**：
- 不修改 cold-waterfall 的测量策略（仍 N passes、SIGTERM 之间、ttfb 计算、percentile）——
  只换 endpoint name + 复用 wrapper。
- 不修改 wrapper 本身。
- 不重写 PR #592 receipt 的结论段（21.8 s → 213 ms 推论独立于 /healthz 修复）。

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `production-ops-readiness`: ADD requirement *Diagnostic restart paths defer to the canonical wrapper and probe /health*
  ——把 PR #611 加的 *Display API restart is reproducible from a single command* 的契约扩展到诊断/测量脚本：
  任何在 node-27 上 restart display API uvicorn 的脚本 MUST defer 到 `scripts/ops/start-display-api.sh`，
  且 health probe MUST 用 `/health`（root，FastAPI 真实端点），
  禁止 fork 出第二条 hand-launch 路径或 probe 不存在的 `/healthz`。

## Impact

- **Code**: `scripts/diagnostic/display-cold-waterfall.sh`
  （净 ~30 行 diff：删 launcher 内联 + 改 5 处 endpoint）。
- **API 契约**: 无变化。
- **OpenAPI**: 无变化。
- **CI**: 路径 scope `scripts/**` + `docs/**` → 触发 Markdown Lint；
  shellcheck 不在 CI（本地跑）。
- **Receipts**:
  **REQUIRED node-27 live receipt** per CLAUDE.md（display deploy receipt oracle = node-27）——
  refactored script 必须在 node-27 实跑、对 real endpoints 验证、捕获 TTFB 表格 + raw tsv。
  PR #592 prior receipt 加 History note（非破坏性 annotation）。
