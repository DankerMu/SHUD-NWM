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
