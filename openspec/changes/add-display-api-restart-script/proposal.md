## Why

Issue [#597](https://github.com/DankerMu/SHUD-NWM/issues/597)（PR [#596](https://github.com/DankerMu/SHUD-NWM/pull/596) 根因调查时 surfaced 的 "请选择流域" popup follow-up）：node-27 display API uvicorn 当时跑成 hand-launched orphan
（PPID=1，无 systemd / 无 docker / 无 supervisor）。
诊断时 `/proc/<pid>/environ` **缺 `DATABASE_URL`**，
尽管 `infra/env/display.env` 里值正确；runbook 引用的 `/tmp/start_display.sh` 在 host 上不存在；
hand-launch ad-hoc 操作没有 source env file。
这不是 popup 真实根因（真因是 basin_id contract drift，已由 PR #596 修复），但是 real operational hazard：
任何走同一 ad-hoc 路径的未来 restart 都会再次静默丢 env vars，
没有 service-manager 安全网。

本 PR 在 node-27 实测：操作账号**无 sudo**（`sudo: a password is required` under `BatchMode=yes`），
所以 issue #597 option 1 的 systemd unit 安装路径在无 out-of-band sudo 协调情况下不可走。
仓库已有 `infra/systemd/nhms-display-compose.service` 用于 docker-compose 部署模型，
但 node-27 实际跑的 shape 是 hand-launched venv uvicorn（诊断时 PID 2326484），不是 docker。
缺口 closing 需要的是 **repo-committed 的 hand-launched 模型 restart wrapper**，always source `infra/env/display.env`。

## What Changes

- **NEW**: `scripts/ops/start-display-api.sh` — node-27 display-api uvicorn hand-launch 的 single-command idempotent restart。
  Always source `infra/env/display.env`、graceful stop 旧 uvicorn（SIGTERM + timeout + SIGKILL fallback）、
  `setsid` detach 重启、跑 startup smoke check
  （`curl http://127.0.0.1:8080/api/v1/models?limit=1` 断言 `data.items[0].basin_id` 非空）——
  正是 PR #596 修复的 contract regression 类型，无需等用户面破坏即可在 restart 命令输出处报警。
- **MODIFIED**: [docs/runbooks/display-readonly-live-mvt.md:41](docs/runbooks/display-readonly-live-mvt.md#L41)
  替换 dangling `/tmp/start_display.sh` 引用为 `scripts/ops/start-display-api.sh`。
- **MODIFIED**: `scripts/diagnostic/display-cold-waterfall.sh`（如果它内联 `setsid python ...` for restart）
  改 defer 到新脚本；如不存在内联（高概率），PR body 记 "no-op confirmed"。
- **NEW**: node-27 live receipt 落 `docs/runbooks/receipts/issue-597-display-api-restart-script-<DATE>.md`
  捕获：pre-restart pid + env baseline、`bash scripts/ops/start-display-api.sh` 调用、
  post-restart pid + env 校验（DATABASE_URL 存在）、smoke check basin_id 非空结果。

**Out of scope**：issue #597 的 systemd unit 选项（option 1）——node-27 操作账号无 sudo，
拖到 follow-up issue（待 sudo 到位）。docker-compose 部署模型迁移也不在范围内
（需要新 image build pipeline + ops 协调，超出本 change 的 hand-launch fixture 范围）。

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `production-ops-readiness`: ADD requirement *Display API restart is reproducible from a single command*
  ——锁定 operator contract：node-27 hand-launched display uvicorn 的 restart 是单条
  `bash scripts/ops/start-display-api.sh` 调用，source `infra/env/display.env`、
  graceful 替换旧进程、并以 startup smoke check 在用户面破坏之前 surface env-drift contract regression。

## Impact

- **Code**: `scripts/ops/start-display-api.sh`（new, ~120 行 bash, shellcheck-clean）；
  `docs/runbooks/display-readonly-live-mvt.md`（1 段编辑 wrap）。
- **API 契约**: 无变化（纯 ops tooling，无后端/OpenAPI 改动）。
- **OpenAPI**: 无变化。
- **CI**: 路径 scope `scripts/**` + `docs/**` → 触发 Markdown Lint；
  无 backend/frontend job 触发（无 `.py`/`apps/**` 改动）。
  Shellcheck 当前不在 CI 中——本地跑。
- **Receipts**: **REQUIRED node-27 live receipt** per CLAUDE.md（display deploy receipt oracle = node-27）；
  脚本必须在 node-27 实跑、对 real `/api/v1/models` 验证并记录。
