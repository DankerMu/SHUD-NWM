# 项目进度

> 跨 session 继承的进度索引：只记**当前到哪了 + 边界 + 入口**。逐 commit/逐缺陷细节以 GitHub issue、PR、OpenSpec、runbook 为准，不在此重复。

## 最新（2026-06-08）

**M26 统一地图展示交付**（`openspec/changes/m26-unified-map-display/`，EPIC **#336 已关闭**，子 issue #337–#341 全合并，tasks 7 节全勾）——node-27 `display_readonly` 展示端从 ~10 条路由 + 顶部导航的碎片化（含 2496 行 M21 玩具页 `HydroMetPage`）收敛为**字面一张全屏地图 = 整个展示端**：

1. **去导航 + 路由收敛**（#337）：`AppShell` 删 `NavBar`，全屏布局（`--m11-nav-height`→0）；`/overview`/`/forecast`/`/hydro-met`/`/meteorology`/`/flood-alerts`/`/basins/:id`/`/segments/:id` 全 `replace` 重定向到 `/`，**保留原始 search + 附加语义参数**（`layer=`/`basinId=`/`segmentId=`，同名键以原始 search 为准）；
   `/ops`/`/monitoring`/`/system/model-assets` 经 RBAC 保留可达。
2. **总览↔详情就地化**（#338）：M11 query 新增 `basinId`，`overviewData` store 的 basinId **改从 query 读**（不再依赖路由 param），单页按 `basinId` 双模式（null=全国总览 / 非 null=流域详情同图 zoom-in），删 `BasinDetailPage` 路由 + 文件。
3. **气象代站 clustered-GeoJSON 图层**（#339）：新 `stationLayerData` store + `M11StationClusterPrimitive`，按选中流域 latest-product 严格身份取 `/met/stations`；**超 500 站流域（Heihe 1709）分页拉取至 client cap，诚实暴露 `total/loaded/truncated`**；全国无 basinId 显"选择流域"honest 空态；为 station-MVT 预留 source 抽象。
4. **两类地图 popup**（#340）：点河段→`q_down` 曲线 + 重现期三态；点代站→六要素 forcing 曲线；maplibre `Popup` 内嵌 echarts，复用 honest-display 校验（不画假曲线、`ok:false` 空态、strict identity）。
5. **删玩具页**（#341）：删 `HydroMetPage`（2496 行）+ 专属测试，迁移 honest-display 库（`bootstrap/stationSeries/riverForecast/ReturnPeriodSection`）供 popup 复用，river 诚实展示覆盖迁移。
6. **node-27 live receipt**（`worklogs/node27-live-receipt.md`，`execution_mode=live_proof`）：①重定向矩阵 7/7 ②全屏无导航 ③QHH↔Heihe 同页 zoom（pathname 恒 `/`）⑥overlay 未注册如实显示「Layer is not registered」=**live-PASS**（均为本地 vitest 无法验、仅 live 可证之项）；
   ④⑤ popup 绘制不变量本地单测全覆盖 + 数据 live 就绪，**live 点击证据由 #389 单独承接**（`/api/v1/basins` 无 bbox 无法自动 framing + CLI 难命中 WebGL 要素），不再归为 live MVT 开关/根因问题。
7. **live MVT closure（#351 → #343）**：#351 已用 2026-06-08 node-27 live receipt 闭合 #343：`NHMS_ENABLE_LIVE_POSTGIS_MVT=true` 后 `/api/v1/layers` 返回 5 个 live layer，`hydro-national/q_down` tile 200；原 river-network 424 / hydro 409 根因是 display readonly 未启用 live PostGIS MVT 和图层未注册。
8. **解耦平行 issue（不在本变更）**：**#342** 后端 station-MVT 点图层端点（全国万级代站，仿 river-network `ST_AsMVT`，node-22 oracle）仍 open；**#389** 承接 bbox/framing/点击自动化/popup live click 浏览器证据缺口；二者均和 #343 已闭合的 live MVT flag/root cause 分开跟踪。
9. **边界**：当前 2 流域规模（QHH 386 站/1633 河段、Heihe 1709 站/2352 河段）用 M11 既有 GeoJSON 河网渲染；全国级（数万代站）仍依赖 #342 station-MVT；④⑤ live 点击截图待 #389 补齐 bbox/framing 与 WebGL 命中证据。

**M25 多流域前端生产化交付**（`openspec/changes/m25-multibasin-frontend-production/`，9 子 issue：#310–#317 已合并，#318 本 PR 收尾）——node-27 `display_readonly` 前端去 QHH 硬编码、按数据驱动：

1. **后端动态发现 + 去硬编码**：`list_basins` 增 `has_display_product`（EXISTS run-status 集合过滤，复用 `QHH_LATEST_READY_RUN_STATUSES` 单一口径，**无 basin_id 白名单**）（#310）；latest-product 去 `QHH_BASIN_ID` 写死、`basin_id` 参数化（缺省 `basins_qhh` 向后兼容 + 旧 `/mvp/qhh/latest-product` 路径保留）（#311）；
   河段/站点列表 `search`+分页+`variable`/`stream_order` 字段可用性降级契约（#313）。
2. **return-period 诚实展示**：`availability.return_period_status`（ready/unavailable）作**独立 supplemental 字段**，不进 blocking reasons（有 q_down 无洪频基线的产品不掉 ready/404）（#312/#316）；无真实产品时仅静态图例 + "暂未发布正式产品"，不渲染假数据。
3. **前端生产化**：流域选择器数据驱动（无前端白名单，新流域自动出现）+ 切流域以 `basin_id` 重拉、strict identity 一致（#314）；河段/站点列表走后端 search/分页（#315）；`/ops`+`/monitoring` 入口按 `display_readonly` 降级（保留 `/meteorology` 门控）（#317）。
4. **可扩展性验证 + 文档收尾**（#318）：真 DB 集成测试断言「全新注册 basin 仅靠数据即在发现接口出现，零代码改动」（`tests/test_real_basin_discovery_integration.py`），前端 `BasinSelector.test.tsx` 数据驱动断言新流域自动渲染。
5. **边界**：以上为**功能交付**；node-27 实质上线仍是 **C1–C4 live receipt**（部署/只读 DB denied-write/cross-plane identity/浏览器 e2e），见 `docs/runbooks/node-27-bringup-checklist.md`，属后续。

**node-27 开发地基就位**（master 直推 `661da44`）——22 业务化稳定后转入 27（`display_readonly`）前端生产化开发，先把流程/文档/CI 铺稳：

1. **m25 change**（`openspec/changes/m25-multibasin-frontend-production/`，多流域前端生产化）已 spec 化并过 codex 三路审核——**P0 修正**：洪水重现期可用性从"进 blocking reasons"改为 `availability.return_period_status` **独立 supplemental 字段**（否则有 q_down 无洪频基线的产品会整体掉 ready/404）；已拆 GitHub issue（并行起点 #310/#311/#313/#317）。
2. **三节点协作固化**：`CLAUDE.md` 双端→三端（27=`nwm@…27:/home/nwm/NWM`），新增**验证 oracle 路由**（后端→22 真 DB pytest / display→27 live receipt / 本地→ruff·openspec·pnpm）+ 远端 ff-only 同步纪律；`dual-end-issue-workflow` skill 同步扩成"双 oracle"（含 node-27 recipes：**开发期本地起服务非 docker、fail-closed ff 恢复**）。
3. **CI 按路径 scope**（`ci.yml`，`dorny/paths-filter`）：纯前端/docs PR 跳过 16min 后端 pytest；
   **draft PR=定向快速通道 / ready·master=全量合并门**（忘标 draft→默认全量，fail-safe）；
   `concurrency` cancel-in-progress；历史 M15 visual evidence 已移到显式手动 workflow，属于 mocked 视觉证据
   而非 node-27 live proof。master 无 branch protection=**人工合并门**。约定见 `CLAUDE.md`「CI 范围与门控」。
4. **node-27 已追平 master**（`/home/nwm/NWM`），2026-05-27 生产 E2E 记录已 `git stash` 保全。
5. **27 上线 = C1–C4 live receipt**（部署/只读 DB denied-write/cross-plane identity/浏览器 e2e），清单见 `docs/runbooks/node-27-bringup-checklist.md`。

## 2026-06-06

**业务化运行韧性硬化批次**（master 直推 + PR #308）——node-22 业务化运行后回传一批生产化改造：

1. **多源下载链**（PR #308）：GFS 换 NODD 多镜像（`s3,gcs,azure,ftpprd,nomads`），IFS 云镜像优先（`aws,azure,google,ecmwf`，ECMWF 直连末位）；NOMADS 403=动态封禁→持久断路器，云镜像 503/429→切源 + per-source cooldown。**从根上消解单源静默丢 cycle**（旧 §3B 缺陷③）。env/语义详见 `docs/runbooks/qhh-continuous.md`。
2. **有序生产调度器重试 operationalize**：可复用 auto-retry job + 冲突守卫；新 env `NHMS_SCHEDULER_LOCK_BACKEND=postgres`、`NHMS_SCHEDULER_CYCLE_LAG_HOURS=6`。
3. **SHUD state/forecast 输出加固**：`state_qc` 分节解析、warm-start chaining。
4. **forecast 天气摄取语义**：GFS APCP cumulative vs interval_bucket 区分 + 去累积 gap fail-loud + f000 行语义。
5. **实时监控 `nhms-monitor`**：cycle/Slurm/scheduler 三段 + 阈值告警 → JSON 快照（schema `nhms.live_monitoring.v1`），用法见 `docs/runbooks/qhh-22-business-bringup.md` §5。

上一节点（m23 §255）：通用 daemon 已自驱全新 cycle 的 canonical 摄取（零行的全新 cycle 整链从 download 起跑，有行但不完整仍硬 block）；node-22 oracle 626 passed，live receipt 待 gateway 部署。

## QHH node-22 业务化（live，进行中）

- **GFS / IFS 双源 7 天全流程已 live 跑通至 `frequency_done` 并 publish**（诊断 lane，cycle 2026060400，`s3://nhms`）：download→canonical→forcing→SHUD→parse→frequency→publish；return_period 诚实标注、无伪造洪水位。**双流域（qhh+heihe）并发 published receipt** 亦取得（#291，流量 display 口径）。
- **科学链已验证正确**（worker 链），但**正式连续业务化在 m24 通用 daemon 上重做并 live**——上述跑通走的是 `run_qhh_continuous.py` **诊断脚本**（m23 已否决其作生产，保留为排障 lane + `DIAGNOSTIC-ONLY` 头 + 护栏测试 #293）。
- **m24 转向**（epic #285，子任务 #286–#293 + #300）：生产路径 = 通用 scheduler/chain daemon 经独立 Slurm gateway。三道硬坎 = ① gateway 在 node-22 部署（#288，从未 live）② 跨周期暖启动 path(b) 短 analysis 段（#289）③ 并发 submit-and-return + durable reservation（#290，代码已合并，未 live）。
  **部署前置**：迁移 `db/migrations/000029_pipeline_reservation.sql` 必须在 #292 go-live 前 apply（node-22 prod DB 尚未 apply）。
- **业务化数据 source-of-truth（硬约束）**：每个流域参数以 `data/Basins/<流域>/input/<流域>/` 真实 SHUD 模型为唯一真值（`.sp.riv` 河道=产品/输出层 1633、`.sp.rivseg`/`seg.shp` GIS 层、`.cfg.para` 参数…）；注册与运行参数一律从此派生，**严禁手配/即兴覆盖**。
- ⚠️ **已知待修（不阻塞）**：`QHH_FORCE_UPSTREAM` 未透传进 sbatch（兜底删 canonical 行触发重转，runbook §6/§9；m24 改走 chain 自动重转消解）；`curve_duration` 跨标签 re-run 孤儿 peak 行（fresh 跑不受影响）。
- ⚠️ **运行纪律**：作业运行中**禁止在 node-22 `git pull`**（换 inode 触发 NFS stale handle 杀作业）。
- 运行手册：[`docs/runbooks/qhh-22-business-bringup.md`](docs/runbooks/qhh-22-business-bringup.md)、[`docs/runbooks/qhh-continuous.md`](docs/runbooks/qhh-continuous.md)。m24 细节见 [`openspec/changes/m24-multibasin-continuous-daemon-live/`](openspec/changes/m24-multibasin-continuous-daemon-live/)。

## 已完成里程碑

- **M21** QHH 水文气象展示 + 运维监控 MVP：Epic #202 关闭，PR #226 合并（`ec5d535`）。
- **M22** 两节点 Docker 只读展示重构：Epic #227 关闭，PR #250 合并（`08c72e9`），CI 全绿。
- 两节点角色边界已落地：22=`compute_control`（scheduler/Slurm/发布/retry），27=`display_readonly`（只读消费 DB + published，`/hydro-met`+`/ops`，无 Slurm/Docker socket/控制面 credential）。
- MVP 范围：QHH/有限流域、GFS 主源 + IFS 并行源、河段流量 `q_down`、forcing 代站 `PRCP/TEMP/RH/wind/Rn/Press`、pipeline 运维闭环。**不**声明全国流域 / CLDAS / ERA5 近实时 / 全国 MVT/PBF / final production ready。

## 仍需 live proof（正式上线前）

目标环境 receipt：PostgreSQL/PostGIS/TimescaleDB（含 27 readonly denied-write probes）、对象存储/published artifacts（`log_uri` 指向 27 可读 URI）、两节点部署角色、
cross-plane identity（同一 `run_id/source/cycle/model/basin` 串起 22 生产→DB→published→`/hydro-met`+`/ops`）、live Slurm 全套、live `/hydro-met` + `/ops` browser run、alert sink/rollback/nationwide MVT。
逐项归因见 [`docs/bugs.md`](docs/bugs.md)。

## 入口

- 文档权威状态与冲突解决顺序见 [`docs/governance/DOC_STATUS.md`](docs/governance/DOC_STATUS.md)。
- 证据索引：[`docs/runbooks/qhh-mvp-smoke-evidence.md`](docs/runbooks/qhh-mvp-smoke-evidence.md)、[`docs/runbooks/two-node-production-e2e-plan.md`](docs/runbooks/two-node-production-e2e-plan.md)、[`docs/VALIDATION.md`](docs/VALIDATION.md)
- 常用验证：

  ```bash
  uv run ruff check . && uv run pytest -q
  openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive
  (cd apps/frontend && corepack pnpm test && corepack pnpm build)
  uv run nhms-pipeline plan-production --dry-run --source gfs --source IFS
  uv run nhms-monitor          # 实时生产健康快照（需 DATABASE_URL）
  ```

## 操作注意

- 不要误 stage 本地/生成物：`.agents/`、`.codex/`、`data/`、`node_modules/`、`dist/`、`__pycache__`、`artifacts/`（`artifacts/` 保持 ignored）。
- 生产 Linux 环境不复用他机 `.venv`/`node_modules`，按 `AGENTS.md` 在目标机重新 `uv sync` / `pnpm install --frozen-lockfile`。
- 完成度判断以源码、测试、`docs/VALIDATION.md`、runbook 和本文为准；历史 OpenSpec 任务状态仅供审计。
