# node-27（display_readonly）上线清单

> 来源：M22 tasks↔代码对账（2026-06-06）。**结论：27 节点代码功能 ~95% 已落地（角色边界、retry/cancel fail-closed、artifact reader、strict identity、readonly 探测、前端 gating 全在），不是从零开发，而是「补尾巴 + live 化」。**
> 本清单 = 待办的全部工作，分三批：A 已完成（回填）、B 测试尾巴（本地可做）、C live 证据（需 node-27 实机 / 真实只读 DB / 浏览器）。
> 对账明细见 `openspec/changes/m22-two-node-docker-readonly-display/tasks.md`；角色边界设计见 `docs/runbooks/two-node-deployment-overview.md`。

## 开发流程衔接（2026-06-07）

- **验证 oracle 路由**：本地跑 lint/unit/OpenSpec/前端构建；真实 DB、ingest、display API、前端生产化和只读边界（本清单 C1–C4）在 **node-27** 产 live receipt；只有 sbatch、Slurm gateway、SHUD runtime 或调度行为变更才走 **node-22** Slurm scheduling oracle。node-22 检查和本地检查都不闭合 C1–C4。
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
  这三项仍须独立产 live receipt。④⑤ popup live 点击证据缺口按 2026-09 状态拆分：**river popup** 的
  framing/命中已由 #1970 门控 hook 交付（详情 geom bbox + 真实渲染要素 + 既有 onOverlayClick 路径），
  live receipt 由 C4-river-click 节（#1895）执行；**station popup**（station-MVT 端点/bbox 属 #342 协同）
  仍由 #389 承接，绘制不变量已由本地单测全覆盖、数据 live 就绪。这不再是整体「#389 唯一承接」的表述。
- **live MVT closure（#351 → #343）**：#351 已用 2026-06-08 node-27 live receipt 闭合 #343；`NHMS_ENABLE_LIVE_POSTGIS_MVT=true`
  后 `/api/v1/layers` 返回 live layers，`hydro-national/q_down` tile 200。原 river-network 424 / hydro 409 根因是
  display readonly 未启用 live PostGIS MVT 和图层未注册。
- **解耦平行 issue**：**#342**（station-MVT 点图层端点，全国万级代站，node-27/display API oracle，除非改 Slurm/SHUD 调度）仍 open；**#389** 承接 bbox/framing/点击自动化/popup live click 浏览器证据缺口；二者均独立于 #351/#343 的 live MVT closure。

## 拓扑回顾

| 节点 | 角色 | 能力 |
|---|---|---|
| node-22 | compute/artifact producer | Slurm gateway、Slurm/SHUD compute、forcing/run artifacts 写入 shared NFS；不连当前活 DB |
| node-27 | active DB + ingest + display | 本机 PostgreSQL `:55432`、node-27 ingest writer、display API、前端；display runtime 为 `display_readonly` |

shared NFS 路径：node-22 视图为 `/ghdc/data/nwm/...`，node-27 视图为
`/home/ghdc/nwm/...`。node-22 本地 PG `:55433` 是 historical
do-not-connect、archived/stopped rollback-only archive；当前 active DB 和
display/frontend oracle 都在 node-27。

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

- [ ] **开发期：27 本地起 display API**（不 `docker compose up`）：只读派生端口，
  再启动 wrapper。

  ```bash
  DISPLAY_API_PORT="$(
    set -a
    . infra/env/display.env
    set +a
    printf '%s' "${NHMS_DISPLAY_API_PORT-8080}"
  )"
  DISPLAY_API_BASE_URL="http://127.0.0.1:${DISPLAY_API_PORT}"
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

### C2b. 写侧最小权限 receipt（#1774）

C2 证的是**读**边界（`nhms_display_ro` 无写权）。写边界是另一半，2026-09 之前完全缺失：
ingest / download / compression / cold-residency / retention 五条 lane 全部以 superuser
`nhms` 连库，也就是一份凭据 == 数据库容器内命令执行，而这台机器同时对外提供
`https://test.nwm.ac.cn`。

- [ ] **pre-merge（additive，可在 unit 全部照常运行时做）**：从 detached worktree 跑
      `bash scripts/node27_provision_write_roles.sh --roles-only`，入证：
      `pg_roles` 中 `nhms_ingest_rw` / `nhms_download_rw` 的
      `rolsuper/rolcreaterole/rolcreatedb/rolreplication/rolbypassrls` 全为 `f`；两条
      `copy-from-program refused for …` NOTICE。此阶段**不做** ownership 转移、不取关系锁、
      不动任何 env 文件。
- [ ] **post-merge（timer 停机窗口）**：跑完整 `scripts/node27_provision_write_roles.sh`，
      入证 owner-drift 清单为空、`nhms_display_ro` 有效 SELECT 集合 before/after 一致、
      `relacl` diff（预期只有 grantor 从 `…/nhms` 改写为 `…/nhms_ingest_rw`）。
- [ ] 五条 lane 各在新角色下跑一轮真实 run 并留 receipt；autopipe dry tick 的统计守卫
      **两条 ANALYZE 腿都必须是 `ok`**（`warning` = 非 owner 被静默跳过，tick 绿而腿死）。
- [ ] env 切换后脱敏 `grep`：`/home/nwm/NWM/infra/env/*.env` 中不再出现 `nhms:` DSN 用户名或
      `PGUSER=nhms`，例外只有 `node27-timeseries-compression-replay.env` 与
      `node27-archive-rebuild-drill.env`（migration-class，理由已记档）。

完整口径、退出码与回滚见 `docs/runbooks/tier-node27-timeseries-storage.md` §9。

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
> （#342，node-27/display API oracle，open；不含 Slurm/SHUD 调度）/ **bbox·framing·popup live click 浏览器自动化**（#389，本节）。

要让 #389 可靠自动化 river/station popup 的 live 点击，需先补齐以下**可被自动化消费的**证据，缺一则
popup live click 只能人工截图、无法纳入 C4 自动 receipt：

- [ ] **basin/河网 framing bbox**：`/api/v1/basins`（及河段/代站列表响应）当前**不返回 geo bbox**，
  浏览器无法据此 `map.fitBounds` 自动定位到要素再点击。定义所需：列表/详情响应附带要素 bbox（或
  提供按 id 取 bbox 的轻端点），使 e2e 能确定性 framing。**此数据契约属 node-27/display API 侧**，
  与 #342 station-MVT 协同，非本前端 issue 单独可闭合。
- [x] **WebGL 要素命中 + 河段 framing（river-click 路径，#1970 已交付，2026-09）**：门控的只读
  `window.__nhmsRiverClickEvidence.selectRenderedRiver` 钩子以真实渲染要素沿既有 `onOverlayClick`
  点击路径完成 fit/命中/弹窗打开；配合 `basin-versions/{id}/river-segments/{segment_id}` 详情响应的
  *geom bbox*（M11 段详情本就带 geom），河段 river popup 的确定性 framing/命中已可自动化（见下方
  C4-river-click 节，由 #1895 产出 live receipt）。**station popup 仍无等价路径**（station-MVT
  端点/bbox 属 #342/#389 协同侧，未闭合）。
- [x] **node-27 浏览器可启动**（**#431 已解，2026-06-10**）：曾缺 `libgbm.so.1`/`libxcb-randr.so.0`
  导致 chromium `exitCode=127`。已用 `sudo apt-get install -y libgbm1 libxcb-randr0`（apt 自动带
  `libwayland-server0`）系统级安装（Ubuntu jammy 上 `sudo` 验证的是调用者 nwm 自身密码，与被禁用的
  root 密码无关，无需 root SSH）。验证：`ldd .../chromium-1217/chrome-linux64/chrome` 无缺库、
  **不带** `LD_LIBRARY_PATH` 启动 chromium exit 0、master `test:e2e:mocked-regression` → 19 passed。
  临时 `~/pwdeps` userspace hack 已清除。live browser lane（含本节 popup live click、
  `e2e/live-display.spec.ts`）的浏览器前提已就绪。
- [ ] 上述就绪后：station popup（forcing 序列）的 live 点击截图 + 断言仍在 **#389** 未闭（river popup
  的流量/起报时间 live 点击已由 C4-river-click 节承接，#1970 交付、#1895 执行）。

#### C4-river-click：`/` 河段点击 GFS+IFS P95 证据（#1970 → #1895）

> #1970 交付了**门控的只读测试钩子 + 无 mock 的 live P95 采集 lane**（代码与本地单测已就绪），
> 本 PR **没有**在 node-27 实机执行、**没有**产 live PASS。真正的 live PASS 由 **#1895** 用
> 下面的 exact merged command 在当前运行里产出。
>
> C4 #389 历史文本（station popup / basin-bbox 的 live receipt）保持诚实：river 河段点击的
> WebGL 钩子与 segment-detail 几何 framing 已由 #1970 交付；station popup 与 basin-bbox 的
> live receipt **仍未交付**，属 #389 仍打开的工作。

- **环境（五个键；口径：URL/receipt 缺失 => BLOCKED，pin 缺失/非法 => FAIL）**：
  `PLAYWRIGHT_LIVE_BASE_URL`（27 前端 bare origin，如
  `https://test.nwm.ac.cn`）、`PLAYWRIGHT_LIVE_API_BASE_URL`（27 API bare origin）、
  `PLAYWRIGHT_LIVE_RIVER_BASIN_ID`、`PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID`（当前 M11 pin，来自当
  run 的 live identity——`GET /api/v1/basins/{basin_id}/versions` 或
  `GET /api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=<pin>`
  的当前 `basin_version_id`/`river_network_version_id`，**不复制历史证据**）、
  `PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH`（本次运行的**唯一不存在**绝对路径，见下）。
  缺失 frontend/API URL 或缺失/不安全 receipt path => `BLOCKED`（无文件或 BLOCKED receipt）；
  缺失/非法 pin（含空值）或非法 URL/path => `FAIL`（CONFIG_INVALID receipt）。
  禁止设置六个 override 键（出现即 FAIL，即使值为空）：`PLAYWRIGHT_LIVE_RIVER_RUN_ID`、
  `PLAYWRIGHT_LIVE_RIVER_MODEL_ID`、`PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID`、
  `PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID`、`PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME`、
  `PLAYWRIGHT_LIVE_RIVER_SCENARIO`（`PLAYWRIGHT_LIVE_RIVER_BASIN_ID` 与
  `PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID` 是必需的 pin，不在此列）。
- **私有运行目录 + 唯一 absent receipt**（当前运行绑定；命令 start/end 括号记录
  本次 run 的时间窗，receipt 的 mtime 必须落在括号内才是本次产物）：

  ```bash
  REPO_ROOT="/home/nwm/NWM"
  RUN_ROOT=$(mktemp -d "$REPO_ROOT/.nhms-issue1895-riverclick-XXXXXX")
  test -d "$RUN_ROOT"                                   # mktemp -d 独占直接创建（无共享 base 复用）
  chmod 0700 "$RUN_ROOT"                                # 独占直接创建（无共享 base 复用）
  test "$(stat -c '%u' "$RUN_ROOT")" = "$(id -u)"
  test "$(stat -c '%a' "$RUN_ROOT")" = "700"
  RECEIPT="$RUN_ROOT/nhms-frontend-river-click-live-evidence-$(date -u +%Y%m%dT%H%M%SZ).json"
  CMD_START=$(date -u +%s)
  test ! -e "$RECEIPT"   # 必须不存在；no-clobber 发布拒绝覆盖任何旧文件
  ```

- **exact merged command**（单 worker / 0 retries；浏览器只在 `page.addInitScript` 里设
  `window.__NHMS_E2E_HOOKS__ = true` 后面访问 `/`，不在 URL 放身份参数；命令从
  REPO_ROOT 运行并通过 `pnpm --dir` 解析 frontend 包（repo root 无 package.json），
  binder 的 `schemas/...` 因此可解析）：

  ```bash
  cd "$REPO_ROOT" || { echo "BLOCKED: REPO_ROOT unreachable"; exit 1; }
  set +e; \
  PLAYWRIGHT_LIVE_BASE_URL="${PLAYWRIGHT_LIVE_BASE_URL-}" \
  PLAYWRIGHT_LIVE_API_BASE_URL="${PLAYWRIGHT_LIVE_API_BASE_URL-}" \
  PLAYWRIGHT_LIVE_RIVER_BASIN_ID="${PLAYWRIGHT_LIVE_RIVER_BASIN_ID-}" \
  PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID="${PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID-}" \
  PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH="$RECEIPT" \
  corepack pnpm@10.11.0 --dir "$REPO_ROOT/apps/frontend" run test:e2e:live-river-click; \
  CMD_EXIT=$?; set -e; \
  CMD_END=$(date -u +%s)
  test "$CMD_EXIT" = "0"
  ```

- **判定**：命令 exit 0 且 receipt 是 schema-1.0 `nhms-frontend-river-click-live-evidence`、
  父目录 mode 0700、receipt mode 0600、`status=PASS`、`warmup_count=1`、`accepted_count=20`、
  `percentile_method=nearest-rank`、`p95_ms < 2000`、`failure=null`、`started_at <= ended_at == generated_at`。
  任何 FAIL/BLOCKED receipt 或 exit != 0 都是 **NO-GO**（`p95_ms >= 2000` = `THRESHOLD_EXCEEDED`）。
  发布失败（路径不安全 / 目标已存在 / 身份漂移）必失败并**不覆盖**旧证据。
- **可执行绑定（receipt 接受性 binder，`set -euo pipefail` 下运行；Node-20
  stdlib 单文件，无运行时依赖；仅接受 PASS 终态——任何非 PASS 都拒绝；
  严格 UTC RFC3339 日历合法时间戳，不用宽松 `Date.parse`、不做字典序比较；
  nearest-rank P95 独立重算自实际 durations；缺失/漂移字段的每一行都是
  `BINDER:` 有界固定形状诊断（不回显路径/origin/identity/OS error）并 exit 1）**：

  ```bash
  test -f "$RECEIPT" && test ! -L "$RECEIPT"                       # regular file, not a symlink
  test "$(stat -c '%u' "$RECEIPT")" = "$(id -u)"                   # euid-owned
  test "$(stat -c '%a' "$(dirname "$RECEIPT")")" = "700"           # parent 0700
  test "$(stat -c '%u' "$(dirname "$RECEIPT")")" = "$(id -u)"
  test "$CMD_START" -le "$(stat -c '%Y' "$RECEIPT")"
  test "$(stat -c '%Y' "$RECEIPT")" -le "$CMD_END"                 # mtime inside the bracket
  test "$(stat -c '%s' "$RECEIPT")" -gt 0                          # non-empty
  test "$(stat -c '%s' "$RECEIPT")" -le 262144                     # refuse oversized content before unbounded schema parse
  test "$(stat -c '%a' "$RECEIPT")" = "600"                        # file 0600
  test "$(stat -c '%h' "$RECEIPT")" = "1"                          # nlink 1
  uv run check-jsonschema --schemafile schemas/frontend_river_click_live_evidence.schema.json "$RECEIPT"
  node "$REPO_ROOT/apps/frontend/scripts/river-click-receipt-binder.mjs" \
    --receipt "$RECEIPT" \
    --frontend-origin "$PLAYWRIGHT_LIVE_BASE_URL" \
    --api-origin "$PLAYWRIGHT_LIVE_API_BASE_URL" \
    --basin-id "$PLAYWRIGHT_LIVE_RIVER_BASIN_ID" \
    --segment-id "$PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID" \
    --cmd-start "$CMD_START" --cmd-end "$CMD_END"
  ```

- **边界**：本 lane 只访问 `/`；`/monitoring` 原文案不变，`/ops` 不在本 metric 内。

---

## 主机容量纪律（每次上 27 干活之前，#1765）

- [ ] 容量核查三个挂载点一起看，**`/` 不能漏**：

  ```bash
  df -h / /home /data/GHDC
  ```

  `/` 只有几十 GB 且以前无人自动看守：一次跨两天的 pytest 用
  `/tmp/pytest-of-nwm` 把它塞满，直接阻塞了当时 PR 的 live receipt。
  `/home` 是 pgdata + object store 共用卷，`/data/GHDC` 是 `ghdc` 表空间 +
  归档根（口径与已知偏差见 `docs/runbooks/current-production-ops.md`）。
- [ ] 在 27 上跑 pytest 之前先把临时根挪出 `/`：

  ```bash
  mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp   # 建议写进 nwm 的登录 profile
  ```

  `mkdir -p` 不能省——`TMPDIR` 指向不存在的目录时 Python 会**静默回落**到
  `/tmp`，于是「设了但没生效」和「设了且生效」看起来一模一样。跑完用
  `ls -d /home/nwm/tmp/pytest-of-nwm` 确认落点，别只看 `df`。
  仓库侧的另一半（`pyproject.toml` 的 `tmp_path_retention_policy = "failed"`）
  已经在代码里，绿的会话不留残留；**不要**在共享配置里加 `--basetemp`。
- [ ] 资源治理审计的告警链已部署（`install` + `systemctl --user daemon-reload`）：
  `nhms-node27-resource-governance.service` 必须带
  `OnFailure=nhms-node27-unit-failure-alert@%n.service`，审计遇到 `critical`
  建议时 exit 1 并向 journal 打 `RESOURCE_GOVERNANCE_CRITICAL:<code>`。

  ```bash
  systemctl --user show nhms-node27-resource-governance.service -p OnFailure
  ```

  **装 `OnFailure=` 之前先看有没有长期 `critical`**：只要还有一条 `critical`
  建议没消，这个 unit 就会**每个每日 tick 都 exit 1**——按设计一直挂在
  `systemctl --user --failed` 里并且每次都发一封信（告警处理器是刻意做傻的，
  没有去重、没有状态）。让它安静的办法是把条件清掉，不是压制告警。所以先读
  最新的一份 receipt 确认当前没有 `severity: critical`：

  ```bash
  ls -t /home/nwm/node27-resource-governance-logs/resource-governance-*.json | head -1 \
    | xargs -r grep -c '"severity": *"critical"'
  ```

  timer 是 `OnCalendar=*-*-* 04:10:00 UTC`，所以「每个 tick」就是每天一封。

---

## 上线判定

- **B 全绿** + **C1–C4 全部产出 live receipt** → 27 节点可声明上线。
- C 的归因区分（`environment-only`/`production-config`/`data-contract`/`code-contract`）记入 `docs/bugs.md`。
- 注意：cross-plane（C3）依赖 22 侧有真实双源 cycle 产出（已业务化具备），以及 published artifacts 已 copyback 到 27 可读路径（`progress.md` §「仍需 live proof」中 `NHMS_PUBLISHED_ARTIFACT_ROOT` 由 22 私有 staging 切 `/ghdc` 的那一步是前置）。
