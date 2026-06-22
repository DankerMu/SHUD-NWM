# node-27（display_readonly）上线清单

> 来源：M22 tasks↔代码对账（2026-06-06）。**结论：27 节点代码功能 ~95% 已落地（角色边界、retry/cancel fail-closed、artifact reader、strict identity、readonly 探测、前端 gating 全在），不是从零开发，而是「补尾巴 + live 化」。**
> 本清单 = 待办的全部工作，分三批：A 已完成（回填）、B 测试尾巴（本地可做）、C live 证据（需 node-27 实机 / 真实只读 DB / 浏览器）。
> 对账明细见 `openspec/changes/m22-two-node-docker-readonly-display/tasks.md`；角色边界设计见 `docs/runbooks/two-node-deployment-overview.md`。

## 开发流程衔接（2026-06-07）

- **验证 oracle 路由**：后端代码/DB pytest 在 **node-22**；display API / 前端生产化 / 只读边界（本清单 C1–C4）在 **node-27**——node-22 的 pytest **不**闭合 C1–C4。详见 `CLAUDE.md`「验证 oracle 路由」+ `dual-end-issue-workflow` skill。
- **27 前端生产化的功能性开发走 m25 change**：`openspec/changes/m25-multibasin-frontend-production/`（多流域选择器、latest-product 去硬编码/basin_id、洪水重现期独立 `return_period_status`、/ops·/monitoring display 降级）；并行起点 issue #310/#311/#313/#317。本清单聚焦"上线 live receipt"，m25 聚焦"功能交付"，二者互补。
- **m25 功能已交付（2026-06-07，#310–#317 已合并，#318 收尾）**：多流域展示（数据驱动选择器 +
  `basin_id` 参数化 + `has_display_product` 动态发现，**无硬编码白名单**）、`/ops`+`/monitoring` 按
  `display_readonly` display 降级、return-period 诚实
  `availability.return_period_status`（独立 supplemental，不进 blocking）均已落地并过本地/CI 校验；
  当时 `/meteorology` 门控属于 pre-M26 页面语义，M26 后只作为 legacy redirect /
  compatibility context，不是当前 active display proof。
  - **不改变本清单 C1–C4 的判定标准**：C1–C4 live receipt 仍须在 node-27 实机产出，是上线的实质；
    m25 交付的是"功能在代码层就绪"，不等于"已在 27 实机验证上线"。
  - 可扩展性（新流域零代码改动出现）已有真 DB 集成断言（`tests/test_real_basin_discovery_integration.py`），
    但其作为上线 receipt 仍以 node-27 cross-plane live（C3）为准。
- **CI**：纯前端/docs 子 PR 按路径 scope 跳过后端 pytest；迭代标 **draft**（定向快速通道）、合并前转 **ready**（全量门）。约定见 `CLAUDE.md`「CI 范围与门控」。

### M26 统一地图展示（2026-06-07，EPIC #336 已关闭）

- **27 展示端形态已变**：展示前端从 ~10 条路由 + 顶部导航收敛为**一张全屏地图**（无 `NavBar`），旧展示路由
  （`/hydro-met`/`/overview`/`/forecast`/`/meteorology`/`/flood-alerts`/`/basins/:id`/`/segments/:id`）
  全 `replace` 重定向到 `/` + 语义参数；`/ops`/`/monitoring`/`/system/model-assets` 经 RBAC 仍可达。
  2496 行玩具页 `HydroMetPage` 已删，honest-display 库迁入 popup 复用。change 详见
  `openspec/changes/m26-unified-map-display/`，全链路与边界见 `progress.md`「最新」M26 块。
- **M26 已在 node-27 实机产 live receipt**（`worklogs/node27-live-receipt.md`，`execution_mode=live_proof`，
  dev-phase 本地 uvicorn 起 `apps.api.main:app`，非 `docker compose up`，符合 C1 deploy gate）：①重定向矩阵
  7/7、②全屏无导航、③QHH↔Heihe 同页 zoom（pathname 恒 `/`）、⑥overlay 未注册如实显示
  「Layer is not registered」=**live-PASS**；平面身份 `service_role=display_readonly`/
  `control_mutations_enabled=false`/`slurm_routes_enabled=false` live 确认。
- **与本清单 C 关系**：M26 receipt 是 **C4 浏览器 e2e** 在新单页地图形态下的**部分闭合**（单页 shell 的重定向/全屏/诚实 overlay live 已证），
  **不替代** C1（生产 docker 部署）/ C2（只读 DB denied-write 矩阵）/ C3（cross-plane identity GFS+IFS 双源）——
  这三项仍须独立产 live receipt。④⑤ 代站/河段 popup 的 live 点击截图因 `/api/v1/basins` 无 bbox（无法自动 framing）+
  CLI 难命中 WebGL 要素而由 #389 单独承接，绘制不变量已由本地单测全覆盖、数据 live 就绪；这是 bbox/点击自动化/popup live click
  证据缺口，不再归为 live MVT 开关/根因问题。
- **live MVT closure（#351 → #343）**：#351 已用 2026-06-08 node-27 live receipt 闭合 #343；`NHMS_ENABLE_LIVE_POSTGIS_MVT=true`
  后 `/api/v1/layers` 返回 live layers，`hydro-national/q_down` tile 200。原 river-network 424 / hydro 409 根因是
  display readonly 未启用 live PostGIS MVT 和图层未注册。
- **解耦平行 issue**：**#342**（station-MVT 点图层端点，全国万级代站，node-22 oracle）仍 open；**#389** 承接 bbox/framing/点击自动化/popup live click 浏览器证据缺口；二者均独立于 #351/#343 的 live MVT closure。

## 拓扑回顾

| 节点 | 角色 | 能力 |
|---|---|---|
| node-22 | `compute_control` | 调度/Slurm/SHUD/发布/retry-cancel（已业务化） |
| node-27 | `display_readonly` | 只读消费 DB + published artifacts，`/` 单页地图 + `/ops`；无 Slurm/Docker socket/控制面写 |

published 路径：22 写 `/ghdc/data/nwm/published`，27 只读 `/home/ghdc/nwm/published`。DB：27 用只读账号（如 `nhms_display_ro`）。

---

## A. 已完成（代码 + 单测，已回填 tasks.md）

无需再做，仅作上线前 self-check 的可信基线：

- 角色边界与启动校验：`apps/api/runtime_mode.py`（4 角色、production-like predicate、display unsafe-config blockers）
- Slurm 路由按角色不挂载：`apps/api/main.py:310`；`GET /api/v1/runtime/config` capability flags：`main.py:283`
- retry/cancel fail-closed `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`、queue-depth `503 CONTROL_PLANE_QUEUE_UNAVAILABLE`：`apps/api/routes/pipeline.py`
- artifact log reader（`published://`/穿越/脱敏/tail）：`services/artifacts/reader.py`；compute 侧发 `published://logs/...`：`chain.py:4143`
- latest-product / ops strict identity（拒 historical fallback、`PIPELINE_STRICT_IDENTITY_MISMATCH`）：Python modules `apps.api.routes.forecast`、`apps.api.routes.pipeline`、`packages.common.forecast_store`
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

- [ ] **开发期：27 本地起 display API**（不 `docker compose up`）：先读取同一份
  host env，再启动 wrapper。

  ```bash
  set -a
  . infra/env/display.env
  set +a
  DISPLAY_API_BASE_URL="http://127.0.0.1:${NHMS_DISPLAY_API_PORT:-8080}"
  scripts/ops/start-display-api.sh
  ```

  `NHMS_DISPLAY_API_PORT` 控制 host 端口，未配置时默认 `8080`。wrapper 与容器使用同一
  `apps.api.main:app` 入口和角色守卫，env 含 `NHMS_SERVICE_ROLE=display_readonly`。
  开发期启动快、无镜像构建、无对外容器；用后按 wrapper 输出 PID 停止。
- [ ] 证明 27 无 Slurm CLI/config/socket、无 Docker socket、无禁止 mount/env、`/api/v1/slurm/*` 404、published 只读、`GET /api/v1/runtime/config` 返回 `display_readonly`：`uv run python scripts/validate_two_node_docker_runtime.py static`（**静态校验 compose/env 而不拉起**，对应 §10.1）+ 对本地服务实机探测（`/health`、`/runtime/config`、`/slurm/health`→404）。
- [ ] **生产部署（非开发期，human-gated）**：`docker compose --env-file infra/env/display.env -f infra/compose.display.yml up -d` 起持久对外容器——难回滚 + 改状态，须显式人工确认/预授权（与 merge 同治理）；`smoke`（镜像构建）归此阶段。

### C2. 只读 DB denied-write receipt（tasks 5.1/5.2/5.4/5.8）

- [ ] 用 27 真实只读账号设 `NHMS_DISPLAY_READONLY_DATABASE_URL`（或 `NHMS_READONLY_DB_VALIDATION_DATABASE_URL`），跑 readonly DB validation 入口，产出脱敏 evidence：
  - display API（health/models/stations/latest-product/pipeline status·stages·jobs·logs/runtime config）在只读凭证下 PASS，identity-bound 路由用一个 strict `source/cycle_time/run_id/model_id`、logs 绑 `job_id`。
  - permission-denied 矩阵：`hydro/met/ops` 关键表的 INSERT/UPDATE/DELETE/DDL/TRUNCATE/sequence/schema CREATE 全被拒，记录 `current_user` + DB role 类型。
  - 缺真实 DB 时入口必须报 `BLOCKED`，不得 mock 冒充 PASS。

### C3. cross-plane identity live（tasks 4.3 + §10.2/10.3）

- [ ] 同一个 `run_id/source/cycle_time/model_id/basin_id` 串起：22 生产 → DB 状态 → published logs → `/api/v1/mvp/qhh/latest-product` → 27 `/` 单页地图 + `/ops`，**拒 historical latest 冒充**。
- [ ] GFS + IFS 双源都过 strict latest/series/ops/logs/browser 才算 cross-plane `PASS`；单源为 `PARTIAL`。

### C4. 浏览器 e2e（tasks 6.8 + §10.4）

> M26（EPIC #336）已对**新单页全屏地图**形态产 live browser receipt（重定向矩阵 / 全屏无导航 / QHH↔Heihe 同页 zoom / overlay 诚实未注册态 = live-PASS，见上「M26」节）；C4 判定**以 `/` 单页地图 + `/ops` 为准**，`/hydro-met -> /` 只作为旧别名重定向 smoke。#351 已闭合 #343 的 live MVT 开关/图层注册根因；④⑤ popup live 点击的 bbox/framing 与 WebGL 命中证据由 #389 补齐。

- [ ] 真实浏览器对 27 backend 跑 `/` 单页地图（strict bootstrap）+ `/ops`（display 模式控件隐藏/禁用、无任何 retry·cancel·Slurm POST、queue-depth unavailable 态、诊断复制、人工 22 恢复指引）；如保留 `/hydro-met -> /`，只记录为 redirect smoke。
- [ ] 证明 27 只展示 22 产生的 retry/cancel 结果，自身从不创建控制面 receipt。
- [ ] 补 `e2e/monitoring.spec.ts` 的 `display_readonly` 浏览器场景（当前 e2e 无此场景）。

#### ④⑤ 代站/河段 popup live click 证据缺口定义（#389 承接）

> 三类证据严格分离，不得互相冒充：**live MVT closure**（#351→#343，已闭合）/ **station-MVT 端点**
> （#342，node-22 oracle，open）/ **bbox·framing·popup live click 浏览器自动化**（#389，本节）。

要让 #389 可靠自动化 river/station popup 的 live 点击，需先补齐以下**可被自动化消费的**证据，缺一则
popup live click 只能人工截图、无法纳入 C4 自动 receipt：

- [ ] **basin/河网 framing bbox**：`/api/v1/basins`（及河段/代站列表响应）当前**不返回 geo bbox**，
  浏览器无法据此 `map.fitBounds` 自动定位到要素再点击。定义所需：列表/详情响应附带要素 bbox（或
  提供按 id 取 bbox 的轻端点），使 e2e 能确定性 framing。**此数据契约属 node-22/display API 侧**，
  与 #342 station-MVT 协同，非本前端 issue 单独可闭合。
- [ ] **WebGL 要素命中坐标**：MapLibre WebGL canvas 内要素无 DOM 句柄，CLI/Playwright 难以稳定命中
  点击。定义所需：暴露确定性「测试钩子」（如按 id 触发 popup 的 `window.__m11SelectFeature(id)` 调试入口，
  或 feature→屏幕坐标的可查询映射），使点击/弹窗可被断言，而非靠像素猜测。
- [x] **node-27 浏览器可启动**（**#431 已解，2026-06-10**）：曾缺 `libgbm.so.1`/`libxcb-randr.so.0`
  导致 chromium `exitCode=127`。已用 `sudo apt-get install -y libgbm1 libxcb-randr0`（apt 自动带
  `libwayland-server0`）系统级安装（Ubuntu jammy 上 `sudo` 验证的是调用者 nwm 自身密码，与被禁用的
  root 密码无关，无需 root SSH）。验证：`ldd .../chromium-1217/chrome-linux64/chrome` 无缺库、
  **不带** `LD_LIBRARY_PATH` 启动 chromium exit 0、master `test:e2e:mocked-regression` → 19 passed。
  临时 `~/pwdeps` userspace hack 已清除。live browser lane（含本节 popup live click、
  `e2e/live-display.spec.ts`）的浏览器前提已就绪。
- [ ] 上述就绪后：river popup（流量/起报时间）+ station popup（forcing 序列）的 live 点击截图 + 断言
  作为 C4 ④⑤ 的 live receipt 归入 **#389**（绘制不变量已由本地单测全覆盖、数据 live 就绪，仅缺自动化
  framing/命中/浏览器三要素）。

---

## 上线判定

- **B 全绿** + **C1–C4 全部产出 live receipt** → 27 节点可声明上线。
- C 的归因区分（`environment-only`/`production-config`/`data-contract`/`code-contract`）记入 `docs/bugs.md`。
- 注意：cross-plane（C3）依赖 22 侧有真实双源 cycle 产出（已业务化具备），以及 published artifacts 已 copyback 到 27 可读路径（`progress.md` §「仍需 live proof」中 `NHMS_PUBLISHED_ARTIFACT_ROOT` 由 22 私有 staging 切 `/ghdc` 的那一步是前置）。
