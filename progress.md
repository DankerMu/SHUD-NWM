# 项目进度

> 跨 session 继承的进度索引：只记**当前到哪了 + 边界 + 入口**。逐 commit/逐缺陷细节以 GitHub issue、PR、OpenSpec、runbook 为准，不在此重复。

## 最新（2026-06-07）

**node-27 开发地基就位**（master 直推 `661da44`）——22 业务化稳定后转入 27（`display_readonly`）前端生产化开发，先把流程/文档/CI 铺稳：

1. **m25 change**（`openspec/changes/m25-multibasin-frontend-production/`，多流域前端生产化）已 spec 化并过 codex 三路审核——**P0 修正**：洪水重现期可用性从"进 blocking reasons"改为 `availability.return_period_status` **独立 supplemental 字段**（否则有 q_down 无洪频基线的产品会整体掉 ready/404）；已拆 GitHub issue（并行起点 #310/#311/#313/#317）。
2. **三节点协作固化**：`CLAUDE.md` 双端→三端（27=`nwm@…27:/home/nwm/NWM`），新增**验证 oracle 路由**（后端→22 真 DB pytest / display→27 live receipt / 本地→ruff·openspec·pnpm）+ 远端 ff-only 同步纪律；`dual-end-issue-workflow` skill 同步扩成"双 oracle"（含 node-27 recipes：**开发期本地起服务非 docker、fail-closed ff 恢复**）。
3. **CI 按路径 scope**（`ci.yml`，`dorny/paths-filter`）：纯前端/docs PR 跳过 16min 后端 pytest；**draft PR=定向快速通道 / ready·master=全量合并门**（忘标 draft→默认全量，fail-safe）；`concurrency` cancel-in-progress；暂停遗留 `frontend-m15-visual`。master 无 branch protection=**人工合并门**。约定见 `CLAUDE.md`「CI 范围与门控」。
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
- **m24 转向**（epic #285，子任务 #286–#293 + #300）：生产路径 = 通用 scheduler/chain daemon 经独立 Slurm gateway。三道硬坎 = ① gateway 在 node-22 部署（#288，从未 live）② 跨周期暖启动 path(b) 短 analysis 段（#289）③ 并发 submit-and-return + durable reservation（#290，代码已合并，未 live）。**部署前置**：迁移 `db/migrations/000029_pipeline_reservation.sql` 必须在 #292 go-live 前 apply（node-22 prod DB 尚未 apply）。
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

目标环境 receipt：PostgreSQL/PostGIS/TimescaleDB（含 27 readonly denied-write probes）、对象存储/published artifacts（`log_uri` 指向 27 可读 URI）、两节点部署角色、cross-plane identity（同一 `run_id/source/cycle/model/basin` 串起 22 生产→DB→published→`/hydro-met`+`/ops`）、live Slurm 全套、live `/hydro-met` + `/ops` browser run、alert sink/rollback/nationwide MVT。逐项归因见 [`docs/bugs.md`](docs/bugs.md)。

## 入口

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
