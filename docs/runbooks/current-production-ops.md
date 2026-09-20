# Current Production Operations Runbook

最后更新：2026-09-17

适用范围：node-27 active DB + ingest + display，node-22 Slurm/SHUD compute，
以及两者共享的 NFS object-store/published 数据面。

> **node-22 维护窗口前执行说明**：node-22 活动 checkout 的共享 `.venv` 在
> 运维批准的维护窗口前保持 Python 3.12.7，**禁止**在其中运行裸 `uv run` /
> `uv sync`（会在 3.11 pin 下重建环境，已实测会打断成半拆状态）。本文中所有
> node-22 活动操作一律使用精确活动解释器
> `/scratch/frd_muziyao/NWM/.venv/bin/python`（控制台入口用 `-m services.orchestrator.cli`）；
> node-27 命令与孤立 rollback checkout 的 `uv sync` 不受此限。

本文是当前生产值守手册。物理部署事实以
[`ROLE_BOUNDARY.md`](../governance/ROLE_BOUNDARY.md) 的 "Current physical deployment"
段为准；[`two-node-deployment-overview.md`](two-node-deployment-overview.md)
保留为两节点 role contract 和设计意图背景，不作为当前 host 分配的操作手册。

历史 bring-up 记录见 [`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)。

## 分册索引

本页自 #1103 起是索引 landing page：每一节的正文完整保留在
`docs/runbooks/production-ops/` 下的分册里，本页只保留原标题层级与指向分册的入口，
历史外链（含 `#` 锚点）因此继续 resolve。分册正文与拆分前逐字相同。

| 分册 | 覆盖 | 内容 |
| --- | --- | --- |
| [`production-ops/overview.md`](production-ops/overview.md) | §1-§2 | 当前结论与节点拓扑 |
| [`production-ops/service-bringup.md`](production-ops/service-bringup.md) | §3 开篇、§3.1 开篇、§3.1.1 两节、§3.1.3 | 服务拉起：下载 / 调度器 / ingest |
| [`production-ops/file-provider-refresh.md`](production-ops/file-provider-refresh.md) | §3.1.2 | DB-free file-provider 稳态刷新 |
| [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md) | §3.1.4、§3.2、§3.3、§3.4 | 流域投递、Slurm Gateway、API / 展示与监控 |
| [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md) | §4、§5 开篇、§5.1-§5.6 | 业务流程与产物位置 |
| [`production-ops/recalibration-and-archive.md`](production-ops/recalibration-and-archive.md) | §5.7、§5.8 | 率定 warm carry-over 与 file-journal 冷归档 |
| [`production-ops/stuck-detection.md`](production-ops/stuck-detection.md) | §6 | 如何判断是否卡住 |
| [`production-ops/operating-scope.md`](production-ops/operating-scope.md) | §7 | 当前运行口径（业务集变更记录） |
| [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md) | §8 开篇、§8.1-§8.7 | 已知卡点：展示 / ingest / forcing / scheduler |
| [`production-ops/known-issues-state-index.md`](production-ops/known-issues-state-index.md) | §8.8-§8.11 | 已知卡点：state-index / journal / scope gate |
| [`production-ops/oncall-sql.md`](production-ops/oncall-sql.md) | §9 | 值守 SQL 片段 |
| [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md) | §10 | 前沿停摆告警 |
| [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md) | §11 | 覆盖新鲜度告警 |

§12「相关文档」的正文仍在本页末尾。

## 1. 当前结论

完整内容见 [`production-ops/overview.md`](production-ops/overview.md#1-当前结论)。

## 2. 节点和服务

完整内容见 [`production-ops/overview.md`](production-ops/overview.md#2-节点和服务)。

## 3. 如何拉起和确认服务

完整内容见 [`production-ops/service-bringup.md`](production-ops/service-bringup.md#3-如何拉起和确认服务)。

### 3.1 下载 / 调度器 / ingest

完整内容见 [`production-ops/service-bringup.md`](production-ops/service-bringup.md#31-下载--调度器--ingest)。

#### 3.1.1 Pipeline-job provenance sidecar and recovery (#2420)

完整内容见 [`production-ops/service-bringup.md`](production-ops/service-bringup.md#311-pipeline-job-provenance-sidecar-and-recovery-2420)。

#### 3.1.1 新流域上线四跳（2026-08-22 #1699 实战 receipt，7 个流域）

完整内容见 [`production-ops/service-bringup.md`](production-ops/service-bringup.md#311-新流域上线四跳2026-08-22-1699-实战-receipt7-个流域)。

#### 3.1.3 DB-free scheduler 的受支持回滚/前滚

完整内容见 [`production-ops/service-bringup.md`](production-ops/service-bringup.md#313-db-free-scheduler-的受支持回滚前滚)。

#### 3.1.2 DB-free file-provider 稳态刷新

完整内容见 [`production-ops/file-provider-refresh.md`](production-ops/file-provider-refresh.md#312-db-free-file-provider-稳态刷新)。

##### `enabled` + `inactive` 是失败态（#2041 / #2146）

完整内容见 [`production-ops/file-provider-refresh.md`](production-ops/file-provider-refresh.md#enabled--inactive-是失败态2041--2146)。

##### refresh timer 健康探针（read-only，不自愈）

完整内容见 [`production-ops/file-provider-refresh.md`](production-ops/file-provider-refresh.md#refresh-timer-健康探针read-only不自愈)。

##### 探针自己的稳态核对（watchdog 不看自己）

完整内容见 [`production-ops/file-provider-refresh.md`](production-ops/file-provider-refresh.md#探针自己的稳态核对watchdog-不看自己)。

#### 3.1.4 流域投递规范（发给建模者；平台侧照此验收）

完整内容见 [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md#314-流域投递规范发给建模者平台侧照此验收)。

### 3.2 Slurm Gateway

完整内容见 [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md#32-slurm-gateway)。

#### 3.2.1 凭据与听端口边界（#1684）

完整内容见 [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md#321-凭据与听端口边界1684)。

#### 3.2.2 协调 rollout / rollback（先 runtime ConditionPathExists 围栏，再备份，再配，再启用）

完整内容见 [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md#322-协调-rollout--rollback先-runtime-conditionpathexists-围栏再备份再配再启用)。

### 3.3 API / 展示服务

完整内容见 [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md#33-api--展示服务)。

### 3.4 监控快照

完整内容见 [`production-ops/gateway-and-services.md`](production-ops/gateway-and-services.md#34-监控快照)。

## 4. 业务流程

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#4-业务流程)。

## 5. 产物位置

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#5-产物位置)。

### 5.1 数据库

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#51-数据库)。

### 5.2 Workspace 和运行日志

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#52-workspace-和运行日志)。

### 5.3 Object-store mirror

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#53-object-store-mirror)。

#### Copyback batch mutex 与目录可穿越性（#2035）

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#copyback-batch-mutex-与目录可穿越性2035)。

#### canonical 降水镜像的跨账号删除权限（#2100）

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#canonical-降水镜像的跨账号删除权限2100)。

#### node-27 canonical 删除持锁（#2252 / #2239 / #2262 / #2360）

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#node-27-canonical-删除持锁2252--2239--2262--2360)。

### 5.4 Published artifacts

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#54-published-artifacts)。

### 5.5 Basins source data

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#55-basins-source-data)。

#### 5.5.1 清理已注册流域的 `forcing/`：清空目录，不要删目录（#1813 / #1702 第 3 项）

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#551-清理已注册流域的-forcing清空目录不要删目录1813--1702-第-3-项)。

### 5.6 新增或恢复流域的运维入口

完整内容见 [`production-ops/artifacts-and-storage.md`](production-ops/artifacts-and-storage.md#56-新增或恢复流域的运维入口)。

### 5.7 率定参数更新（recalibration）的 warm carry-over

完整内容见 [`production-ops/recalibration-and-archive.md`](production-ops/recalibration-and-archive.md#57-率定参数更新recalibration的-warm-carry-over)。

#### 5.7.1 整条 rollout 的顺序与 manifest 发布（2026-08-22 实战 receipt）

完整内容见 [`production-ops/recalibration-and-archive.md`](production-ops/recalibration-and-archive.md#571-整条-rollout-的顺序与-manifest-发布2026-08-22-实战-receipt)。

### 5.8 node-22 file-journal cycle cold archive（已启用，#2119）

完整内容见 [`production-ops/recalibration-and-archive.md`](production-ops/recalibration-and-archive.md#58-node-22-file-journal-cycle-cold-archive已启用2119)。

#### 查看 timer 与 receipt

完整内容见 [`production-ops/recalibration-and-archive.md`](production-ops/recalibration-and-archive.md#查看-timer-与-receipt)。

#### 回滚与恢复

完整内容见 [`production-ops/recalibration-and-archive.md`](production-ops/recalibration-and-archive.md#回滚与恢复)。

## 6. 如何判断是否卡住

完整内容见 [`production-ops/stuck-detection.md`](production-ops/stuck-detection.md#6-如何判断是否卡住)。

### 6.1 No-progress circuit（跨 pass 重复同一理由的证据标记，#1118）

完整内容见 [`production-ops/stuck-detection.md`](production-ops/stuck-detection.md#61-no-progress-circuit跨-pass-重复同一理由的证据标记1118)。

## 7. 当前运行口径

完整内容见 [`production-ops/operating-scope.md`](production-ops/operating-scope.md#7-当前运行口径)。

### 7.1 2026-08-07：hhe 退出业务化（当时业务集 17 流域）

完整内容见 [`production-ops/operating-scope.md`](production-ops/operating-scope.md#71-2026-08-07hhe-退出业务化当时业务集-17-流域)。

#### 7.1.1 2026-08-25 补漏：baseline `core.model_instance` 必须一并 deactivate

完整内容见 [`production-ops/operating-scope.md`](production-ops/operating-scope.md#711-2026-08-25-补漏baseline-coremodel_instance-必须一并-deactivate)。

### 7.2 2026-08-25：zhaochen 系列退出业务化（#1701，owner 裁定不建后继）

完整内容见 [`production-ops/operating-scope.md`](production-ops/operating-scope.md#72-2026-08-25zhaochen-系列退出业务化1701owner-裁定不建后继)。

## 8. 当前已知卡点

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#8-当前已知卡点)。

### 8.1 Display port drift

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#81-display-port-drift)。

### 8.2 Autopipe ingest failures

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#82-autopipe-ingest-failures)。

### 8.3 Forcing handoff parse failures

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#83-forcing-handoff-parse-failures)。

### 8.4 `/ghdc` 与计算节点边界

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#84-ghdc-与计算节点边界)。

### 8.5 Node-22 scheduler stuck after missing forcing artifact

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#85-node-22-scheduler-stuck-after-missing-forcing-artifact)。

#### 8.5.1 Withheld copyback source (`COPYBACK_SOURCE_WITHHELD`)

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#851-withheld-copyback-source-copyback_source_withheld)。

### 8.6 Heihe 底图和 DB 范围混用

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#86-heihe-底图和-db-范围混用)。

### 8.7 Heihe 河段两层模型

完整内容见 [`production-ops/known-issues-pipeline.md`](production-ops/known-issues-pipeline.md#87-heihe-河段两层模型)。

### 8.8 state-index copyback fail-closed 与 replay 补账

完整内容见 [`production-ops/known-issues-state-index.md`](production-ops/known-issues-state-index.md#88-state-index-copyback-fail-closed-与-replay-补账)。

### 8.9 state-index checksum mismatch 与受控 repair

完整内容见 [`production-ops/known-issues-state-index.md`](production-ops/known-issues-state-index.md#89-state-index-checksum-mismatch-与受控-repair)。

### 8.10 NHMS_SCHEDULER_JOURNAL_ROOT 必须是 realpath

完整内容见 [`production-ops/known-issues-state-index.md`](production-ops/known-issues-state-index.md#810-nhms_scheduler_journal_root-必须是-realpath)。

### 8.11 #1760 scope gate 与既存分叉 job_id 行

完整内容见 [`production-ops/known-issues-state-index.md`](production-ops/known-issues-state-index.md#811-1760-scope-gate-与既存分叉-job_id-行)。

## 9. 值守 SQL 片段

完整内容见 [`production-ops/oncall-sql.md`](production-ops/oncall-sql.md#9-值守-sql-片段)。

### 9.1 `core.river_segment` 两类行与计数不变量（#1693）

完整内容见 [`production-ops/oncall-sql.md`](production-ops/oncall-sql.md#91-coreriver_segment-两类行与计数不变量1693)。

### 9.2 `pg_stat_activity` 归因与 cancel 纪律（#1714）

完整内容见 [`production-ops/oncall-sql.md`](production-ops/oncall-sql.md#92-pg_stat_activity-归因与-cancel-纪律1714)。

## 10. 前沿停摆告警（frontier stall alert）

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#10-前沿停摆告警frontier-stall-alert)。

### 10.1 判据（progress-based，不看墙上时钟）

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#101-判据progress-based不看墙上时钟)。

### 10.2 三类告警邮件怎么读

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#102-三类告警邮件怎么读)。

### 10.3 阈值口径（改之前先读这段）

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#103-阈值口径改之前先读这段)。

### 10.4 误报处置

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#104-误报处置)。

### 10.5 状态、产物与恢复闭环语义

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#105-状态产物与恢复闭环语义)。

### 10.6 投递通道（认证 SMTP shim，不要用本机 sendmail）

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#106-投递通道认证-smtp-shim不要用本机-sendmail)。

### 10.7 信号口径（认领的盲区，不是 bug）

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#107-信号口径认领的盲区不是-bug)。

### 10.8 安装

完整内容见 [`production-ops/frontier-stall-alert.md`](production-ops/frontier-stall-alert.md#108-安装)。

## 11. 覆盖新鲜度告警（coverage freshness alert）

完整内容见 [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md#11-覆盖新鲜度告警coverage-freshness-alert)。

### 11.1 判据（两个前沿的关系型 gap，不是 rc、不是墙上时钟）

完整内容见 [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md#111-判据两个前沿的关系型-gap不是-rc不是墙上时钟)。

### 11.2 退出码与邮件怎么读

完整内容见 [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md#112-退出码与邮件怎么读)。

### 11.3 处置：退 1 的三个真分支 + 退 3 的三条

完整内容见 [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md#113-处置退-1-的三个真分支--退-3-的三条)。

### 11.4 阈值旋钮 `NHMS_COVERAGE_GAP_DAYS`（改之前先读这段）

完整内容见 [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md#114-阈值旋钮-nhms_coverage_gap_days改之前先读这段)。

### 11.5 安装

完整内容见 [`production-ops/coverage-freshness-alert.md`](production-ops/coverage-freshness-alert.md#115-安装)。

## 12. 相关文档

- [`ROLE_BOUNDARY.md`](../governance/ROLE_BOUNDARY.md)：current physical
  deployment source of truth.
- [`two-node-deployment-overview.md`](two-node-deployment-overview.md)：role
  contract and design-intent background; read its top banner before using it.
- [`node-27-bringup-checklist.md`](node-27-bringup-checklist.md)：node-27
  display bring-up and live checks.
- [`display-readonly-live-mvt.md`](display-readonly-live-mvt.md)：display API
  restart and live MVT evidence.
- [`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)：historical bring-up
  and early incident notes; not current topology.
