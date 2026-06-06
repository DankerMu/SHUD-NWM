# 项目进度

最后更新：2026-06-06，m23 §255 摄取接线收口：通用 daemon 现已**自驱全新 cycle 的 canonical 摄取**——scheduler 对零 canonical 行的全新 cycle 不再硬 block，而是放 `restart_stage=None` 整链 cohort 让 chain 从 download 起跑（download→convert→forcing→forecast→parse→frequency→publish，经 Slurm gateway）；有行但不完整仍硬 block，空 horizon/provider 不可用不误判 fresh。分支 `feat/m23-255-generic-canonical-ingestion`，node-22 oracle 626 passed；live 端到端 receipt 待 gateway 部署后补。这补上了 #287 当初乐观标注但实际缺失的 daemon 摄取自驱（#292 曾 `submitted_count=0` blocked）。

上一里程碑：2026-06-05，m24 §3B 多流域 live：通用编排器双流域并发链路打通 + 抓出并修掉 6 个真实缺陷，**已取得 qhh+heihe 双流域 published receipt**（流量 display 口径，cycle gfs_2026060500 → `complete`）。

用途：作为跨 session 继承的项目真实进度索引。本文只保留当前完成态、仍需 live proof 的边界和常用验证入口；历史 review 细节以 GitHub issue、PR、OpenSpec 和 runbook 为准。

## QHH node-22 业务化（live，进行中）

- **首个真实端到端 cycle 已跑通 publish**（24h 冒烟，job 5980）：download→canonical→forcing(386 站)→SHUD→parse(qc_passed)→published_for_display(1633 段，return_period 诚实标 `no_frequency_curve`，无伪造洪水位)。
- **GFS 7 天 / 5min 全流程已跑通**（cycle 2026060400, s3://nhms）：`hydro.hydro_run` 状态 `frequency_done`（洪频结果已生成，display-products 已发布）。
- **IFS 7 天 / 10min 全流程已跑通**（job 6004, cycle 2026060400, s3://nhms）：`frequency_done`，river_timeseries 1,381,518 行 × 1633 段，return_period `('1h',True,1633)+('1h',False,1381518)`。
- IFS canonical 重转后 384 产品全 `ok`（修复前 382 ok + 2 `warning_negative_precip`），**8a8ba3d precip 量化容差修复在真实 IFS cycle 验证通过**（forcing 不再缺 prcp）。
- **已落地修复**（master）：sp.riv 多块解析（17f5229）、GFS APCP 6h 桶去累积（142dff0）、原生变步长预报 GFS/IFS（b642b86/eaf5649）、forcing manifest 默认 2MB→32MB（a58c25a）、cycle 脚本 state 文件保留 `slurm_job_id`（1baee15）、SHUD 输出间隔默认 180→5min（81e50ff）。
- **近期修复**：stage-skip 门 + 10min 默认 + return_period 批量 INSERT（d29d370/539f793）；GFS/IFS forcing 变量质量处理对齐 SHUD/rSHUD 约定（rn/rh 钳位、precip/shortwave GRIB 量化负值容差，8cced52）；xhigh 全面复审 39 项 CONFIRMED 整改 + IFS precip 量化容差（8a8ba3d）。
- **对象存储切换**：废弃 e2e 标签 `s3://nhms-22-e2e`，DB+文件系统 e2e 数据已清除（残留 0），前缀切 `s3://nhms`。
- **GFS 尾段刷新（方案A）已完成**：用修正后的 return_period（`curve_duration` peak 行 `167h→1h`）重跑 frequency/publish，不重算 SHUD。
- ⚠️ **已知待修（不阻塞，均记后续 issue）**：
  - ① `QHH_FORCE_UPSTREAM` 未透传进 sbatch（continuous runner `--export` 仅带 `DATABASE_URL`），改 converter 逻辑后旧 canonical 不自动重转，当前以删除 `met.canonical_met_product` 行触发重转兜底（runbook §6/§9）。
  - ② `curve_duration` 标签由 `167h→1h` 后，`_delete_all_prior_peaks` 按新 duration 删除清不掉旧标签孤儿 peak 行（仅跨标签 re-run 出现，fresh 业务跑不受影响），GFS 方案A 已手动清理 1633 行。
- **架构转向（m24，已立项）**：上面的双源 live 跑通走的是 `run_qhh_continuous.py → run_qhh_cycle.sh` **诊断脚本**（m23 design.md 已否决其作生产自动化）；**通用编排器**（`services/orchestrator` scheduler/chain + Slurm HTTP gateway）虽代码就绪但**从未 live**（m20 0/33）。本轮"全持续守护"改造 = 把业务化迁到通用 daemon,瞄准**多流域通用 + 并行发起任务 + 跨周期暖启动承接**。
  - 三道硬坎:① Slurm HTTP gateway 在 node-22 从未部署(通用 chain 只走 gateway)② 暖启动 forecast→forecast 有时间语义缺口(`nhms-state save` 存预报窗末 `end_time`、非下周期 init time;现状每周期从固定打包标定态起跑、无水文记忆)→ m24 走 path(b) 短 analysis 段 ③ 并发 submit-and-return 是净新增(现状顺序 cohort)。
  - OpenSpec：[`openspec/changes/m24-multibasin-continuous-daemon-live/`](openspec/changes/m24-multibasin-continuous-daemon-live/)(`openspec validate --strict` 通过;经三轮 codex 复审含 full-output 并行收敛)。跟踪 **epic #285** + 子任务 #286–#293(§0 baseline→§P 依赖门→§1 gateway→§2 暖启动→§3A 并发→§3B 多流域 live→§4 daemon→§5 诊断退役)。
  - **§3A 并发 submit-and-return + durable 两阶段预留(#290)代码已完成并合并**：锁内 sbatch 前写持久预留行(`pipeline_job` + `idempotency_key`,partial unique index)、提交后原子 bind `slurm_job_id`、reclaim 原子接管死预留；防双提交跨重叠 pass + 提交崩溃窗口(crash-after-sbatch-before-bind)经 reconcile-by-comment(`sacct --comment=nhms_idem:<key>`,array master 行归一化到裸 id)恢复;grace-gate 锚 `updated_at` 防 slurmdbd 滞后误把 in-flight 预留降级。**尚未 live**——overlapping-submit 实况 receipt 待 #292 daemon go-live。**部署前置**：迁移 `db/migrations/000029_pipeline_reservation.sql`(预留列 + 部分唯一索引)必须在预留代码上线 / #292 go-live 前 apply(node-22 prod DB 尚未 apply)。out-of-scope LOW 收尾 → #300。
  - **诊断脚本暂留作 bring-up 回退与排障**,不再以其声称 production;待 m24 落地后退役(护栏 #293)。
  - **§5 诊断退役 + 文档(#293)已落地**:生产路径 = 通用 daemon(`plan-production --continuous` → `services/orchestrator/scheduler.py` `run_continuous`,经独立 Slurm gateway);三个 QHH 脚本(`run_qhh_cycle.sh`/`run_qhh_continuous.py`/`create_qhh_shud_manifest.py`)**保留**为诊断/排障 lane 并加 `DIAGNOSTIC-ONLY` 头(含具体 smoke 命令 + 最小 PASS 条件)。护栏测试 `tests/test_qhh_scripts_static.py::test_production_scheduler_does_not_invoke_qhh_diagnostic_scripts` 静态扫 `services/orchestrator/*.py`,断言生产 scheduler/chain 零引用这三个脚本 + 运行时 manifest 组装在 chain(`_build_forecast_runtime_manifest`)。runbook/infra(env/compose/systemd)文档已标注 production daemon + gateway contract。
  - **§3B 多流域 live(2026-06-05,#291)——机制打通 + 抓出并修 6 个真实缺陷 + 双流域 published receipt**:通用编排器在 node-22 真实 Slurm 上把 qhh+heihe **双流域并发**跑过完整链路(download→canonical→forcing→SHUD→parse→frequency→publish),per-basin 身份/array 路由/partial 隔离均 live 验证正确(forecast 一流域失败、survivor 续走、publish 按 per-basin 发 residual_blocker 零泄漏)。EF real DB 2585 passed。抓出真实缺陷:
    - ①heihe forcing 站点 PK 未按 project 命名空间化(82a137e)②cfgrib 非包坏、是手动路径漏注入 `LD_LIBRARY_PATH=$NHMS_GRIB_ENV_ROOT/lib`(runbook 已补 + #292 §4.5 preflight)③NOMADS 403 瞬时限流被硬标 `retryable=False`→静默丢 cycle(已修 `retryable=True`)④gateway `get_job_status` 解析不了 Slurm array 父 ID→`JOB_NOT_FOUND` 阻断所有 array 阶段(已修:聚合 `<jobid>_<N>` member 行成父状态)。
    - ⑤**注册保真缺陷(已修,业务化核心)**:通用编排 forecast `verify_output` 被两流域输出列数失配阻断(qhh 期望 3739 列、实得 1634)。根因——通用注册只 seed `seg.shp`/`.sp.rivseg` 几何层(qhh 3738 / heihe 4759)、漏 SHUD forecast 实际算/输出的 **`.sp.riv` 河道层**(qhh **1633** / heihe **2352**),`verify_output` 拿 `segment_count+1` 拒掉**正确**输出。诊断流一直按 `.sp.riv` 发布 1633 段(见本节首条 job 5980 + `river_timeseries × 1633 段`),证明 1633 才是 qhh 产品真值。**修(8cf7130)**:`basins_geometry` 暴露 `output_segment_count`、`basins_registry_import` seed `shud_output_river=true` 输出层(id `{model_id}_shud_riv_NNNNNN`)且记 `resource_profile.output_segment_count`(chain.py:5084 透传 manifest);qhh 现存注册 profile 从真实 `.sp.riv` 派生补 1633(checksum c59a7fa)。使 注册≡输出≡产品——通用路径**暴露**(非引入)的保真问题,反证 m24 迁移价值。
    - ⑥**publish 无基线硬失败(已修)**:flood return-period tiles 需每流域 `flood.flood_frequency_curve` 历史基线(hindcast 校准),**全库 0 行**(两流域皆无;诊断流当年发的本就是流量 display、显式绕开无基线),M3 publish 遂硬失败 `NO_PUBLISHABLE_PRODUCTS` 破 userspace。**修(0601cea)**:`_publish_from_database` 无 flood 行时降级走现成 `_publish_qdown_from_database` 发流量 display + 标 `degraded_to_display` + return-period 记诚实 residual_blocker,仅 flood/q_down 皆空才真失败(flood happy-path 字节不变)。
    - **双流域 published receipt(live)**:cycle `gfs_2026060500`→`complete`(publish job 6043 succeeded)。复用 download/forcing(跳过下载)、qhh forecast `6040_0 COMPLETED`(列数不再失配)→publish 降级 manifest `status=published` `degraded_to_display=true` `published_basins=2`;qhh `segment_count=1633`/274344 行、heihe `2352`/395136 行;per-basin 身份零泄漏。①–⑥均已修+测试+提交(分支 `feat/issue-291-multibasin-identity`,commits 含 8cf7130/0601cea)。
    - **后续(非阻塞)**:真正洪水 return-period tiles 需为两流域 onboard `flood_frequency_curve` 基线(hindcast 校准,新流域入网工作,留 #292/#293 或独立任务);node-22 留有审计表 `ops.pipeline_job_resume_backup`(本轮 resume 前 job 快照)。
  - **业务化数据 source-of-truth(硬约束)**:每个流域的全部配置以 **`data/Basins/<流域>/input/<流域>/`** 下的真实 SHUD 模型为唯一真值(`.sp.riv` 河道=产品/输出层、`.sp.rivseg`/`seg.shp` GIS 细分层、`.sp.att` 单元、`.cfg.para` 运行参数…);后续新增流域一律落此目录,**业务化注册与运行的所有参数必须从这里派生,严禁手配/即兴覆盖**(本轮 bring-up 的 smoke 注册 3738、`GFS_FORECAST_START_HOUR=3`、partition 覆盖等即兴参数即反面教材,不得带入生产)。
- 运行手册：[`docs/runbooks/qhh-22-business-bringup.md`](docs/runbooks/qhh-22-business-bringup.md)(已标注诊断 lane + m24 转向 + data/Basins source-of-truth)。
- ⚠️ 运行纪律:作业运行中**禁止在 node-22 `git pull`**(会换 inode 触发 NFS stale handle 杀掉正在 exec 脚本的作业)。

## 当前结论

- M21 QHH 水文气象展示 + 运维监控 MVP 已完成：Epic #202 已关闭，子任务 #203-#214 已全部关闭，最后 PR #226 已合并到 `master`，merge commit 为 `ec5d535db334ddc6dc4f899742c3ff3d667e2df0`。
- M22 两节点 Docker 只读展示重构已完成：Epic #227 已关闭，子任务 #228-#239 已全部关闭，最后 PR #250 已合并到 `master`，merge commit 为 `08c72e9ca3cc1f5da3bf3ac0ea7dce20964e5348`。
- PR #250 CI 已全绿：Markdown Lint、OpenAPI Validate、JSON Schema Validate、SQL Migration Dry Run、Unit Tests、Frontend Build、Frontend M15 Visual Evidence 均为 `SUCCESS`。
- MVP 范围仍限定为 QHH/有限流域、GFS 主源、IFS 并行源、河段流量 `q_down`、forcing 代站 `PRCP/TEMP/RH/wind/Rn/Press` 和 pipeline 运维闭环；不声明全国所有流域、CLDAS、ERA5 近实时、真实全国 MVT/PBF 或最终 production ready。
- 当前仓库支持两节点角色边界：22 节点为 `compute_control`，负责 scheduler、Slurm/Gateway、产物发布和 retry/cancel；27 节点为 `display_readonly`，只读消费 DB 与 published artifacts，提供 `/hydro-met`、`/ops`、日志查看、异常展示、诊断信息复制和 22 人工处理建议。
- 27 节点不触发 retry/cancel，不调用 Slurm Gateway，不写 hydro/met/pipeline 终态，不读取 22 私有 workspace；Docker/Compose/systemd 与 evidence gate 已按该边界落地。
- 内部 deterministic E2E、Docker security/read-only display evidence gate、前后端单测/构建和契约检查已完成；目标环境 live E2E 尚未完成，不能声明 final production readiness。

## 证据索引

- MVP 统一证据索引：[`docs/runbooks/qhh-mvp-smoke-evidence.md`](docs/runbooks/qhh-mvp-smoke-evidence.md)
- 两节点生产 E2E 计划：[`docs/runbooks/two-node-production-e2e-plan.md`](docs/runbooks/two-node-production-e2e-plan.md)
- 两节点 Docker 运行手册：[`infra/README.two-node-docker.md`](infra/README.two-node-docker.md)
- M22 OpenSpec：[`openspec/changes/m22-two-node-docker-readonly-display/`](openspec/changes/m22-two-node-docker-readonly-display/)
- 当前验证入口：[`docs/VALIDATION.md`](docs/VALIDATION.md)
- 已知问题和 live 复测归因：[`docs/bugs.md`](docs/bugs.md)

## 当前能力

- FastAPI 后端已实现 forecast、models、pipeline、hindcast、flood alerts、best-available、state snapshots、data-source、runtime config 等路由。
- OpenAPI 契约位于 `openapi/nhms.v1.yaml`，前端类型由该文件生成。
- 数据库 migration 覆盖 core/met/hydro/flood/map/ops schema、索引、pipeline 字段、best-available lineage 和两节点只读展示边界所需字段。
- GFS、IFS、ERA5 adapter 已实现并有 deterministic 测试覆盖；CLDAS 仍按受限数据源处理。
- Orchestrator / production scheduler 支持 forecast/analysis/hindcast、GFS/IFS 周期发现、active runnable model 发现、Slurm job array、retry/cancel、partial success、publish stage、pipeline persistence、dry-run evidence 和 readiness ingestion。
- `met.forcing_station_timeseries` 已由 forcing producer 写入，覆盖 `PRCP/TEMP/RH/wind/Rn/Press`。
- `/hydro-met` 支持 latest-product bootstrap、站点/河段列表和地图、forcing 曲线、`q_down` 曲线、GFS/IFS source 选择和 IFS shorter-horizon 标注。
- `/ops` 支持 source/cycle selector、stage cards、jobs table、published log modal、queue/metrics、operator RBAC；`display_readonly` 下展示只读诊断和人工 22 处理建议，不发控制面 POST。
- Docker 交付包括单 app 镜像、角色化 entrypoint、Compose env skeleton、systemd units、disk preflight、HostConfig security checks 和只读 display E2E evidence gate。

## 仍需 live proof

这些不是 M21/M22 deterministic 完成度缺口，而是正式生产上线前必须在目标环境补齐的 live 证据：

- 目标 PostgreSQL/PostGIS/TimescaleDB receipt，以及 27 readonly DB credential 的 denied write probes。
- 对象存储或 published artifacts 目录 receipt；DB 中 `log_uri` 必须指向 27 可读 published URI，不能依赖 22 私有 workspace。
- 两节点部署 receipt：22 使用 `NHMS_SERVICE_ROLE=compute_control`，27 使用 `NHMS_SERVICE_ROLE=display_readonly`；27 无 Slurm route、无 Docker socket、无 22 workspace、无控制面 credential。
- cross-plane identity receipt：同一个 `run_id/source/cycle_time/model_id/basin_id` 串起 22 生产、DB 状态、published logs、latest-product、`/hydro-met` 和 `/ops`，不能用 historical latest 或 mocked API 冒充通过。
- live Slurm `sbatch`/`squeue`/`sacct`/`scancel` receipt，live GFS/IFS source download receipt，live QHH SHUD runtime receipt，并绑定正式 pipeline persistence。
- live `/hydro-met` browser run against target backend。
- live `/ops` readonly run against target backend：展示 22 侧 retry/cancel 前后的状态、published logs 和诊断信息；27 本身不得执行 retry/cancel。
- live alert sink、rollback、nationwide MVT/PBF 和 final production readiness receipts。
- 对 [`docs/bugs.md`](docs/bugs.md) 中已记录问题逐项复测归因，区分 `environment-only`、`production-config`、`data-contract`、`code-contract`、`test-runbook` 和 `frontend-feedback`。

## 常用验证命令

后端与 OpenSpec：

```bash
uv run ruff check .
uv run pytest -q
openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive
openspec validate m22-two-node-docker-readonly-display --strict --no-interactive
```

前端：

```bash
cd apps/frontend
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm run check:api-types
corepack pnpm build
corepack pnpm check:bundle
```

M22 focused checks：

```bash
uv run pytest -q \
  tests/test_runtime_mode.py \
  tests/test_monitoring_api.py \
  tests/test_pipeline_logs_artifacts.py \
  tests/test_artifact_reader.py \
  tests/test_readonly_db_validation.py \
  tests/test_two_node_docker_runtime.py \
  tests/test_two_node_docker_source_trust.py \
  tests/test_two_node_e2e_evidence.py
```

Docker / production readiness 入口：

```bash
uv run nhms-pipeline plan-production --dry-run --source gfs --source IFS
uv run nhms-production validate-readiness --help
```

## 操作注意

- 工作区可能存在 `.agents/`、`.codex/`、`data/`、`docs/images/`、`node_modules/`、`dist/`、`__pycache__`、`artifacts/` 等本地或生成文件；不要误 stage。
- `artifacts/` 是本项目产生证据和临时产物的默认位置，应保持 ignored；系统盘空间有限时，临时产物放在本仓库 ignored 路径或 `/scratch/frd_muziyao/` 下新建目录。
- 生产 Linux 环境不要复用其他机器的 `.venv` 或 `node_modules`；按 `AGENTS.md` 在目标机重新 `uv sync --all-extras --dev` 和 `corepack pnpm install --frozen-lockfile`，以目标机命令结果作为 receipt。
- 历史 OpenSpec proposal/tasks 保留当时路径和任务状态用于审计；判断当前完成度以源码、测试、`docs/VALIDATION.md`、M21/M22 evidence 和本文为准。
