**分册：如何判断是否卡住**

本页是当前生产值守手册的 §6 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 6. 如何判断是否卡住

先分清三种状态：

- 正常运行：node-22 Slurm 有 active job，或 node-27 autopipe 正在本轮 ingest；
  `/home/nwm/autopipe-logs/autopipe.log` 周期性刷新。
- 等下一 cron tick：Slurm queue 空，autopipe 最近一轮 `rc=0`，DB 中没有新的
  un-ingested runs。
- 真实卡住：autopipe 多轮非 0、同一 run 反复 failed，public `/health` 失败，
  或 node-22 Slurm terminal 后 shared object-store/published 不更新。

推荐检查顺序：

```bash
date '+%F %T %Z'

# node-27 ingest/display
ssh -p 32099 nwm@210.77.77.27 \
  'tail -n 120 /home/nwm/autopipe-logs/autopipe.log &&
   curl -fsS --max-time 5 http://127.0.0.1:8080/health &&
   curl -fksS --max-time 5 https://test.nwm.ac.cn/health'

# node-22 compute
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R" &&
   pgrep -af "[s]ervices.slurm_gateway"'
```

If public health fails but local `127.0.0.1:8080/health` succeeds, inspect nginx
proxy target and certificates. If local health fails, restart with
`bash scripts/ops/start-display-api.sh` from `/home/nwm/NWM` and read
`/tmp/display-api.log`.

### 6.1 No-progress circuit（跨 pass 重复同一理由的证据标记，#1118）

调度器每个**完整 pass** 会统计"同一主体连续报同一 no-progress 理由"的次数，
达阈值即在证据里开闸。**纯观测**：不改调度决策、不停重试、不新增终态。

阈值 `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`（默认 3；`<= 0` 完全禁用——
不写状态文件、evidence 无该键、零日志）。跨 pass 计数落在
`<evidence_root>/no-progress-tracker.json`（oneshot 每 tick 新进程，内存计数
活不过一个 tick）；该文件不以 `scheduler_` 开头，retention 归 `unrecognised`
永不删除。

pass evidence 顶层 `no_progress_circuit` 块：

```json
{
  "threshold": 3,
  "tracked": 4,
  "state_reset": "missing",
  "open": [
    {
      "subject_kind": "job",
      "subject_id": "job_cycle_gfs_2026071200_forecast_fixture_forecast",
      "reason": "query_unavailable:comment_accounting_unproven",
      "consecutive_passes": 3,
      "first_pass_id": "scheduler_2026081812_...",
      "last_pass_id": "scheduler_2026081814_..."
    },
    {
      "subject_kind": "candidate",
      "subject_id": "gfs:2026-07-12T00:00:00+00:00",
      "reason": "blocked:state_snapshot_index_prior_checkpoint_missing_after_history",
      "consecutive_passes": 3,
      "first_pass_id": "scheduler_2026081812_...",
      "last_pass_id": "scheduler_2026081814_...",
      "operator_action_required": true
    }
  ],
  "truncated": 0
}
```

- `tracked` = 本 pass 在跟踪的 (主体, 理由) 条目数；`open` 只列到阈值的，按
  次数降序**最多 50 条**，多出的计在 `truncated`。
- `state_reset` 只在状态文件缺失（`"missing"`，首次启用即如此）或损坏
  （`"corrupt"`）时出现，两者都只是从零重算，**不会让 pass 失败**。健康期
  每个完整 pass 都会重写状态文件，所以稳态下这个键不该再出现。
- `operator_action_required` 只在该候选行自己带 #1152 三态判据时随行出现。
- `state_write_failed: true` = **本 pass 的计数没落盘**（tracker 写盘失败），
  下一 pass 会从最后一次成功落盘的值接着数，本 pass 白数一轮。出现即查
  evidence_root 权限/挂载，以及是否有残留的 `no-progress-tracker.json.tmp`
  （非常规残留——符号链接、空目录——会被下一 pass 自动清掉；非空目录或异主
  文件要人工清）。同时会有一条
  `SCHEDULER_NO_PROGRESS_CIRCUIT_STATE_WRITE_FAILED` 日志。
- **超出 evidence 字节预算的 pass 上该块会被整块丢弃**（它是两道字节门里第一个
  被舍的项，以保证尺寸裁决、既有键裁剪与 pass 终态都与本功能不存在时逐字相同）。
  此时 journalctl 的聚合 WARNING 与状态文件里的计数都不受影响——按下面的 grep
  走，别以为"没这个块=没开闸"。

告警（journalctl 是当前唯一被实际消费的通道，每个开闸的完整 pass 一条聚合行）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'journalctl -u nhms-compute-scheduler.service --since "-24h" \
     | grep SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN'
```

**`consecutive_passes` 数的是完整观察 pass，不是 timer tick**：早退、prelock
阻塞、lock 争用、资源中止的 pass 既不计数也不清零（它们的候选列表本就是空的，
在那里观察等于把计数误清零）。所以墙钟跨度可能明显大于同数 tick——不要拿
`first_pass_id`/`last_pass_id` 的时间差除以 tick 间隔来反推。

同样的 gap 还有 **adapter 级**的一层：某个 pass 里适配器的源整个缺席（reconcile
段报错只写 `reserved_unbound_error`、dry-run），该适配器名下的条目**原样保留**
（不计数也不清除），`last_pass_id` 就此冻结。所以读条目时对一下产物自己的
`pass_id`：**`open` 条目的 `last_pass_id` 落后于本 pass 的 `pass_id`，说明这条
是陈旧观测**（当前 pass 根本没看到该主体，只是没被清除），别当成"这一轮又卡了
一次"。WARNING 行里的 `last=` 字段就是给这个对账用的。

`reason` 的三类来源与下游处置：

| reason 形状 | 来源 | 去哪儿处置 |
|---|---|---|
| `blocked:<candidate reason>`，且条目带 `operator_action_required: true` | #1152 predecessor-pending 三态判据 | [`scheduler-dbfree-typed-reasons.md`](../scheduler-dbfree-typed-reasons.md)（`self_heal_expected` / `backfill_predecessor_state` 一节） |
| `<action>:identity_mismatch_blocked` / 相关 identity 尾迹 | #1173 identity 阶梯（streak ≥ 3 自动放行为 `identity_mismatch_released`） | [`failed-basin-retry.md`](../failed-basin-retry.md) § `identity_mismatch_released`；本文 §8.5 是同一条线的配置口径 |
| `query_unavailable:comment_accounting_unproven` | #1116：本集群不存 job comment，reserved 行**设计性永久**扣着 | [`failed-basin-retry.md`](../failed-basin-retry.md) §"Reserved rows held by `comment_accounting_unproven`"——**必须人工处置**，无自动出口 |

开闸只说明"这个主体连续 N 个完整 pass 没动过"，不判定谁对谁错；先按上表定位
到下游 runbook，再决定动不动手。

### 6.2 调度器停摆探针 `nhms-node22-scheduler-stall-health`（#2570）

node-22 的 DB-free 调度器卡住时以前**没有任何出口会响**：`no_progress_circuit`
只打一行 journal，`resource_limit_blocked` 只落产物，`monitoring.py` 在无
`DATABASE_URL` 的 node-22 上根本起不来。本探针补上这条出口。

形状与 file-provider refresh 探针同构：**本机、DB-free、只读、有界、固定优先级
判级、非健康即非零退出**。出口 = 非零退出 → failed unit + journal + receipt +
本节巡检行。node-22 实测**没有**任何 `OnFailure=` 通道（node-27 才有
`nhms-node27-unit-failure-alert@.service` 那条 mail lane），接 mail 是独立一单。

- 脚本：`scripts/node22_scheduler_stall_health.py`（stdlib only，可从部署树之外
  staged 运行）
- 单元：`infra/systemd/nhms-node22-scheduler-stall-health.{service,timer}`，
  `OnCalendar=*:03/15` + `RandomizedDelaySec=60`
- 证据根：`NHMS_SCHEDULER_STALL_EVIDENCE_ROOT`（默认
  `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence`，与
  `NHMS_SCHEDULER_EVIDENCE_ROOT` 同址，**只读**）
- receipt：`NHMS_SCHEDULER_STALL_RECEIPT_ROOT/latest.json`（默认
  `/scratch/frd_muziyao/nhms-prod/workspace/scheduler-stall-health/receipts`）。
  **刻意不落在证据根下**，否则探针自己的产物会进 readiness discovery 与
  pass-evidence retention 的扫描面；配错即退出码 2。
- 退出码：`0` 健康 / `1` 告警 / `2` 配置拒绝（判级前 fail-closed，此时**不读任何
  产物、不调用 systemctl**）。

判级是**首个匹配者胜**的固定优先级：读不了就不能判级（完整性最先）→ systemd 侧
（lane 死了的话产物侧一切陈述都是几小时前的）→ 产物侧 → tracker。`ok` 只在无任何
条件匹配时可达，**证据缺失绝不回落到 `ok`**。

手动跑一次（只读，不需要装 unit）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  '/scratch/frd_muziyao/NWM/.venv/bin/python \
     /scratch/frd_muziyao/NWM/scripts/node22_scheduler_stall_health.py --json'
```

#### 6.2.1 十一档 verdict 的处置

<a id="stall-probe-failed"></a>

**1. `probe_failed`** —— 运行期证据不可信，本 tick 不可判级。成因：`systemctl`
查询失败/输出不可解析、产物超 `MAX_EVIDENCE_BYTES`（5 MB）/软链/非普通文件/坏
JSON/缺 `started_at`、tracker `schema_version` 不匹配或条目残缺、证据根列不出、
**目录枚举触顶 `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED`**。
处置：读 receipt 的 `errors[]` 与 `evidence.unreadable[]` 定位到具体文件名；
触顶那条说明证据根条目数超界，先确认 retention 作业
（`scripts/node22_scheduler_evidence_retention.py` + 它那对 timer/service）还活着，
再决定抬阈值还是清积压。**不要**因为看不懂就抬阈值
把它压成 `ok` —— 枚举触顶后排序余量论证失效，判级结果不可信。

<a id="stall-timer-not-enabled"></a>

**2. `timer_not_enabled`** —— `nhms-compute-scheduler.timer` 的
`UnitFileState != enabled`。最狠的一档：重启后仍然死。
处置：确认是不是有人 `systemctl --user disable` 过；恢复见
[`../current-production-ops.md`](../current-production-ops.md) §3.1 的调度器
启停段落。

<a id="stall-timer-stopped"></a>

**3. `timer_stopped`** —— timer `ActiveState != active` **且 service 也不在跑**。
lane 现在不会再触发。合取项是**保守**写法，不是「timer 在飞时会 inactive」——
实测 timer 在整趟 pass 期间**始终 `active`**（`SubState` 在 `running`/`waiting`
间摆），`list-timers` 的 NEXT 显示 `-` 只是 active 期间不计 realtime next elapse。
处置：查 journal 里 timer 最后一次状态跃迁，再按 §3.1 恢复。

<a id="stall-scheduler-service-failed"></a>

**4. `scheduler_service_failed`** —— service 的 `Result != success`。
**没有失败值允许表**：systemd 对 oneshot 的取值域含 `exit-code`/`signal`/
`timeout`/`core-dump`/`resources`/`protocol` 等，本机只实测到过 `success`，
所以判据是「非 `success` 即报」。
处置：`journalctl --user -u nhms-compute-scheduler.service -n 200` 找上一趟的
失败原因；`resources` 一类多半是 Slurm 侧，转
[`../failed-basin-retry.md`](../failed-basin-retry.md)。

<a id="stall-scheduler-not-triggering"></a>

**5. `scheduler_not_triggering`** —— `LastTriggerUSec` 超过
`NHMS_SCHEDULER_STALL_MAX_TRIGGER_AGE_MINUTES`（默认 360 分钟）**且 service 当前
不在跑**。timer 从未触发过（`LastTriggerUSec` 为空）时同判此档。
后半个合取项是必须的：一趟实测 193 分钟的健康长 pass 期间 `LastTriggerUSec`
自然会老。
处置：`systemctl --user list-timers nhms-compute-scheduler.timer` 看 NEXT；
若 timer 活着却不触发，查 user-systemd 的 `daemon` 是否被重新执行过。

<a id="stall-evidence-unavailable"></a>

**6. `evidence_unavailable`** —— 证据根下**没有任何治理终态 pass 产物**。
与第 7 档刻意分开：这是「从来没写过」，不是「写过但很旧」。
处置：确认 `NHMS_SCHEDULER_EVIDENCE_ROOT` 与探针的
`NHMS_SCHEDULER_STALL_EVIDENCE_ROOT` 指向同一目录；再确认 retention 没有把整个
目录清空（`retention/` 子目录里有它自己的 receipt）。

<a id="stall-evidence-stale"></a>

**7. `evidence_stale`** —— service 当前**不在跑**，且最新终态产物的 `started_at`
超过 `NHMS_SCHEDULER_STALL_MAX_PASS_AGE_MINUTES`（默认 360 分钟）。
这是**独立兜底**：timer 照常触发、service 每次秒退不写产物时，systemd 侧四个信号
全绿而产物断流。「service 不在跑」是结构性闸门，不靠「360 > 实测最长 193.9」的
数值侥幸。
处置：`journalctl --user -u nhms-compute-scheduler.service --since -6h`，找为什么
每趟都提前 return（多半是 preflight 或 lock）。

<a id="stall-pass-limit-blocked"></a>

**8. `pass_limit_blocked`** —— 回看窗口
`NHMS_SCHEDULER_STALL_LIMIT_LOOKBACK_MINUTES`（默认 120 分钟）内**存在**某趟
`status == "resource_limit_blocked"`。**窗口存在性**判定，不是「最新一趟」定点
判定：该状态只在那趟是最新期间成立（实测空闲态 4.5–27 分钟），定点判定配上 15
分钟探针周期几乎观测不到。这档压在后三档之上，因为那趟产物本身已降级（`counts`
缺键），诊断窗口最窄。
处置：打开 receipt 里 `signals.resource_limit_passes_in_window` 对应的那趟产物，
读 `limit` 块，转
[`../scheduler-dbfree-typed-reasons.md`](../scheduler-dbfree-typed-reasons.md)。

<a id="stall-lock-contended-persistent"></a>

**9. `lock_contended_persistent`** —— 连续 ≥ `NHMS_SCHEDULER_STALL_LOCK_PASSES`
（默认 5）趟 `status == "lock_contended"`。单趟是常态噪声（实测 276 趟里 1 趟），
连续才是形状：证据根现存三份人工清锁凭据
（`stale-lock-clear-issue882-20260706T072555Z.json` 等），说明这事发生过至少三次。
这档其它信号都覆盖不到 —— 产物照常新鲜、没有阻塞候选、tracker 也不长。
处置：按 [`../node22-control-plane-manual-recovery.md`](../node22-control-plane-manual-recovery.md)
的人工处置流程核对锁主，再决定是否清锁，并照既有形状把凭据落成
`stale-lock-clear-<issue>-<ts>.json`；**探针自己绝不清锁**。

<a id="stall-submission-stalled"></a>

**10. `submission_stalled`** —— 连续 ≥
`NHMS_SCHEDULER_STALL_NO_SUBMISSION_PASSES`（默认 20）趟「零提交且有阻塞候选」。
四类口径（每趟终态产物恰好落一类）：

| 类 | 判据 | 对 streak 的作用 |
|---|---|---|
| progress | `counts.submitted_count > 0` | **打断** |
| blocked | `submitted_count == 0` 且 `blocked_candidate_count > 0` | **延长** |
| idle | `submitted_count == 0` 且 `blocked_candidate_count == 0` | **打断**（阻塞候选消失即阻塞已解除） |
| neutral | 产物**无 `progress_guard` 键**，**或** 两个 count 任一缺失 | **跳过**（既不延长也不打断） |

中性**不是 status 允许表**。调度器自己的口径是「early-exit / pre-lock /
lock-contended / resource-limit-aborted」**四类早退写入点**，不是四个 status 字符
串；`progress_guard` 在 `scheduler_runtime.py:834` 才构造，早退 pass 的产物里没有
该键 —— 那才是这四类的可观测投影。实测 276 份完全吻合。
反例（写进用例钉死）：`scheduler_2026092223_7e6955b406ba.json` 是
`restart_reconciled` 且 `blocked_candidate_count=47`，正是 #2570 四趟停摆的第一趟，
按词表会被当中性跳过；`preflight_blocked` 是 fully-observed、调度器自己会计数。
处置：读 receipt 的 `passes` 计数与最新几趟产物的 `blocked_candidates`，转
[`../scheduler-dbfree-typed-reasons.md`](../scheduler-dbfree-typed-reasons.md)。

<a id="stall-no-progress-circuit-open"></a>

**11. `no_progress_circuit_open`** —— `no-progress-tracker.json` 里存在**未被抑制
的**条目 `consecutive_passes >= NHMS_SCHEDULER_STALL_CIRCUIT_PASSES`（默认 20）。
该阈值**刻意高于**调度器自身的 observe 阈值
`NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES=3`：circuit 负责观测，探针负责告警，
两者解耦。抑制见 6.2.4。
处置：按 §6.1 的 reason 三类表定位下游 runbook。

<a id="stall-ok"></a>

**12. `ok`** —— 以上皆不匹配。仅在无任何条件匹配时可达。

#### 6.2.2 阈值 retune（drop-in，不是 EnvironmentFile）

service unit **刻意不带 `EnvironmentFile`**：shipped 默认值就是 node-22 生产值，
env 文件会多出一个能静默放宽告警阈值的未跟踪面。改阈值一律走 drop-in：

```bash
mkdir -p ~/.config/systemd/user/nhms-node22-scheduler-stall-health.service.d
cat > ~/.config/systemd/user/nhms-node22-scheduler-stall-health.service.d/10-thresholds.conf <<'EOF'
[Service]
Environment=NHMS_SCHEDULER_STALL_MAX_PASS_AGE_MINUTES=480
EOF
systemctl --user daemon-reexec   # 或 daemon-reload
```

范围校验（越界即退出码 2，判级前拒绝）：两个 age ≥ 240；
`LIMIT_LOOKBACK_MINUTES` ≥ 30（须覆盖探针周期 15 + `RandomizedDelaySec=60` +
余量）；`LOCK_PASSES` / `NO_SUBMISSION_PASSES` ≥ 2；`CIRCUIT_PASSES` ≥ 1；
`SCAN_LIMIT >= max(NO_SUBMISSION_PASSES, LOCK_PASSES) + 12`（12 是单小时桶余量，
实测单桶最多 8 趟）；`MAX_ENTRIES_SCANNED >= SCAN_LIMIT`。
**改完必须把 drop-in 记在本节** —— 一个没人看得见的阈值就是没人能审计的阈值。

#### 6.2.3 安装步骤与装前装后对拍

**本单不发安装脚本**。`scripts/install_node22_refresh_timer_health.sh` 的
protected-state 是**强制执行 + 自动回滚**（`ERR` trap 失败即移除探针单元、两段式
install/enable、机器可读 `protected_unchanged`，保护集是
`nhms-compute-scheduler.{timer,service}` +
`nhms-scheduler-file-provider-refresh.{timer,service}` **四个**单元）。
下面这套人工对拍**明确放弃**三样东西，不声称等价：**强制性**（没人拦着你手滑）、
**自动回滚**（出错不会自己收拾）、以及 **`nhms-scheduler-file-provider-refresh`
那对单元的保护**（本流程只对拍调度器那两个）。补 installer 是独立一单。

```bash
# 1) 装前快照（必须留档）
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'systemctl --user show nhms-compute-scheduler.timer nhms-compute-scheduler.service \
     -p Id,UnitFileState,ActiveState,SubState' | tee /tmp/sched-before.txt

# 2) 安装（只拷贝探针自己的两个文件，绝不碰调度器单元）
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'install -m 0644 /scratch/frd_muziyao/NWM/infra/systemd/nhms-node22-scheduler-stall-health.service \
       ~/.config/systemd/user/ &&
   install -m 0644 /scratch/frd_muziyao/NWM/infra/systemd/nhms-node22-scheduler-stall-health.timer \
       ~/.config/systemd/user/ &&
   systemctl --user daemon-reload &&
   systemctl --user enable --now nhms-node22-scheduler-stall-health.timer'

# 3) 装后快照并对拍：必须逐行一致
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'systemctl --user show nhms-compute-scheduler.timer nhms-compute-scheduler.service \
     -p Id,UnitFileState,ActiveState,SubState' | tee /tmp/sched-after.txt
diff /tmp/sched-before.txt /tmp/sched-after.txt && echo PROTECTED_UNCHANGED
```

`ActiveState`/`SubState` 若因为恰好有一趟 pass 起落而变化，**不要**当成对拍失败，
但必须在留档里写清当时 `nhms-compute-scheduler.service` 在飞（`activating`）。
`UnitFileState` 任何变化都是事故，立即回滚探针单元。

#### 6.2.4 抑制白名单：当前值与出处

`NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS` 默认值（checked-in）：

```
ambiguous_fallback_match:comment_accounting_unproven
```

**为什么这条结构上不可收敛**：该 reason 由
`services/orchestrator/reconcile.py:2564` 写出 —— 当 name-window fallback 匹配到
两个及以上 owned in-window master 时，控制器无法证明 forcing identity
（`reconcile.py:763`），durable reason 落为 `comment_accounting_unproven`。
根因是**本 Slurm 集群不返回 `job_comment`**（#1116，见 §6.1 reason 表第三行），
exact-comment 对账因此**永远** unproven：本 session 在 node-22 实测 reconcile
`match_count 2` 无法收敛。tracker 里这两条的 `consecutive_passes` 实测已到
1300 / 1198，裸阈值会让告警永久钉住 → 告警疲劳 → 运维被训练成忽略它。
追踪见 #2570（node-22 侧）与 #1116（根因侧）。

纪律：

- 匹配是 **reason 精确串**，不是前缀、不是 `subject_id`（后者带 cycle，每个新
  cycle 都要改配置，必然腐烂）。
- **抑制只作用于 verdict 11**，对 verdict 1–10 任何一条都没有影响。
- 被抑制的条目照样写进 receipt 的 `suppressed[]`
  （`subject_kind`/`subject_id`/`reason`/`consecutive_passes`/`matched_rule`），
  可审计性由 receipt 承担，不靠人记。
- 残余风险（明写、不消除）：若某个真实新 stall 复用了被抑制的 reason，探针不报。
  接受，因为该 reason 已判定为结构不可收敛；换 reason 类的 stall 仍会报。

#### 6.2.5 巡检行：探针自己还活着吗

`Persistent=` **不能**复活一个「enabled 但 inactive」的探针 —— 那种 timer 永远
不会跃迁到 active，也就没有任何时刻去重放漏掉的 tick。所以探针自身的存活是**人工
巡检项**：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'systemctl --user list-timers nhms-node22-scheduler-stall-health.timer --all &&
   cat /scratch/frd_muziyao/nhms-prod/workspace/scheduler-stall-health/receipts/latest.json'
```

两条判据：

1. `list-timers` 的 **NEXT 不是 `-`**（探针 timer 用的是 `OnCalendar`，与被监视的
   `OnUnitActiveSec` 调度器 timer 不同，它**有** realtime next elapse）。
2. receipt 的 `generated_at` 新鲜 —— 15 分钟周期 + `RandomizedDelaySec=60`，
   超过 20 分钟没更新就是探针自己停了。

receipt 里每个信号的**观测值与其阈值并列**（`last_trigger_age_minutes` 对
`max_trigger_age_minutes`，`no_submission_streak` 对 `no_submission_passes`，
依此类推），所以判级可以只凭 receipt 独立重推，不需要回到证据根。
