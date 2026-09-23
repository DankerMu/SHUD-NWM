# Proposal

## Why

#2570 C 组：node-22 的 DB-free 调度器卡住时，**没有任何出口会响**。

- `services/orchestrator/monitoring.py:36` 的 `MonitorConfig.from_env` 在 `DATABASE_URL`
  缺失时直接 `raise ValueError`，而 node-22 按设计**不连任何活 DB**（CLAUDE.md 三端表）。
  该模块在 node-22 上不可运行；node-22 的 `workspace/monitoring/` 产物停在 2026-06-29 10:54（实测）。
  node-27 能跑它，但看不见 node-22 的 `/scratch` 证据目录（共享的 NFS 只有 `/ghdc/data/nwm`）。
  **所以 C 组不能加在 `monitoring.py` 里。**
- 现场：`no_progress_circuit` 只打一行 stderr（`scheduler_no_progress.py:46`），进 journal 无人看；
  `resource_limit_blocked` 只落产物。2026-09-22T21:13 那趟事故（一个 unit 的整条链被未治理异常吞掉）
  是人工翻 279 份产物才发现的，**期间零告警**。
- node-22 的调度器 timer/service 本身也**无人守**：`nhms-node22-refresh-timer-health` 守的是
  file-provider refresh lane，没有任何探针看 `nhms-compute-scheduler.{timer,service}`。

先例把这类事做对过一次：`scripts/node22_refresh_timer_health.py` +
`infra/systemd/nhms-node22-refresh-timer-health.{service,timer}` —— 本机、DB-free、只读、有界、
固定优先级判级、非健康即非零退出。本单照此先例给**调度器 lane** 建同构探针。

## 先量后设（全部实测，2026-09-23，276 份终态 pass 产物）

初版 fixture 按「6 分钟节奏 × 5 = 30 分钟」定产物超龄阈值，被生产数据直接证伪：

| 量 | 实测 |
|---|---|
| pass 时长 med / p90 / max | 8.0 / 11.7 / **193.1** 分钟；>60 分钟 13 趟 |
| 相邻 `started_at` 间隔 med / p90 / p99 / max | 8.3 / 13.4 / **160.4** / **193.9** 分钟；>30 分钟 19 次 |
| 终态 status 分布 | planned 252、submitted 15、`resource_limit_blocked` 4、restart_reconciled 2、submitted_partial 1、`lock_contended` 1、preflight_blocked 1 |
| `counts.submitted_count` / `blocked_candidate_count` 缺键 | 4 份，**全部**是 `resource_limit_blocked` 的降级产物 |
| `.pre_execution.json` 与终态产物并存 | 21 份 |
| 单个小时桶最多 pass 数 | 8 |

**结论一**：健康的 busy pass 本身就跑 160–194 分钟，「最新终态产物的年龄」把
「timer 死了」和「一趟长 pass 正在飞」混成同一个量，**不能**充当存活信号。
存活必须直接读 systemd（照 refresh 探针读它那条 lane 的做法），产物年龄退为**独立的兜底**信号，
阈值按实测 max 194 分钟留足余量（默认 360 分钟，范围校验 ≥240）。
且 systemd 侧的判据同样必须实测：调度器 service 是 `Type=oneshot`，**在飞时
`ActiveState=activating` / `SubState=start`，永远不会是 `active`**；timer 则在飞期间
**始终 `active`**（`SubState` 在 `running`/`waiting` 间摆）。凡以「service 不在跑」为合取项的档
（verdict 3/5/7）若写成 `== "active"`，会在健康长 pass 期间全部误报。

**结论二**：探针最需要读的恰恰是降级产物，而那正是 `counts` 缺键的那批。
「缺键即 0」会把降级 pass 读成「零提交」，「缺键即 `probe_failed`」会在窗口里一出现合法降级产物
就压住全部信号。必须有第四类：**中性**（既不延长也不打断 streak），并记进 receipt。

但中性**不能**用 status 允许表定义。`infra/env/compute.scheduler-dbfree.env.example:148-150`
的「Early-exit, pre-lock, lock-contended and resource-limit-aborted passes neither count nor clear」
说的是**四类早退写入点**，不是四个 status 字符串；映射成词表两头都错 —— 实测：
`scheduler_2026092223_7e6955b406ba` 是 `restart_reconciled` 且 `blocked_candidate_count=47`，
正是 #2570 四趟停摆的**第一趟**，按词表会被当中性跳过。
改用写方派生的可观测量：`progress_guard` 在 `scheduler_runtime.py:834` 才构造，
**早退 pass 的产物里没有该键**。实测 276 份完全吻合 —— guard 缺席的恰好是
4 份 `resource_limit_blocked` + 1 份 `lock_contended`，而 `restart_reconciled`、
`preflight_blocked` 都带 guard、会按 counts 正确判为 blocked。

## What Changes

新增 node-22 本机只读探针 `scripts/node22_scheduler_stall_health.py` + 一对 systemd user unit。
探针读三类证据 —— `systemctl --user show`（只读）、`NHMS_SCHEDULER_EVIDENCE_ROOT` 下的治理 pass 产物、
`no-progress-tracker.json` —— 按**固定优先级**判出唯一 verdict：

| 序 | verdict | 触发 |
|---|---|---|
| 1 | `probe_failed` | 运行期证据不可信（systemd 查询失败、产物超界/软链/坏 JSON/缺 `started_at`、tracker schema 不匹配、目录枚举触顶） |
| 2 | `timer_not_enabled` | 调度器 timer `UnitFileState != enabled` |
| 3 | `timer_stopped` | timer `ActiveState != active` **且** service 也不在跑（lane 不会再触发） |
| 4 | `scheduler_service_failed` | 调度器 service `Result != success` |
| 5 | `scheduler_not_triggering` | `LastTriggerUSec` 超龄 **且** service 当前不在跑 |
| 6 | `evidence_unavailable` | 根下无任何治理终态 pass 产物 |
| 7 | `evidence_stale` | service 当前不在跑 **且** 最新终态产物 `started_at` 超龄（systemd 说活着但没产物 —— 独立兜底） |
| 8 | `pass_limit_blocked` | 全部已解析产物中，回看窗口内**存在**某趟 `status == "resource_limit_blocked"` |
| 9 | `lock_contended_persistent` | 连续 ≥N 趟 `status == "lock_contended"` |
| 10 | `submission_stalled` | 连续 ≥N 趟「零提交且有阻塞候选」 |
| 11 | `no_progress_circuit_open` | tracker 有未被抑制的条目 `consecutive_passes >= 阈值` |
| 12 | `ok` | 以上皆不匹配 |

- 退出码照先例：`0` 健康 / `1` 告警 / `2` 配置拒绝（判级前 fail-closed）。
- verdict 8 是**窗口存在性**判定而非「最新一趟」定点判定：`resource_limit_blocked` 只在那趟是最新期间成立
  （实测空闲态 4.5–27 分钟），定点判定配上小时级探针周期几乎观测不到。探针周期 15 分钟，回看窗口默认 120 分钟。
- verdict 9 来自真实历史：证据根现存三份人工清锁凭据
  （`stale-lock-clear-issue882-20260706T072555Z.json` 等），该形状发生过至少三次，原七档一档都不覆盖。
- 每个非健康 verdict 在 receipt 与 stderr 各留一行带 `runbook` 指针（journal 就是本机的告警通道）。
- 慢性条目抑制：tracker 实有 2 条 `ambiguous_fallback_match:comment_accounting_unproven`
  （`consecutive_passes` 1300 / 1198）。裸阈值会让告警永久钉住 → 告警疲劳。
  处置：**无状态 reason 白名单**（默认值 checked-in），被抑制条目照样写进 receipt。

## Out of Scope

- **不引入邮件/IM 出口**。node-22 实测无任何 `OnFailure=` 通道（node-27 才有
  `nhms-node27-unit-failure-alert@.service` 那条已通的 mail lane）。本单出口 = 非零退出 → failed unit
  + journal + receipt + runbook 巡检行，与 refresh 探针完全同构。接 mail lane 是独立一单。
- **不改 `monitoring.py`**，不给它加 DB-free 分支。
- **不改调度器自身**：`no_progress_circuit` 仍 observe-only、仍每层最先剥（#1118）；
  `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES=3` 是调度器自己的 observe 阈值，探针用自己的告警阈值，刻意解耦。
- **不做自愈**：探针不启停/不清锁/不改任何 unit，照先例 D1「detection only」。
- 不做「models selected 但 `candidate_count == 0`」规则 —— 与正常空闲 pass 不可区分
  （实测 252/276 趟是 `planned`，正是该形状）。
- **不写安装脚本，且不声称等价**。`install_node22_refresh_timer_health.sh` 的 protected-state 是
  **强制执行 + 自动回滚**（`ERR` trap 失败即移除探针单元、两段式 install/enable、机器可读
  `protected_unchanged`、保护集是 `nhms-compute-scheduler.{timer,service}` +
  `nhms-scheduler-file-provider-refresh.{timer,service}` **四个**单元）。本单改用 runbook 里的人工前后对拍，
  **明确放弃**的是：强制性、自动回滚、以及 file-provider-refresh 那对的保护。
  先例依据只是「两对 retention unit 也是手装的」，不构成等价。补 installer 是独立一单。
- #2570 A 组（`chain_stage_execution.py` 异常隔离）：另单另 PR。

## Triage

```text
Issue type: feature
Fixture level: expanded
Upstream suggested level: absent（自定 expanded：新增生产 systemd 单元 + 11 档告警判级语义，
  误判双向有害——漏报等于回到今天，误报等于告警疲劳并训练运维忽略它；初版 fixture 的
  默认阈值已被生产数据证伪一次，量纲必须由实测反推）
Blast radius: 探针只读不改调度器；但装在 node-22 生产 user-systemd 里，
  安装动作若碰到 `nhms-compute-scheduler.timer` 会打断生产调度
Selected risk packs: Error handling/部分输出；Resource limits/大输入；Config/项目设置；
  File IO/路径安全；Legacy compatibility；Concurrency-ordering（仅 ordering 轴）
Evidence floor: 11 档判级各自用例 + 优先级用例（含「在飞几何不得判死」）+ 四类 pass 口径各自用例 +
  `restart_reconciled`+blocked>0 必判 blocked 的回归用例 +
  排序只信 `started_at` 不信文件名/mtime + 有界读与 fail-closed + 慢性抑制不误伤且不越界 +
  unit 静态断言 + 先例套件全绿 + node-22 实机 live receipt
```
