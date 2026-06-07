# node-27 业务化上线 worklog（分支 node27-live-bringup）

> 归档：deployment / live-operationalization archetype（dual-end-issue-workflow）。
> Evidence Floor = `docs/runbooks/node-27-bringup-checklist.md` C1–C4 live receipt。
> 本 worklog = 上线前**实测状态 + 有序行动计划 + 阻塞**；receipt 判定标准以 checklist 为准，不在此复述。
> 状态快照日期：2026-06-07（master `5a83123` 同步后实测）。

## 1. node-27 实测现状（2026-06-07）

| 项 | 状态 | 证据 |
|---|---|---|
| 仓库同步 | ✅ master `5a83123`，工作树干净（仅 untracked `.python-version`，master 未跟踪、无冲突） | ff-only 成功，behind 0 |
| uv / python | ✅ `~/.local/bin/uv` + Python 3.11.15 | — |
| 前端构建 | ✅ `apps/frontend/dist`（assets + index.html）已在 | — |
| compose + 静态校验脚本 | ✅ `infra/compose.display.yml` + `scripts/validate_two_node_docker_runtime.py` 在位 | — |
| display.env 模板 | ✅ `infra/env/display.example` 在位 | — |
| **published copyback（C3 前置）** | ✅ **双源真实数据已回灌** `/home/ghdc/nwm/published`：472 文件，`logs/gfs`+`logs/IFS` 多周期，`tiles/hydro` 含 GFS+IFS `q-down`（2026-06-01~03） | `find` 472 files |
| 历史证据 | ℹ️ `artifacts/` 有 dev-server / mvp-e2e / production-like-e2e / production-closure 历史 | — |

## 2. 阻塞 / 缺失（上线前必须解决）

| # | 阻塞 | 影响 | 解除方 |
|---|---|---|---|
| B1 | **`infra/env/display.env` 缺失**（node-side，不同步） | C1/C2/C3 全部依赖它（角色 env + 只读 DSN + published root） | orchestrator 从 display.example 派生填值（node-27 本地） |
| B2 | **只读 DB 账号未确认**（`NHMS_DISPLAY_READONLY_DATABASE_URL` 未设） | C2 denied-write receipt 的前置；无真实 RO 账号 → C2 必须报 `BLOCKED`（禁止 mock 冒充 PASS） | **外部依赖**：需 node-22/DBA 在只读副本上建 `nhms_display_ro`（或确认已有），提供 DSN |
| B3 | `NHMS_PUBLISHED_ARTIFACT_ROOT` 未设 | C1/C3 published 只读探测 | B1 内填 `=/home/ghdc/nwm/published` 即可（数据已在） |

## 3. 有序行动计划（C1→C4）

> 路由：repo-side（本分支改 → 本地校验 → push → node-27 pull）/ node-side（orchestrator SSH 在 27 跑，产 receipt）/ external（需他方提供）。

### C1 部署 receipt（dev-phase 本地起服务，非 docker compose up）
- [ ] **B1** 派生 `infra/env/display.env`（node-side）：`NHMS_SERVICE_ROLE=display_readonly`、`NHMS_REQUIRE_SERVICE_ROLE=1`、`NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=1`、`NHMS_PUBLISHED_ARTIFACT_ROOT=/home/ghdc/nwm/published`、`DATABASE_URL`=只读 DSN（B2）、CORS 等。
- [ ] 静态校验：`uv run python scripts/validate_two_node_docker_runtime.py static`（校验 compose/env 不拉起，§10.1）。
- [ ] dev-phase 本地起服务：`set -a; source infra/env/display.env; set +a` → `uv run python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000`，实机探测 `/health`、`/api/v1/runtime/config`→`display_readonly`、`/api/v1/slurm/*`→404，published 只读；产脱敏 receipt 到 `artifacts/`；用后 kill PID。
- [ ] （生产部署 `docker compose up -d` 属 human-gated，不在 dev-phase，单列治理。）

### C2 只读 DB denied-write receipt（tasks 5.1/5.2/5.4/5.8）— **依赖 B2**
- [ ] 设 `NHMS_DISPLAY_READONLY_DATABASE_URL`，跑 readonly DB validation 入口；display API（health/models/stations/latest-product/pipeline status·stages·jobs·logs/runtime config）在只读凭证 PASS，identity-bound 路由用 strict `source/cycle_time/run_id/model_id`，logs 绑 `job_id`。
- [ ] permission-denied 矩阵：hydro/met/ops 关键表 INSERT/UPDATE/DELETE/DDL/TRUNCATE/sequence/schema CREATE 全拒，记 `current_user` + role 类型。
- [ ] 无真实 RO DB → 入口报 `BLOCKED`（不得 mock 冒充）。

### C3 cross-plane identity live（tasks 4.3 + §10.2/10.3）— published 前置✅
- [ ] 同一 `run_id/source/cycle_time/model_id/basin_id` 串：22 产 → DB → published logs → `/api/v1/mvp/qhh/latest-product` → 27 `/hydro-met`+`/ops`，拒 historical 冒充。
- [ ] GFS+IFS 双源都过 strict latest/series/ops/logs/browser → `PASS`；单源 `PARTIAL`。（published 已有双源数据，待 latest-product 对真实 RO DB 跑通。）

### C4 浏览器 e2e（tasks 6.8 + §10.4）
- [ ] **repo-side**：补 `e2e/monitoring.spec.ts` 的 `display_readonly` 浏览器场景（当前 e2e 无此场景）——本分支可先做。
- [ ] 真实浏览器对 27 backend 跑 `/hydro-met`（strict bootstrap）+ `/ops`（控件隐藏/禁用、无 retry·cancel·Slurm POST、queue-depth unavailable、诊断复制、人工 22 恢复指引）。
- [ ] 证明 27 只展示 22 产生的 retry/cancel 结果，自身从不创建控制面 receipt。

## 4. 关键决策点（需用户/他方）
- **B2 只读账号**：node-22/DBA 是否已建 `nhms_display_ro`？无则 C2 `BLOCKED`，需外部提供 DSN。
- **生产部署 `docker compose up -d`**：human-gated（与 merge 同治理），dev-phase 不触发。
- **上线判定**：B 全绿 + C1–C4 全产 live receipt（C3 双源 PASS）→ 可声明上线。

## 5. 进度
- 2026-06-07：建分支 `node27-live-bringup`；node-27 ff-only 同步到 `5a83123`；实测状态 + 计划落本 worklog。下一步待定（C4 e2e 场景可 repo-side 先行；C1 需先派生 display.env）。
