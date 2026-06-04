# 项目进度

最后更新：2026-06-04，node-22 业务化联调。

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
- **全持续守护暂缓**（等指令）。
- 运行手册：[`docs/runbooks/qhh-22-business-bringup.md`](docs/runbooks/qhh-22-business-bringup.md)。
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
