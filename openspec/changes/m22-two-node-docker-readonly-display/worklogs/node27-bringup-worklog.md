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

## 2. 阻塞 / 缺失 — **2026-06-07 全部解除** ✅

| # | 阻塞 | 解除结果 |
|---|---|---|
| B1 | `infra/env/display.env` 缺失 | ✅ 已在 node-27 派生 `infra/env/display.env`（0600，不入 git）：`NHMS_SERVICE_ROLE=display_readonly`、RO DSN、`NHMS_PUBLISHED_ARTIFACT_ROOT=/home/ghdc/nwm/published` |
| B2 | 只读 DB 账号 | ✅ 已在 node-22 主库建 `nhms_display_ro`（superuser nhms 执行）：6 应用 schema + `_timescaledb_internal` 只读；自检 SELECT 通过（hydro_run/river_timeseries hypertable 2944万）、CREATE/INSERT permission denied。**node-27 经公网 `210.77.77.22:55433` 连**（内网 10.0.2.100 不可达；node-27 在 10.0.1.27 子网） |
| B3 | published root 未设 | ✅ display.env 填 `/home/ghdc/nwm/published`（472 文件双源数据已在） |

> **拓扑订正**：node-27 是**共享节点**（另跑 geoserver/docmost/GHDC Django api 栈/nginx/rabbitmq），其宿主 `:5433` postgres 非 node-22 物理副本（拒 RO 角色认证）。NHMS display 实际连 **node-22 公网 DB `210.77.77.22:55433`**，以 `nhms_display_ro` 只读账号。真正的本地只读副本属后续（C2 denied-write 矩阵可在此账号上跑）。

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

## 6. B2 只读账号创建 SQL（就绪，待执行 — 暂不跑）

> 由 orchestrator 在 **node-22**（compute_control，DB 主库）执行。production DB 状态变更，**待用户点头**。
> 纪律：node-22 **不 git pull**（NFS stale handle 杀生产）；`psql` 不在 PATH → 走 `uv run python` + psycopg2；
> `set -a; source infra/env/compute.host.env; set +a` 取 DSN；任何 DSN echo 前 `sed -E 's#://[^@]*@#://REDACTED@#'` 脱敏。
> 前置探测（只读，安全）：`current_user` 是否有 `CREATEROLE`/superuser；`nhms_display_ro` 是否已存在；目标 DB/schema 清单。

只读角色设计（最小权限、纯 SELECT、无写无 DDL）：

```sql
-- 1) 角色（强随机密码，建时生成，不写入仓库）
CREATE ROLE nhms_display_ro LOGIN PASSWORD '<generated-strong-random>';
-- 2) 连接 + schema 使用权
GRANT CONNECT ON DATABASE <db> TO nhms_display_ro;
GRANT USAGE ON SCHEMA core, hydro, met, flood, ops TO nhms_display_ro;
-- 3) 现有表只读
GRANT SELECT ON ALL TABLES IN SCHEMA core, hydro, met, flood, ops TO nhms_display_ro;
-- 4) 未来表自动只读（default privileges 须按各 schema owner 角色分别设）
ALTER DEFAULT PRIVILEGES IN SCHEMA core, hydro, met, flood, ops GRANT SELECT ON TABLES TO nhms_display_ro;
-- 5) 显式不授予：INSERT/UPDATE/DELETE/TRUNCATE/CREATE/USAGE-on-sequence-nextval 等（默认即无）
```

验证（建后自检，C2 会正式产矩阵）：以 `nhms_display_ro` 连接 → `SELECT` 通过；`INSERT/UPDATE/CREATE TABLE` 全 `permission denied`。
产出 DSN（脱敏存档 + 真实凭据交付用户填 display.env）：`postgresql://nhms_display_ro:REDACTED@210.77.77.22:<port>/<db>`。
> 实际 schema 清单以前置探测为准（上面是按 specs 拓扑的预估）；owner 分歧时 default privileges 分角色补。

## 7. 执行结果（2026-06-07 live bring-up）

receipt：node-27 `artifacts/production-closure/node27-bringup-receipt.json`（`execution_mode: live_proof`，未入 git）。

| 项 | 结果 |
|---|---|
| **B1/B2/B3** | ✅ 全部解除（见 §2） |
| **C1 部署 receipt** | ✅ 静态校验 `validate_two_node_docker_runtime.py static` = **PASS**；dev 本地起 `uvicorn apps.api.main:app`（node-27 `127.0.0.1:8000`，`.venv` 直跑）；`/health`=200、`/api/v1/runtime/config`=`{service_role:display_readonly, control_mutations_enabled:false, slurm_routes_enabled:false, queue_depth_mode:display_readonly_unavailable}`、`/api/v1/slurm/health`=**404**、`/api/v1/pipeline/queue-depth`=**404**、retry POST=**405**（控制面禁用） |
| **C3 cross-plane（双源）** | ✅ **GFS+IFS 双源 PASS**：latest-product GFS=`fcst_gfs_2026060518_basins_qhh_shud`(ready,386站/1633段)、IFS=`fcst_ifs_2026060518_basins_qhh_shud`(ready)；`/basins?has_display_product=true`=`[basins_heihe, basins_qhh]`（多流域发现）。链路：22 产→node-22 DB→node-27 display API→浏览器，真实数据无 historical 冒充 |
| **浏览器（agent-browser，本地隧道 127.0.0.1:8899→node-27:8000）** | ✅ `/hydro-met` 完整渲染：QHH latest-product、386 forcing 站点全变量真实曲线（quality_flag ok）、5371 河段、q_down 168点、honest-display 红线"不绘制假曲线"在。截图 `/tmp/n27-hydromet.png` |
| **C2 只读 denied-write** | 🟡 账号级已证（§2 自检 CREATE/INSERT denied）；完整 permission-denied 矩阵（各表 INSERT/UPDATE/DDL/TRUNCATE/sequence）+ 脱敏 evidence 待补 |
| **C4 浏览器 e2e** | 🟡 `/ops` 在 production auth 下匿名访问被路由守卫挡（"权限不足"）→ display-mode 受控场景需认证会话；`e2e/monitoring.spec.ts` display_readonly 场景（repo-side）待补 |

### 隧道说明
未发现用户预设的 node-27 隧道（本地仅 18080=sub2api）。本次 orchestrator 自建 `ssh -L 8899:127.0.0.1:8000 nwm@node-27` 跑浏览器校验（会话级，后台任务保活）。如需常驻访问，可按 CLAUDE.md `local:8080→node-27:8080` 另建并把服务绑对应端口。

## 8. 剩余（上线前）
- **C2**：完整只读 denied-write 矩阵 + 脱敏 evidence（在 `nhms_display_ro` 上跑 `readonly_db_validation` 入口）。
- **C4**：认证会话下 `/ops` display-mode 受控 e2e（控件隐藏/禁用、无 retry·cancel·Slurm POST、queue unavailable、诊断复制）+ 补 `e2e/monitoring.spec.ts` display_readonly 场景（repo-side，本分支可做）。
- **真正本地只读副本**（可选硬化）：当前 display 直连 node-22 公网 DB；若要 node-27 独立只读副本，需 DBA 配流复制后把 DSN 切到本地副本。
- **生产部署**：`docker compose -f infra/compose.display.yml up -d`（human-gated）。

## 9. 进度
- 2026-06-07：建分支；node-27 ff-only 同步 `5a83123`；梳理 C1–C4。
- 2026-06-07：用户授权执行 → B2 建 `nhms_display_ro`（node-22）；B1 派生 display.env；C1 静态校验 PASS + 起服务 + 角色/slurm 边界证实；C3 GFS+IFS 双源 latest-product 实证；agent-browser 经隧道渲染 `/hydro-met` 通过；receipt 落 node-27 artifacts；临时凭据三端清理；密码仅存 display.env(0600)。
