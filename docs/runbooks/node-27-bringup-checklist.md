# node-27（display_readonly）上线清单

> 来源：M22
> tasks↔代码对账（2026-06-06）。**结论：27 节点代码功能 ~95% 已落地（角色边界、retry/cancel
> fail-closed、artifact reader、strict
> identity、readonly 探测、前端 gating 全在），不是从零开发，而是「补尾巴 +
> live 化」。**
> 本清单 = 待办的全部工作，分三批：A 已完成（回填）、B 测试尾巴（本地可做）、C
> live 证据（需 node-27 实机 / 真实只读 DB / 浏览器）。对账明细见
> `openspec/changes/m22-two-node-docker-readonly-display/tasks.md`；角色边界设计见
> `docs/runbooks/two-node-deployment-overview.md`。

## 当前边界：selective-cold 源码已退役；effective-deployment handoff 待授权

原 #1891/#1895 的生产冷层 rollout 已撤回，不再要求新 G1 retry、冷样本或
G0–G8 窗口。R1–R3 的仓库源码闭包已删除 cold-only runtime、旧 wrappers、
schemas/examples/tests/CI 和 source grant-audit；R4 已把当前权威转回真实
PGDATA、governance、compression、readonly 与 C4 owners。不存在可由当前仓库
启动 selective-cold 的命令、env template 或 grant audit。

这不声明 effective unit/dropin/env、live privilege、tablespace 或数据已处置。
R5 仍须验证存活能力，并另行授权 effective-deployment handoff；#2293/#2298/#1938
只能在受影响路径和部署引用真正退出后按 capability-retired 处置，迁出的缺陷随真实
owner 保留。冷样本/I9/I8/#2162/#2017 不是一揽子退役依赖，部署仍须协调真实
owner 和 foreign holds；既有容量、升级、恢复职责不取消，也不新增 RPO/RTO gate。

本清单 C1–C4、river-click 和既有只读/展示 producer 继续由各自 owner 承担；
**不把 #1895 改造成新的全面 display/storage acceptance 项目**。Bringup-C4
production acceptance 保留外层 reviewed-SHA/digest 与文件身份保证；本地 C4
producer/binder 成功不代替它，也不代替独立的生产部署证明。

## 开发流程衔接（2026-06-07）

- **验证 oracle 路由**：本地跑 lint/unit/OpenSpec/前端构建；真实 DB、ingest、display
  API、前端生产化和只读边界（本清单 C1–C4）在 **node-27** 产 live
  receipt；只有 sbatch、Slurm gateway、SHUD runtime 或调度行为变更才走
  **node-22** Slurm scheduling oracle。node-22 检查和本地检查都不闭合 C1–C4。
- **27 前端生产化的功能性开发走 m25
  change**：`openspec/changes/m25-multibasin-frontend-production/`（多流域选择器、latest-product 去硬编码/basin_id、洪水重现期独立
  `return_period_status`、/ops·/monitoring display 降级）；并行起点 issue
  #310/#311/#313/#317。本清单聚焦"上线 live
  receipt"，m25 聚焦"功能交付"，二者互补。
- **m25 功能已交付（2026-06-07，#310–#317 已合并，#318 收尾）**：多流域展示（数据驱动选择器 +
  `basin_id` 参数化 + `has_display_product`
  动态发现，**无硬编码白名单**）、`/ops`+`/monitoring` 按 `display_readonly`
  display 降级、return-period 诚实
  `availability.return_period_status`（独立 supplemental，不进 blocking）均已落地并过本地/CI 校验；当时
  `/meteorology` 门控属于 pre-M26 页面语义，M26 后只作为 legacy redirect /
  compatibility context，不是当前 active display proof。
  - **不改变本清单 C1–C4 的判定标准**：C1–C4 live
    receipt 仍须在 node-27 实机产出，是上线的实质；m25 交付的是"功能在代码层就绪"，不等于"已在 27 实机验证上线"。
  - 可扩展性（新流域零代码改动出现）已有真 DB 集成断言（`tests/test_real_basin_discovery_integration.py`），但其作为上线 receipt 仍以 node-27
    cross-plane live（C3）为准。
- **CI**：纯前端/docs 子 PR 按路径 scope 跳过后端 pytest；迭代标
  **draft**（定向快速通道）、合并前转 **ready**（全量门）。约定见
  `CLAUDE.md`「CI 范围与门控」。

### M26 统一地图展示（2026-06-07，EPIC #336 已关闭）

- **27 展示端形态已变**：展示前端从 ~10 条路由 + 顶部导航收敛为**一张全屏地图**（无
  `NavBar`），旧展示路由（`/hydro-met`/`/overview`/`/forecast`/`/meteorology`/`/flood-alerts`/`/basins/:id`/`/segments/:id`）全
  `replace` 重定向到 `/` + 语义参数；`/ops`/`/monitoring`/`/system/model-assets`
  经 RBAC 仍可达。2496 行玩具页 `HydroMetPage`
  已删，honest-display 库迁入 popup 复用。change 详见
  `openspec/changes/m26-unified-map-display/`，全链路与边界见
  `progress.md`「最新」M26 块。
- **M26 已在 node-27 实机产 live
  receipt**（`worklogs/node27-live-receipt.md`，`execution_mode=live_proof`，dev-phase 本地 uvicorn 起
  `apps.api.main:app`，非 `docker compose up`，符合 C1 deploy
  gate）：①重定向矩阵 7/7、②全屏无导航、③QHH↔Heihe 同页 zoom（pathname 恒
  `/`）、⑥overlay 未注册如实显示「Layer is not
  registered」=**live-PASS**；平面身份 `service_role=display_readonly`/
  `control_mutations_enabled=false`/`slurm_routes_enabled=false` live 确认。
- **与本清单 C 关系**：M26 receipt 是 **C4 浏览器 e2e**
  在新单页地图形态下的**部分闭合**（单页 shell 的重定向/全屏/诚实 overlay
  live 已证）， **不替代** C1（生产 docker 部署）/ C2（只读 DB
  denied-write 矩阵）/ C3（cross-plane identity
  GFS+IFS 双源）——这三项仍须独立产 live receipt。④⑤ popup
  live 点击证据缺口按 2026-09 状态拆分：**river popup**
  的 framing/命中已由 #1970 门控 hook 交付（详情 geom
  bbox + 真实渲染要素定位 + 浏览器真实指针点击走产品 MapLibre click 路径），live
  receipt 由独立 C4-river-click 展示 lane 执行（原 #1895
  rollout 归属已撤回）；**station
  popup**（station-MVT 端点/bbox 属 #342 协同）仍由 #389 承接，绘制不变量已由本地单测全覆盖、数据 live 就绪。这不再是整体「#389 唯一承接」的表述。
- **live MVT closure（#351 → #343）**：#351 已用 2026-06-08 node-27 live
  receipt 闭合 #343；`NHMS_ENABLE_LIVE_POSTGIS_MVT=true` 后 `/api/v1/layers`
  返回 live layers，`hydro-national/q_down` tile 200。原 river-network 424 /
  hydro 409 根因是 display readonly 未启用 live PostGIS MVT 和图层未注册。
- **解耦平行 issue**：**#342**（station-MVT 点图层端点，全国万级代站，node-27/display
  API oracle，除非改 Slurm/SHUD 调度）仍 open；**#389**
  承接 bbox/framing/点击自动化/popup live
  click 浏览器证据缺口；二者均独立于 #351/#343 的 live MVT closure。

## 拓扑回顾

| 节点    | 角色                         | 能力                                                                                                      |
| ------- | ---------------------------- | --------------------------------------------------------------------------------------------------------- |
| node-22 | compute/artifact producer    | Slurm gateway、Slurm/SHUD compute、forcing/run artifacts 写入 shared NFS；不连当前活 DB                   |
| node-27 | active DB + ingest + display | 本机 PostgreSQL `:55432`、node-27 ingest writer、display API、前端；display runtime 为 `display_readonly` |

shared NFS 路径：node-22 视图为 `/ghdc/data/nwm/...`，node-27 视图为
`/home/ghdc/nwm/...`。node-22 本地 PG `:55433` 是 historical
do-not-connect、archived/stopped rollback-only archive；当前 active
DB 和 display/frontend oracle 都在 node-27。

---

## A. 已完成（代码 + 单测，已回填 tasks.md）

无需再做，仅作上线前 self-check 的可信基线：

- 角色边界与启动校验：`apps/api/runtime_mode.py`（4 角色、production-like
  predicate、display unsafe-config blockers）
- Slurm 路由按角色不挂载：`apps/api/main.py:310`；`GET /api/v1/runtime/config`
  capability flags：`main.py:283`
- retry/cancel fail-closed
  `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`、queue-depth
  `503 CONTROL_PLANE_QUEUE_UNAVAILABLE`：`apps/api/routes/pipeline.py`
- artifact log
  reader（`published://`/穿越/脱敏/tail）：`services/artifacts/reader.py`；compute 侧发
  `published://logs/...`：`chain.py:4143`
- latest-product / ops strict identity（拒 historical
  fallback、`PIPELINE_STRICT_IDENTITY_MISMATCH`）：Python modules
  `apps.api.routes.forecast`、`apps.api.routes.pipeline`、`packages.common.forecast_store`
- readonly
  DB 探测框架（sim/mock 跑通 + 防 mock 冒充 PASS）：`services/production_closure/readonly_db_validation.py`
- 前端 readonly gating（隐藏控件、no control
  POST、strict 上下文、诊断复制、本地 notified 态）：`apps/frontend`
  monitoring + hydroMet

---

## B. 测试尾巴（本地可做，功能已实现仅缺自动化）

> 这三项不阻塞上线，是契约/测试完备性硬化。已派 subagent 实现中。

| 项  | 内容                                                                   | 落点                                                                    |
| --- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| 2.7 | display retry/cancel `409` + queue `503` 的 OpenAPI 契约 + drift 测试  | `openapi/nhms.v1.yaml`、`main.py:715-733`、`tests/test_api_contract_pipeline_ops.py` |
| 2.8 | retry/cancel 的 gateway-spy + 401/403/409 RBAC 矩阵 + no-write DB 断言 | `tests/test_retry_cancel_consistency.py`                                |
| 3.6 | `JOB_LOG_*` 四个错误码进 OpenAPI + drift 测试                          | `openapi/nhms.v1.yaml`、`tests/test_pipeline_logs_artifacts.py`         |

验证：`uv run ruff check . && uv run pytest -q tests/test_api_contract*.py tests/test_retry_cancel_consistency.py tests/test_pipeline_logs_artifacts.py`。改 OpenAPI 后需
`cd apps/frontend && corepack pnpm run check:api-types`。

---

## C. live 证据（必须在 node-27 实机产出，是「上线」的实质）

C4 producer 已由 #2123 合并并独立保留。#2137 已交付的旧 C1-C3/G8/G0
rollout 合同是历史记录，不访问 node-27、也不产 live
receipt；其冷层 rollout 授权已撤回。以下通用 C1–C4 仍是既有上线/展示 owner 的证据要求，不是 #1895 新任务。

### C1. 部署 receipt（开发期本地起服务，非 docker compose up）

> **部署顺序（migration 先于 display API 重启）**：display API 的 SQL 引用了
> `db/migrations` 里的列，所以**先把待应用的 migration 全部 apply 到 active
> PG（node-27 本机 `:55432`），再重启/拉起 display
> API**。反向顺序会让新代码打到缺列的库上，national tile 与 `/api/v1/layers`
> 全部 500。「提前 apply 是安全的」这句只对**该条 migration 本身是纯 ADD COLUMN
> IF NOT
> EXISTS**时成立（新列没人读，在跑的旧版本不受影响）；**不要外推到全仓**：`000041`
> / `000042` / `000049` 都是
> `DROP INDEX`，提前 apply 会直接改变在跑旧代码的执行计划。逐条判断。
>
> - #2031 的
>   `000057_river_network_version_geometry_generation.sql`（`core.river_network_version.geometry_generation`）属于纯 ADD
>   COLUMN 的安全一类：三条 national tile 路由（两条 hydro-national +
>   `river_network_national_mvt_tile`）与 `/api/v1/layers` 背后的两个 national
>   digest 都投影该列，**必须先 apply 000057，再重启 display
>   API**；apply 后按 C1–C4 对这三条路由 + `/api/v1/layers` 出 live receipt（200
>   / 预期 424，不得 500）。重启会把每个 national cache key 轮换一次（一轮 100%
>   cold miss，自愈；prewarm 覆盖 z=3–5），属预期。
> - **000057 的写侧同样硬依赖该列，且不需要重启就会生效**：`_backfill_output_segment_geometry`
>   （`workers/model_registry/basins_registry_import.py`）在改写 geometry 的同一事务里 bump
>   `geometry_generation`。触达路径有三条：(a)
>   `nhms-node27-autopipe.timer`（每 10 分钟）在**新 basin** seed 时拉起的
>   `import-basins-registry`
>   子进程（默认 backfill）；(b) 同一 timer 对**已 seed**
>   basin 的 display-ready 臂（`_ensure_seeded_basin_display_ready` →
>   `_backfill_output_geometry(only_missing=True)`）；(c) 运维手跑
>   `qhh_production_bootstrap.py`（`only_missing=False`，必 bump）。列不在时
>   `UndefinedColumn` 会让整个 backfill/import 事务回滚 —— (a)
>   basin 不注册、tick 记 `seed_failed` / `stage=import`；(b) tick 记
>   `stage=display_ready`；并每 10 分钟重演。这条路径**只要 `git pull --ff-only`
>   就已经生效**（timer 直接跑仓库里的脚本，没有服务需要重启），所以
>   **000057 必须与这次 pull 同一个窗口 apply，早于下一次 timer
>   tick、早于任何 bootstrap**。geometry 已完整的网络上它是休眠的（`only_missing=True`
>   的路径在 bump 之前就返回 0）。

- [ ] **开发期：27 本地起 display API**（不
      `docker compose up`）：只读派生端口，再启动 wrapper。验证须绑定实际端口、真实运行进程、`/health`、readonly
      runtime 配置和 `/api/v1/slurm/health` 的 404；start
      log 不是 receipt。旧 #1895 C1
      wrapper 待退役；保留的自动观察/身份绑定能力须经 R1 迁到实际 owner，不因撤回冷 rollout 而把日志或静态检查当作运行态 PASS。

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

  `NHMS_DISPLAY_API_PORT` 控制 host 端口，未配置时默认
  `8080`。wrapper 与容器使用同一 `apps.api.main:app` 入口和角色守卫，env 含
  `NHMS_SERVICE_ROLE=display_readonly`。开发期启动快、无镜像构建、无对外容器；用后按 wrapper 输出 PID 停止。

- [ ] 证明 27 无 Slurm CLI/config/socket、无 Docker
      socket、无禁止 mount/env、`/api/v1/slurm/*`
      404、published 只读、`GET /api/v1/runtime/config` 返回
      `display_readonly`：`uv run python scripts/validate_two_node_docker_runtime.py static`（**静态校验 compose/env 而不拉起**，对应 §10.1）+ 对本地服务实机探测（`/health`、`/runtime/config`、`/slurm/health`→404）。
- [ ] **生产部署（非开发期，human-gated）**：`docker compose --env-file infra/env/display.env -f infra/compose.display.yml up -d`
      起持久对外容器——难回滚 + 改状态，须显式人工确认/预授权（与 merge 同治理）；`smoke`（镜像构建）归此阶段。

### C2. 只读 DB denied-write receipt（tasks 5.1/5.2/5.4/5.8）

- [ ] 用 27 真实只读账号设 `NHMS_DISPLAY_READONLY_DATABASE_URL`（或
      `NHMS_READONLY_DB_VALIDATION_DATABASE_URL`），跑 canonical readonly DB
      validation 入口
      `scripts/validate_readonly_db_boundary.py`，产出脱敏 evidence；底层
      `services/production_closure/readonly_db_validation`
      独立保留。凭证和 evidence 必须绑定本次实际运行，私有保存且不得泄露 DSN。旧 #1895
      C2 acceptance wrapper/digest 绑定链已撤回，不是 canonical
      validator 的永久前置；R1 已迁移真正保留的绑定消费者，R3 已删除旧出口：
  - display API（health/models/stations/latest-product/pipeline
    status·stages·jobs·logs/runtime
    config）在只读凭证下 PASS，identity-bound 路由用一个 strict
    `source/cycle_time/run_id/model_id`、logs 绑 `job_id`。
  - permission-denied 矩阵：`hydro/met/ops`
    关键表的 INSERT/UPDATE/DELETE/DDL/TRUNCATE/sequence/schema
    CREATE 全被拒，记录 `current_user` + DB role 类型。
  - 缺真实 DB 时入口必须报 `BLOCKED`，不得 mock 冒充 PASS。
  - 无人值守即可（#2484）：discovery 自己绑定 strict tuple——最新 display-ready
    run（`succeeded`/`parsed`/`published`，可用 `--source` 收窄；给
    `--strict-run-id` 则恰取该 run，不看状态）、该 run 的 `basin_id`（`latest_product`
    按它请求）、该 run 自己最新的带 `log_uri` 的 job；手供 `--cycle-time`/`--model-id`/`--job-id`
    只是逐字段覆盖，须与 `--strict-run-id` 指向同一 run，否则 tuple 自相矛盾（路由 404/409，判
    `BLOCKED`/`FAIL`，不会误判 PASS）。
    identity-bound 路由按字段比对 echo：`source` 大小写不敏感，`cycle_time`
    按 UTC cycle hour（`Z` 与 `+00:00` 拼写等价，不再影响结果），其余精确；echo
    矛盾是 `FAIL`，echo 缺字段是 `BLOCKED`。
  - 运行前先 `set -a; . infra/env/display.env; set +a`，再导出只读 DSN：validator 在进程内驱动本 checkout 的
    app，缺 `NHMS_PUBLISHED_ARTIFACT_ROOT` 时 `job_logs` 会因 `published_root_missing` 判 `BLOCKED`（400
    `JOB_LOG_URI_UNSUPPORTED`），而线上 `:8080` 是有这个变量的（#2484 receipt §3.1）。按源拆跑再合并时，
    per-source run id 必须是 `<合并 run id>-gfs` / `-ifs`，否则合并报 `READONLY_DB_MERGE_SOURCE_PARENT_RUN_MISMATCH`。
  - deny-write 结论读 `summary.json` 的 `lane_statuses.deny_write`（role、permission
    probes、manual-action probes）；`lane_statuses.read_routes` 是路由 smoke 的结论，
    顶层 `status` 仍是两者取最差。

### C2b. 写侧最小权限 receipt（#1774）

C2 证的是**读**边界（`nhms_display_ro`
无写权）。写边界是另一半，2026-09 之前完全缺失：ingest / download / compression
/ retention 及当时计划的 cold-residency 五条 lane 在历史调查中以 superuser
`nhms` 连库，也就是一份凭据 == 数据库容器内命令执行，而这台机器同时对外提供
`https://test.nwm.ac.cn`。

- [ ] **pre-merge（additive，可在 unit 全部照常运行时做）**：从 detached
      worktree 跑
      `bash scripts/node27_provision_write_roles.sh --roles-only`，入证：
      `pg_roles` 中 `nhms_ingest_rw` / `nhms_download_rw` 的
      `rolsuper/rolcreaterole/rolcreatedb/rolreplication/rolbypassrls` 全为
      `f`；两条 `copy-from-program refused for …` NOTICE。此阶段**不做**
      ownership 转移、不取关系锁、不动任何 env 文件。
- [ ] **post-merge（timer 停机窗口）**：跑完整
      `scripts/node27_provision_write_roles.sh`，入证 owner-drift 清单为空、`nhms_display_ro`
      有效 SELECT 集合 before/after 一致、 `relacl` diff（预期只有 grantor 从
      `…/nhms` 改写为 `…/nhms_ingest_rw`）。
- [ ] 存活 lane 各在新角色下跑一轮真实 run 并留 receipt；**不为冷层退役启动 cold
      lane**。R3 已删除 cold-only grant/positive audit，不据本文直接撤销生产权限。autopipe dry tick 的统计守卫
      **两条 ANALYZE 腿都必须是 `ok`**（`warning`
      = 非 owner 被静默跳过，tick 绿而腿死）。
- [ ] env 切换后脱敏 `grep`：`/home/nwm/NWM/infra/env/*.env` 中不再出现 `nhms:`
      DSN 用户名或 `PGUSER=nhms`，例外只有
      `node27-timeseries-compression-replay.env` 与
      `node27-archive-rebuild-drill.env`（migration-class，理由已记档）。

完整口径、退出码与回滚见 `docs/runbooks/tier-node27-timeseries-storage.md` §9。

### C3. cross-plane identity live（tasks 4.3 + §10.2/10.3）

- [ ] 同一个 `run_id/source/cycle_time/model_id/basin_id` 串起：22 生产 →
      DB 状态 → published logs → `/api/v1/mvp/qhh/latest-product` → 27 `/`
      单页地图 + `/ops`，**拒 historical
      latest 冒充**。通用 cross-plane 身份和双源要求保留。原 #1895
      direct-current
      C3 的冷 rollout 已撤回，但独立 C4 生产接受所需的 reviewed-SHA/exact-byte
      digest 校验不能一起撤销：R1.6 必须将该最小职责迁交本清单 C4 的既有 display
      owner（**Bringup-C4 production
      acceptance**）。不迁移 C3 其他已撤销的冷 baseline/完整周期检查，不保留整个旧链。通用 producer-complete
      full-scope/twelve-lane aggregator 仍只接受完整 producer
      bundle，不能用空目录或旧 C3 receipt 冒充。
- [ ] GFS + IFS 双源都过 strict latest/series/ops/logs/browser 才算 cross-plane
      `PASS`；单源为 `PARTIAL`。

### C4. 浏览器 e2e（tasks 6.8 + §10.4）

> M26（EPIC #336）已对**新单页全屏地图**形态产 live browser
> receipt（重定向矩阵 / 全屏无导航 / QHH↔Heihe 同页 zoom /
> overlay 诚实未注册态 = live-PASS，见上「M26」节）。C4
> producer/validator/private
> publisher/binder 已由 #2123 合并，其输入分类优先级由 #2130 晋升为权威规格。独立 C4 判定执行
> `test:e2e:live-c4-display`：以 `/` strict 单页地图 + `/ops` 为准，
> `/hydro-met -> /` 仅作旧别名 redirect smoke。`e2e/monitoring.spec.ts`
> 不是 C4 替代品；撤回 #1895 rollout 不删除或重新分配这些独立展示职责。

- [ ] 既有 display owner 在其批准窗口以真实 node-27 backend 执行
      `test:e2e:live-c4-display`，完成 GFS/IFS 双源 `/` strict bootstrap 与
      `/ops` readonly lane，且 receipt 通过已合并的 C4 schema、semantic
      validator、private publisher 和 binder。
- [ ] 由该 C4 lane 实测 display 模式控件隐藏/禁用、无 retry·cancel·Slurm
      POST、queue-depth
      unavailable、诊断复制、人工 22 恢复指引，并证明 27 只展示 22 的结果而不创建控制面 receipt。
- [ ] **Bringup-C4 production acceptance**
      由既有 display 上线 owner 承担。代码 owner：`services/production_closure/c4_production_acceptance.py`（IO：
      `c4_production_acceptance_io.py`）。公开入口：
      `uv run --no-sync python scripts/node27_c4_production_acceptance.py`。批准来源是操作员已批准的交付/C1 记录上的
      `status=PASS`、`head_sha`、
      `reviewed_sha`；本 CLI 只绑定这些字段，不自批、不代替完整 C1–C3 证明。全量时间戳冻结，不要把 freeze 时刻取整到秒。
      先 freeze，再跨过下一整秒后才记录 `C4_CMD_START`（`sleep 1` 足够），然后执行既有 live-C4 lane，结束后记录
      `C4_CMD_END`，再 bind/verify。私有目录须预先存在且为 euid 0700；目标文件不得已存在。

```bash
uv run --no-sync python scripts/node27_c4_production_acceptance.py freeze \
  --approved-record "$APPROVED_RECORD" \
  --reviewed-sha "$REVIEWED_SHA" \
  --receipt "$C4_RECEIPT" \
  --frontend-origin "$FRONTEND_ORIGIN" \
  --api-origin "$API_ORIGIN" \
  --basin-id "$PLAYWRIGHT_LIVE_C4_BASIN_ID" \
  --segment-id "$PLAYWRIGHT_LIVE_C4_SEGMENT_ID" \
  --output "$C4_FREEZE"
sleep 1
C4_CMD_START=$(/usr/bin/date -u +%s)
test ! -e "$C4_RECEIPT"
cd "$REPO_ROOT" || { echo "BLOCKED: REPO_ROOT unreachable"; exit 1; }
set +e
PLAYWRIGHT_LIVE_BASE_URL="$FRONTEND_ORIGIN" \
PLAYWRIGHT_LIVE_API_BASE_URL="$API_ORIGIN" \
PLAYWRIGHT_LIVE_C4_BASIN_ID="${PLAYWRIGHT_LIVE_C4_BASIN_ID:?current C4 basin pin is required}" \
PLAYWRIGHT_LIVE_C4_SEGMENT_ID="${PLAYWRIGHT_LIVE_C4_SEGMENT_ID:?current C4 segment pin is required}" \
PLAYWRIGHT_LIVE_C4_RECEIPT_PATH="$C4_RECEIPT" \
corepack pnpm@10.11.0 --dir "$REPO_ROOT/apps/frontend" run test:e2e:live-c4-display
C4_CMD_EXIT=$?; set -e
C4_CMD_END=$(/usr/bin/date -u +%s)
test "$C4_CMD_EXIT" = "0"
uv run --no-sync python scripts/node27_c4_production_acceptance.py bind \
  --freeze "$C4_FREEZE" \
  --reviewed-sha "$REVIEWED_SHA" \
  --cmd-start "$C4_CMD_START" \
  --cmd-end "$C4_CMD_END" \
  --output "$C4_BINDING"
uv run --no-sync python scripts/node27_c4_production_acceptance.py verify \
  --freeze "$C4_FREEZE" \
  --binding "$C4_BINDING" \
  --reviewed-sha "$REVIEWED_SHA" \
  --output "$C4_ACCEPTANCE"
```

  缺记录、SHA 不符、字节或文件身份变化均拒绝；本地 C4 binder
  PASS 不替代此外层 gate，也不代替独立 C1–C3 或部署完成。不向 C4 闭集 schema/既有 binder
  CLI 增加 SHA/digest 字段。本登记只覆盖 R1.6 外层接受职责，不声称全部 C1–C3、R1.4 或 epic 已完成。旧冷 G0–G8 不恢复为上线流程。

Issue #389 的 station popup/bbox/framing 与 #342
station-MVT 是独立缺口，不挂在本 C4 checkbox 下。#1970 已交付的河段 click
oracle 仍由上述 C4 lane live 执行。

#### 外部底图 provider：同源缓存代理（#2436 → #2550）

单页地图的底图是天地图（Tianditu）。自 #2550 起，浏览器**不再直连** `t*.tianditu.gov.cn`：
瓦片 URL 是同源的 `/api/v1/basemap/tianditu/{layer}/{z}/{x}/{y}`，由 display API
（`apps/api/routes/basemap.py`）在服务端带 key 取回，并写入文件缓存
`<NHMS_MVT_FILE_CACHE_DIR>/basemap/tianditu/`。

- **key 只放在服务端**：运行时读 `NHMS_TIANDITU_KEY`（写在 `infra/env/display.env`），缺省沿用原来的浏览器端 key；
  前端 bundle 里不再有 key，`VITE_TIANDITU_KEY` 已删除。换 key 只需改 env 并重启 display，不用重新构建前端。
- **上游请求形态**：带浏览器 UA、**不带 Referer**。该 key 的权限类型是「浏览器端」且带域名白名单：
  非浏览器 UA 返回 `301012`，白名单外的 Referer 返回 `301007`，浏览器 UA 且不带 Referer 返回 200
  （[2026-09-20 receipt](receipts/2026-09-20-node27-publish-tick-and-basemap-origin/README.md) §2.1 B1）。
  所以任何 serving origin（`test.nwm.ac.cn`、`nwm.ac.cn`、loopback）拿到的都是同一个结果，
  #2436 那种「loopback 按设计 403」的来源差异已经不存在。
- **失败语义**：上游 429（`302010 该tk已限流`）时，display 返回 503 `BASEMAP_UPSTREAM_THROTTLED`，
  带 `Retry-After: 60` 和 `no-store`，该 worker 在 60 秒内不再为**该图层**请求上游（天地图按图层限流，别的图层照常取）；其它上游失败返回 502
  `BASEMAP_UPSTREAM_UNAVAILABLE` + `no-store`。失败**从不**写入缓存。天地图原本给 429 带的是
  `max-age=432000`，这个头不再到达浏览器。
- **前端兜底**：style 最底层是纯色 `background`；底图 source 出错时，`m11-map-source-error`
  横幅显示一条固定中文提示（不含 URL、不含 key），并且**不覆盖**业务图层的错误。
  C4 lane 仍然把任何 `mapSourceError` 判为不可 PASS（`c4DisplayEvidence/dom.ts:40`），
  这道闸不放松：底图限流期间取不到 C4 PASS 是如实的结果。
- **缓存容量**：`basemap/` 子树不在 MVT retention 的清理范围内（retention 只枚举两位十六进制目录），
  也没有自动淘汰机制；按访问到的瓦片集合增长。node-27 的缓存根在 `/home` 卷
  （2026-09-16 receipt 记为 `/home/nwm/.cache/nhms/mvt`）。容量核查按根 `CLAUDE.md` 用 `df -h` 实测；
  需要回收时可以整棵删除 `basemap/`，删除后只会重新回源。
- **复核 provider** 时，curl 必须带浏览器 `User-Agent`；key **不得**出现在 receipt、日志、截图
  或 issue 评论里。live 复核 display 时直接请求同源代理即可：
  `curl -sI https://test.nwm.ac.cn/api/v1/basemap/tianditu/vec/1/1/0`
  （预期 200 + `X-Tile-Cache: miss|hit`；key 限流期间预期 503 + `no-store` + `Retry-After`）。
  首次 loopback smoke 见 [`receipts/2026-09-22-issue-2550-basemap-proxy-node27/README.md`](receipts/2026-09-22-issue-2550-basemap-proxy-node27/README.md)。

#### ④⑤ 代站/河段 popup live click 证据缺口定义（#389 承接）

> 三类证据严格分离，不得互相冒充：**live MVT closure**（#351→#343，已闭合）/
> **station-MVT 端点** （#342，node-27/display API
> oracle，open；不含 Slurm/SHUD 调度）/ **bbox·framing·popup live
> click 浏览器自动化**（#389，本节）。

要让 #389 可靠自动化 river/station
popup 的 live 点击，需先补齐以下**可被自动化消费的**证据，缺一则 popup live
click 只能人工截图、无法纳入 C4 自动 receipt：

- [ ] **basin/河网 framing
      bbox**：`/api/v1/basins`（及河段/代站列表响应）当前**不返回 geo
      bbox**，浏览器无法据此 `map.fitBounds`
      自动定位到要素再点击。定义所需：列表/详情响应附带要素 bbox（或提供按 id 取 bbox 的轻端点），使 e2e 能确定性 framing。**此数据契约属 node-27/display
      API 侧**，与 #342 station-MVT 协同，非本前端 issue 单独可闭合。
- [x] **WebGL 要素命中 + 河段 framing（river-click 路径，#1970 已交付，2026-09；batch Q 改为真实点击）**：门控的只读
      `window.__nhmsRiverClickEvidence`（仅 `locateRenderedRiver` /
      `armPointerCapture` / `takePointerCapture` 三个方法）只做 fit/命中定位与
      canvas pointerdown 捕获，**不调用任何产品回调**；弹窗由 Playwright
      `page.mouse.click` 在定位点发出的真实（trusted）指针事件经产品 MapLibre
      click 路径打开；配合
      `basin-versions/{id}/river-segments/{segment_id}` 详情响应的 _geom
      bbox_（M11 段详情本就带 geom），河段 river
      popup 的确定性 framing/命中已可自动化（见下方 C4-river-click 节，由独立 display
      owner 产出 live receipt）。**station
      popup 仍无等价路径**（station-MVT 端点/bbox 属 #342/#389 协同侧，未闭合）。
- [x] **node-27 浏览器可启动**（**#431 已解，2026-06-10**）：曾缺
      `libgbm.so.1`/`libxcb-randr.so.0` 导致 chromium `exitCode=127`。已用
      `sudo apt-get install -y libgbm1 libxcb-randr0`（apt 自动带
      `libwayland-server0`）系统级安装（Ubuntu jammy 上 `sudo`
      验证的是调用者 nwm 自身密码，与被禁用的 root 密码无关，无需 root
      SSH）。验证：`ldd .../chromium-1217/chrome-linux64/chrome` 无缺库、
      **不带** `LD_LIBRARY_PATH` 启动 chromium exit 0、master
      `test:e2e:mocked-regression` → 19 passed。临时 `~/pwdeps` userspace
      hack 已清除。live browser lane（含本节 popup live click、
      `e2e/live-display.spec.ts`）的浏览器前提已就绪。
- [ ] 上述就绪后：station popup（forcing 序列）的 live 点击截图 + 断言仍在
      **#389** 未闭（river
      popup 的流量/起报时间 live 点击已由 C4-river-click 节承接，#1970 交付、display
      owner 执行）。

#### C4-river-click：`/` 河段点击 GFS+IFS P95 证据（#1970 → #1895）

> #1970 交付了**门控的只读测试钩子 + 无 mock 的 live
> P95 采集 lane**（代码与本地单测已就绪），本 PR
> **没有**在 node-27 实机执行、**没有**产 live PASS。原 **#1895**
> rollout 执行归属已撤回；下面的独立命令仍由既有 display
> owner 在批准的当前运行中产证。标题中的 #1895 仅保留历史锚点，不是退役的新 live
> acceptance gate。
>
> C4 #389 历史文本（station popup / basin-bbox 的 live
> receipt）保持诚实：river 河段点击的 WebGL 钩子与 segment-detail 几何 framing 已由 #1970 交付；station
> popup 与 basin-bbox 的 live receipt **仍未交付**，属 #389 仍打开的工作。

- **点击机制（receipt schema 1.1，#1970 batch Q）**：每次尝试（warmup + 20）依次为：arm 响应观测 →
  `armPointerCapture()` → `locateRenderedRiver(input)`（fit/idle/16px 唯一命中，另校验定位点
  `document.elementFromPoint` 是地图 canvas、产品自身点击目标解析（代站聚合 → 代站 → 叠加河段 →
  流域面）选中的正是该河段，否则 `HOOK_POINT_OCCLUDED`；钩子**从不**调用 `onOverlayClick`）→
  `page.mouse.click(clientX, clientY)` 恰一次（CDP `Input.dispatchMouseEvent`，trusted 输入，走产品
  MapLibre click）→ `takePointerCapture()`，t0 = 该 trusted pointerdown 的 `timeStamp`（与页面
  `performance.now()` 同一时间原点）。捕获缺失/非 trusted/重复，或捕获点与定位点任一轴相差 > 2 CSS px
  => `CLICK_DISPATCH_INVALID` FAIL；钩子拒绝以 `HOOK_SELECTION_FAILED` 记录并在 message 中保留闭集码
  `hook <CODE>`（如 `hook HOOK_FEATURE_MISMATCH`）。receipt 固定 `click_dispatch=trusted_pointer_event`；
  hook-dispatch 时代的 schema-1.0 receipt（t0 取在直接 `onOverlayClick` 之前）**不可比、binder 拒绑**。
  另一处不可比：`page.mouse.click` 先 move 再 press，move 在 pointerdown 之前就触发了 discharge 河段的
  hover latest-product 预取（`handleMapOverlayHover` → `prefetchHydroMetLatestProducts`），1.0 的直接派发没有这一步。
- **环境（五个键；口径：URL/receipt 缺失 => BLOCKED，pin 缺失/非法 => FAIL）**：
  `PLAYWRIGHT_LIVE_BASE_URL`（27 前端 bare origin，如
  `https://test.nwm.ac.cn`）、`PLAYWRIGHT_LIVE_API_BASE_URL`（27 API bare
  origin）、
  `PLAYWRIGHT_LIVE_RIVER_BASIN_ID`、`PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID`（当前 M11
  pin，来自当 run 的 live identity——`GET /api/v1/basins/{basin_id}/versions` 或
  `GET /api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=<pin>`
  的当前 `basin_version_id`/`river_network_version_id`，**不复制历史证据**）、
  `PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH`（本次运行的**唯一不存在**绝对路径，见下）。缺失 frontend/API
  URL 或缺失/不安全 receipt path => `BLOCKED`（无文件或 BLOCKED
  receipt）；缺失/非法 pin（含空值）或非法 URL/path => `FAIL`（CONFIG_INVALID
  receipt）。禁止设置六个 override 键（出现即 FAIL，即使值为空）：`PLAYWRIGHT_LIVE_RIVER_RUN_ID`、
  `PLAYWRIGHT_LIVE_RIVER_MODEL_ID`、`PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID`、
  `PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID`、`PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME`、
  `PLAYWRIGHT_LIVE_RIVER_SCENARIO`（`PLAYWRIGHT_LIVE_RIVER_BASIN_ID` 与
  `PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID` 是必需的 pin，不在此列）。
- **私有运行目录 + 唯一 absent
  receipt**（当前运行绑定；命令 start/end 括号记录本次 run 的时间窗，receipt 的 mtime 必须落在括号内才是本次产物）：

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

- **exact merged command**（单 worker / 0 retries；浏览器只在
  `page.addInitScript` 里设 `window.__NHMS_E2E_HOOKS__ = true` 后面访问
  `/`，不在 URL 放身份参数；命令从 REPO_ROOT 运行并通过 `pnpm --dir`
  解析 frontend 包（repo root 无 package.json），binder 的 `schemas/...`
  因此可解析）：

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

- **判定**：命令 exit 0 且 receipt 是 schema-1.1
  `nhms-frontend-river-click-live-evidence`（`click_dispatch=trusted_pointer_event`）、父目录 mode 0700、receipt mode
  0600、`status=PASS`、`warmup_count=1`、`accepted_count=20`、
  `percentile_method=nearest-rank`、`p95_ms < 2000`、`failure=null`、`started_at <= ended_at == generated_at`。任何 FAIL/BLOCKED
  receipt 或 exit != 0 都是 **NO-GO**（`p95_ms >= 2000` =
  `THRESHOLD_EXCEEDED`）。发布失败（路径不安全 / 目标已存在 / 身份漂移）必失败并**不覆盖**旧证据。
- **可执行绑定（receipt 接受性 binder，`set -euo pipefail` 下运行；Node-20
  stdlib 单文件，无运行时依赖；仅接受 PASS 终态——任何非 PASS 都拒绝；严格 UTC
  RFC3339 日历合法时间戳，不用宽松 `Date.parse`、不做字典序比较；nearest-rank
  P95 独立重算自实际 durations；缺失/漂移字段的每一行都是 `BINDER:`
  有界固定形状诊断（不回显路径/origin/identity/OS error）并 exit 1）**：

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

- **三网 pin 规则（D4）**：lane 仍是单 pin；验收要对**三个当前产品河网**各跑一次上面的
  prelude → 命令 → binder（每次新的私有 RUN_ROOT 与唯一 absent receipt）。
  - **选网**：`/api/v1/basins` 中 GFS 与 IFS 的
    `/api/v1/mvp/qhh/latest-product?identity_only=true` **都是 200** 的流域，按其当前
    `river_network_version_id` 在 `core.river_network_version.segment_count`（display 只读角色，
    `BEGIN READ ONLY`）排序，取**最大**、**最接近中位数**（在去掉最大/最小后的其余网中取
    |count − median| 最小，平局取 count 小者）、**最小**三个；不足三个 => BLOCKED，不得凑数。
  - **pin**：该网在 `core.river_segment` 中**唯一**一条 `river_segment_id LIKE '%\_shud\_riv\_000001'`
    的河段（当前即 `<basin_id>_shud_shud_riv_000001`，discharge 图层实际渲染的 `…_shud_shud_riv_…` id 族），
    在同一个 `BEGIN READ ONLY` 查询里取出；匹配数 ≠ 1 即 BLOCKED `exit 1`。**不要用 latest-product 的
    `model_id` 构造 pin**：它是部署组 id（如 `dg_be70a045…`），拼出的 id 会 404（2026-09-26 node-27 实测）；
    也不要用 `${basin}_shud` 拼接。使用前 segment detail 必须 200。**不要用 `…_shud_reach_…`**：
    segment detail 对两种 id 都回 200，preflight 能过，但地图 discharge 图层渲染的是
    `shud_riv` id，钩子会以 `HOOK_FEATURE_MISMATCH` 拒绝（lane 记 `HOOK_SELECTION_FAILED`，message
    `hook HOOK_FEATURE_MISMATCH`）。
  - **只读发现命令**（证据落在私有 PIN_DIR，随 receipt 一并记录；`set -euo pipefail`，
    BLOCKED 分支（不足三网或某网 `_shud_riv_000001` 匹配数 ≠ 1）或任一 pin 的 segment detail 非 200
    都会 `exit 1` 停下，不会带着坏 pin 继续）：

```bash
set -euo pipefail
REPO_ROOT="/home/nwm/NWM"
API="${PLAYWRIGHT_LIVE_API_BASE_URL:?set the bare API origin first}"
PIN_DIR=$(mktemp -d "$REPO_ROOT/.nhms-issue1970-riverclick-pins-XXXXXX")
chmod 0700 "$PIN_DIR"
curl -fsS --max-time 30 "$API/api/v1/basins?limit=500" > "$PIN_DIR/basins.json"
node -e 'for (const b of JSON.parse(require("fs").readFileSync(0, "utf8")).data) console.log(b.basin_id)' \
  < "$PIN_DIR/basins.json" > "$PIN_DIR/basin_ids.txt"
: > "$PIN_DIR/product_networks.tsv"
while read -r BASIN; do
  G=$(curl -sS --max-time 30 -o "$PIN_DIR/gfs-$BASIN.json" -w '%{http_code}' \
    "$API/api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=$BASIN")
  I=$(curl -sS --max-time 30 -o "$PIN_DIR/ifs-$BASIN.json" -w '%{http_code}' \
    "$API/api/v1/mvp/qhh/latest-product?source=IFS&identity_only=true&basin_id=$BASIN")
  if [ "$G" = "200" ] && [ "$I" = "200" ]; then
    RNV=$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync(0, "utf8")).data.river_network_version_id)' \
      < "$PIN_DIR/gfs-$BASIN.json")
    printf '%s\t%s\n' "$BASIN" "$RNV" >> "$PIN_DIR/product_networks.tsv"
  fi
done < "$PIN_DIR/basin_ids.txt"
RNV_LIST=$(cut -f2 "$PIN_DIR/product_networks.tsv" | sort -u | paste -sd, -)
# display_readonly runtime role (nhms_display_ro), read-only SELECT; no writer credentials
( set -a; . "$REPO_ROOT/infra/env/display.env"; set +a
  psql "$DATABASE_URL" -X -q -At -F "$(printf '\t')" -v ON_ERROR_STOP=1 -v rnvs="$RNV_LIST" <<'SQL'
BEGIN READ ONLY;
SELECT v.river_network_version_id, v.segment_count,
  (SELECT min(s.river_segment_id) FROM core.river_segment s
   WHERE s.river_network_version_id = v.river_network_version_id
     AND s.river_segment_id LIKE '%\_shud\_riv\_000001'),
  (SELECT count(*) FROM core.river_segment s
   WHERE s.river_network_version_id = v.river_network_version_id
     AND s.river_segment_id LIKE '%\_shud\_riv\_000001')
FROM core.river_network_version v
WHERE v.river_network_version_id = ANY (string_to_array(:'rnvs', ','));
COMMIT;
SQL
) > "$PIN_DIR/network_pins.tsv"
node - "$PIN_DIR" > "$PIN_DIR/pins.tsv" <<'JS' || { echo 'BLOCKED: pin discovery did not produce three pins' >&2; exit 1; }
const fs = require('fs')
const dir = process.argv[2]
const lines = (name) => fs.readFileSync(`${dir}/${name}`, 'utf8').split('\n').filter(Boolean).map((line) => line.split('\t'))
const networks = new Map(lines('network_pins.tsv')
  .map(([rnv, count, pin, matches]) => [rnv, { count: Number(count), pin, matches: Number(matches) }]))
const rows = lines('product_networks.tsv')
  .map(([basin, rnv]) => ({ basin, rnv, ...networks.get(rnv) }))
  .filter((row) => Number.isInteger(row.count))
  .sort((a, b) => a.count - b.count || a.basin.localeCompare(b.basin))
// Pin: the network's single discharge-layer `…_shud_riv_000001` segment in core.river_segment.
for (const row of rows) {
  if (row.matches !== 1) { console.error(`BLOCKED: ${row.rnv} has ${row.matches} _shud_riv_000001 segments`); process.exit(1) }
}
if (rows.length < 3) { console.error('BLOCKED: fewer than three product networks'); process.exit(1) }
const n = rows.length
const median = n % 2 ? rows[(n - 1) / 2].count : (rows[n / 2 - 1].count + rows[n / 2].count) / 2
const mid = rows.slice(1, -1).reduce((best, row) => (Math.abs(row.count - median) < Math.abs(best.count - median) ? row : best))
for (const [role, row] of [['largest', rows[n - 1]], ['median', mid], ['smallest', rows[0]]]) {
  console.log([role, row.basin, row.pin, row.count].join('\t'))
}
JS
while IFS="$(printf '\t')" read -r ROLE BASIN SEG COUNT; do
  BV=$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync(0, "utf8")).data.basin_version_id)' < "$PIN_DIR/gfs-$BASIN.json")
  RNV=$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync(0, "utf8")).data.river_network_version_id)' < "$PIN_DIR/gfs-$BASIN.json")
  CODE=$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' \
    "$API/api/v1/basin-versions/$BV/river-segments/$SEG?river_network_version_id=$RNV")
  echo "$ROLE $BASIN $SEG segment_count=$COUNT detail=$CODE"
  test "$CODE" = "200" || { echo "FAIL: $ROLE pin $SEG segment detail returned $CODE" >&2; exit 1; }
done < "$PIN_DIR/pins.tsv"
```

- **三 receipt 验收**：对 `pins.tsv` 的三行（largest / median / smallest）逐行设置
  `PLAYWRIGHT_LIVE_RIVER_BASIN_ID=<basin>`（第 2 列）、`PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID=<pin>`（第 3 列，即该网唯一的 `…_shud_riv_000001`），
  各自重新执行上面的私有运行目录 prelude、exact merged command 与 binder（`set -euo pipefail`）。
  **三次都必须 `BINDER: PASS`**（schema 1.1、`click_dispatch=trusted_pointer_event`、warmup 1 +
  20 samples、`p95_ms < 2000`）门才算过；任一 FAIL/BLOCKED 即 NO-GO，如实记录，不得重跑到绿。完整样本下的
  `THRESHOLD_EXCEEDED` 是产品发现；`CLICK_DISPATCH_INVALID` / `HOOK_*` / `SERIES_REQUEST_INVALID`
  是机制缺陷，须修复。
- **边界**：本 lane 只访问 `/`；`/monitoring` 原文案不变，`/ops`
  不在本 metric 内。

---

## 主机容量纪律（每次上 27 干活之前，#1765）

- [ ] 容量核查三个挂载点一起看，**`/` 不能漏**：

  ```bash
  df -h / /home /data/GHDC
  ```

  `/` 只有几十 GB 且以前无人自动看守：一次跨两天的 pytest 用
  `/tmp/pytest-of-nwm` 把它塞满，直接阻塞了当时 PR 的 live
  receipt。当前宿主 PGDATA 是 `/data/GHDC/nhms-primary/pgdata`，容器 bind 仍为
  `/home/postgres/pgdata/data`；工作集峰值须比较配置目标的实际设备与可用字节，不能借用独立
  `/home` telemetry，目标不可用时也不能回退到 `/home`。 `/home`
  保留旧副本及临时文件等 residual use，不是当前 PGDATA；冷层仍未启用。Issue
  #2273 的源码修正不表示旧 pinned
  runtime 已部署修正或服务健康，I8 前须取得批准的目标容量证据（口径见
  `docs/runbooks/current-production-ops.md`）。

- [ ] 在 27 上跑 pytest 之前先把临时根挪出 `/`：

  ```bash
  mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp   # 建议写进 nwm 的登录 profile
  ```

  `mkdir -p` 不能省——`TMPDIR` 指向不存在的目录时 Python 会**静默回落**到
  `/tmp`，于是「设了但没生效」和「设了且生效」看起来一模一样。跑完用
  `ls -d /home/nwm/tmp/pytest-of-nwm` 确认落点，别只看
  `df`。仓库侧的另一半（`pyproject.toml` 的
  `tmp_path_retention_policy = "failed"`）已经在代码里，绿的会话不留残留；**不要**在共享配置里加
  `--basetemp`。

- [ ] 资源治理审计的告警链已部署（`install` +
      `systemctl --user daemon-reload`）：
      `nhms-node27-resource-governance.service` 必须带
      `OnFailure=nhms-node27-unit-failure-alert@%n.service`，审计遇到 `critical`
      建议时 exit 1 并向 journal 打 `RESOURCE_GOVERNANCE_CRITICAL:<code>`。

  ```bash
  systemctl --user show nhms-node27-resource-governance.service -p OnFailure
  ```

  **装 `OnFailure=` 之前先看有没有长期 `critical`**：只要还有一条 `critical`
  建议没消，这个 unit 就会**每个每日 tick 都 exit 1**——按设计一直挂在
  `systemctl --user --failed`
  里并且每次都发一封信（告警处理器是刻意做傻的，没有去重、没有状态）。让它安静的办法是把条件清掉，不是压制告警。所以先读最新的一份 receipt 确认当前没有
  `severity: critical`：

  ```bash
  ls -t /home/nwm/node27-resource-governance-logs/resource-governance-*.json | head -1 \
    | xargs -r grep -c '"severity": *"critical"'
  ```

  timer 是 `OnCalendar=*-*-* 04:10:00 UTC`，所以「每个 tick」就是每天一封。

---

## 上线判定

- **B 全绿** + **C1–C4 全部产出 live receipt** → 27 节点可声明上线。
- C 的归因区分（`environment-only`/`production-config`/`data-contract`/`code-contract`）记入
  `docs/bugs.md`。
- 注意：cross-plane（C3）依赖 22 侧有真实双源 cycle 产出（已业务化具备），以及 published
  artifacts 已 copyback 到 27 可读路径（`progress.md` §「仍需 live proof」中
  `NHMS_PUBLISHED_ARTIFACT_ROOT` 由 22 私有 staging 切 `/ghdc`
  的那一步是前置）。
