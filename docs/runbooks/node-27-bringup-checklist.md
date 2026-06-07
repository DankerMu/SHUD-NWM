# node-27（display_readonly）上线清单

> 来源：M22 tasks↔代码对账（2026-06-06）。**结论：27 节点代码功能 ~95% 已落地（角色边界、retry/cancel fail-closed、artifact reader、strict identity、readonly 探测、前端 gating 全在），不是从零开发，而是「补尾巴 + live 化」。**
> 本清单 = 待办的全部工作，分三批：A 已完成（回填）、B 测试尾巴（本地可做）、C live 证据（需 node-27 实机 / 真实只读 DB / 浏览器）。
> 对账明细见 `openspec/changes/m22-two-node-docker-readonly-display/tasks.md`；角色边界设计见 `docs/runbooks/two-node-deployment-overview.md`。

## 开发流程衔接（2026-06-07）

- **验证 oracle 路由**：后端代码/DB pytest 在 **node-22**；display API / 前端生产化 / 只读边界（本清单 C1–C4）在 **node-27**——node-22 的 pytest **不**闭合 C1–C4。详见 `CLAUDE.md`「验证 oracle 路由」+ `dual-end-issue-workflow` skill。
- **27 前端生产化的功能性开发走 m25 change**：`openspec/changes/m25-multibasin-frontend-production/`（多流域选择器、latest-product 去硬编码/basin_id、洪水重现期独立 `return_period_status`、/ops·/monitoring display 降级）；并行起点 issue #310/#311/#313/#317。本清单聚焦"上线 live receipt"，m25 聚焦"功能交付"，二者互补。
- **CI**：纯前端/docs 子 PR 按路径 scope 跳过后端 pytest；迭代标 **draft**（定向快速通道）、合并前转 **ready**（全量门）。约定见 `CLAUDE.md`「CI 范围与门控」。

## 拓扑回顾

| 节点 | 角色 | 能力 |
|---|---|---|
| node-22 | `compute_control` | 调度/Slurm/SHUD/发布/retry-cancel（已业务化） |
| node-27 | `display_readonly` | 只读消费 DB + published artifacts，`/hydro-met`+`/ops`；无 Slurm/Docker socket/控制面写 |

published 路径：22 写 `/ghdc/data/nwm/published`，27 只读 `/home/ghdc/nwm/published`。DB：27 用只读账号（如 `nhms_display_ro`）。

---

## A. 已完成（代码 + 单测，已回填 tasks.md）

无需再做，仅作上线前 self-check 的可信基线：

- 角色边界与启动校验：`apps/api/runtime_mode.py`（4 角色、production-like predicate、display unsafe-config blockers）
- Slurm 路由按角色不挂载：`apps/api/main.py:310`；`GET /api/v1/runtime/config` capability flags：`main.py:283`
- retry/cancel fail-closed `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`、queue-depth `503 CONTROL_PLANE_QUEUE_UNAVAILABLE`：`apps/api/routes/pipeline.py`
- artifact log reader（`published://`/穿越/脱敏/tail）：`services/artifacts/reader.py`；compute 侧发 `published://logs/...`：`chain.py:4143`
- latest-product / ops strict identity（拒 historical fallback、`PIPELINE_STRICT_IDENTITY_MISMATCH`）：`routes/forecast.py`、`routes/pipeline.py`、`forecast_store.py`
- readonly DB 探测框架（sim/mock 跑通 + 防 mock 冒充 PASS）：`services/production_closure/readonly_db_validation.py`
- 前端 readonly gating（隐藏控件、no control POST、strict 上下文、诊断复制、本地 notified 态）：`apps/frontend` monitoring + hydroMet

---

## B. 测试尾巴（本地可做，功能已实现仅缺自动化）

> 这三项不阻塞上线，是契约/测试完备性硬化。已派 subagent 实现中。

| 项 | 内容 | 落点 |
|---|---|---|
| 2.7 | display retry/cancel `409` + queue `503` 的 OpenAPI 契约 + drift 测试 | `openapi/nhms.v1.yaml`、`main.py:715-733`、`tests/test_api_contract.py` |
| 2.8 | retry/cancel 的 gateway-spy + 401/403/409 RBAC 矩阵 + no-write DB 断言 | `tests/test_retry_cancel_consistency.py` |
| 3.6 | `JOB_LOG_*` 四个错误码进 OpenAPI + drift 测试 | `openapi/nhms.v1.yaml`、`tests/test_pipeline_logs_artifacts.py` |

验证：`uv run ruff check . && uv run pytest -q tests/test_api_contract.py tests/test_retry_cancel_consistency.py tests/test_pipeline_logs_artifacts.py`。改 OpenAPI 后需 `cd apps/frontend && corepack pnpm run check:api-types`。

---

## C. live 证据（必须在 node-27 实机产出，是「上线」的实质）

代码 + 单测都在，缺的是真实环境 receipt。这是 27 节点开发的核心交付。

### C1. 部署 receipt（开发期本地起服务，非 docker compose up）

- [ ] **开发期：27 本地起 display API**（不 `docker compose up`）：`set -a; source infra/env/display.env; set +a` 后 `uv run python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000`（与容器同一 `apps.api.main:app` 入口和角色守卫，env 含 `NHMS_SERVICE_ROLE=display_readonly`）。快、无镜像构建、无对外容器；用后 `kill` PID。
- [ ] 证明 27 无 Slurm CLI/config/socket、无 Docker socket、无禁止 mount/env、`/api/v1/slurm/*` 404、published 只读、`GET /api/v1/runtime/config` 返回 `display_readonly`：`uv run python scripts/validate_two_node_docker_runtime.py static`（**静态校验 compose/env 而不拉起**，对应 §10.1）+ 对本地服务实机探测（`/health`、`/runtime/config`、`/slurm/health`→404）。
- [ ] **生产部署（非开发期，human-gated）**：`docker compose --env-file infra/env/display.env -f infra/compose.display.yml up -d` 起持久对外容器——难回滚 + 改状态，须显式人工确认/预授权（与 merge 同治理）；`smoke`（镜像构建）归此阶段。

### C2. 只读 DB denied-write receipt（tasks 5.1/5.2/5.4/5.8）

- [ ] 用 27 真实只读账号设 `NHMS_DISPLAY_READONLY_DATABASE_URL`（或 `NHMS_READONLY_DB_VALIDATION_DATABASE_URL`），跑 readonly DB validation 入口，产出脱敏 evidence：
  - display API（health/models/stations/latest-product/pipeline status·stages·jobs·logs/runtime config）在只读凭证下 PASS，identity-bound 路由用一个 strict `source/cycle_time/run_id/model_id`、logs 绑 `job_id`。
  - permission-denied 矩阵：`hydro/met/ops` 关键表的 INSERT/UPDATE/DELETE/DDL/TRUNCATE/sequence/schema CREATE 全被拒，记录 `current_user` + DB role 类型。
  - 缺真实 DB 时入口必须报 `BLOCKED`，不得 mock 冒充 PASS。

### C3. cross-plane identity live（tasks 4.3 + §10.2/10.3）

- [ ] 同一个 `run_id/source/cycle_time/model_id/basin_id` 串起：22 生产 → DB 状态 → published logs → `/api/v1/mvp/qhh/latest-product` → 27 `/hydro-met` + `/ops`，**拒 historical latest 冒充**。
- [ ] GFS + IFS 双源都过 strict latest/series/ops/logs/browser 才算 cross-plane `PASS`；单源为 `PARTIAL`。

### C4. 浏览器 e2e（tasks 6.8 + §10.4）

- [ ] 真实浏览器对 27 backend 跑 `/hydro-met`（strict bootstrap）+ `/ops`（display 模式控件隐藏/禁用、无任何 retry·cancel·Slurm POST、queue-depth unavailable 态、诊断复制、人工 22 恢复指引）。
- [ ] 证明 27 只展示 22 产生的 retry/cancel 结果，自身从不创建控制面 receipt。
- [ ] 补 `e2e/monitoring.spec.ts` 的 `display_readonly` 浏览器场景（当前 e2e 无此场景）。

---

## 上线判定

- **B 全绿** + **C1–C4 全部产出 live receipt** → 27 节点可声明上线。
- C 的归因区分（`environment-only`/`production-config`/`data-contract`/`code-contract`）记入 `docs/bugs.md`。
- 注意：cross-plane（C3）依赖 22 侧有真实双源 cycle 产出（已业务化具备），以及 published artifacts 已 copyback 到 27 可读路径（`progress.md` §「仍需 live proof」中 `NHMS_PUBLISHED_ARTIFACT_ROOT` 由 22 私有 staging 切 `/ghdc` 的那一步是前置）。
