**分册：解析失败驻留告警**

本页是当前生产值守手册的 §13 分册（#2529 新增）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 13. 解析失败驻留告警（parse-failure residency alert）

2026-09-19 两趟 autopipe tick（tick #16 `05:55:40Z→06:30:56Z`、tick #17
`06:30:56Z→06:53:36Z`）以 `rc=1` 收尾，11 个 run 落 `OUTPUT_PARSE_DB_ERROR`（57014
statement timeout），**全程无人知道**：autopipe unit 没挂 `OnFailure=`，§10 前沿车道阈值 4 h、
§11 覆盖车道阈值以天计，都构造性够不着小时级的解析失败；重试无上限、无计数。那一次三趟 tick
内自愈；真正要抓的是**不自愈**的那一次（#1781：永久 rc=1，靠人工 grep 才发现）。
`nhms-node27-parse-failure-residency-alert.timer`（每 30 分钟，`:15`/`:45`，
`scripts/node27_parse_failure_residency_alert.py`）补的就是这个探测器。根因判别 receipt 见
[`../receipts/2026-09-23-issue-2529-parse-timeout-root-cause/README.md`](../receipts/2026-09-23-issue-2529-parse-timeout-root-cause/README.md)。

### 13.1 判据（驻留型，不看单 tick 的 rc）

- **被观察集**：`hydro.hydro_run` 中 `status='failed'`、`error_code LIKE 'OUTPUT\_PARSE\_%'`、
  且 `updated_at` 落在**重试存活界**（默认 6 h）内的行。autopipe 每 tick 都把 `failed` 的 run
  原样重投，`mark_run_failed` 每次都刷新 `updated_at`，所以这个界只留下"仍在被重试"的 run。
- **状态**：`{run_id: {first_observed_failing_at, last_alerted_at}}`（JSON，原子替换，旁边一把
  `fcntl` 锁）。某次观察里不再出现的 run（已 `parsed`，或老出存活界）被删掉；它以后再失败从头计时。
  解析器不写中间状态——重试后的 run 要么回到 `failed`、要么变 `parsed`——所以"连续观察都在集合里"
  就是"一直在失败"。
- **驻留**：`now - first_observed_failing_at >= 阈值`（默认 2 h）。
- **退出码**：存在「从未告警过」或「上次告警已 ≥ 重告间隔（默认 24 h）」的驻留 run → **退 1**
  （这些 run 的 `last_alerted_at` 先写进状态再退出）；否则退 0（报告里照样列出已告警过的驻留 run）；
  配置 / 数据库 / 状态文件错误 → **退 2**（一行带类型码的说明，无 traceback）。另一个实例持锁 →
  退 0，打一行 `tick skipped`（跳过一个 tick 不是告警）。
- **只按 `OUTPUT_PARSE_` 前缀、不拆码**：`OUTPUT_PARSE_DB_ERROR` 把 57014 / 锁类 / 永久 schema 错误
  折叠成一个码，但重试路径不读 `error_code`，拆码不改变任何行为；本车道按前缀判驻留，不需要拆。

### 13.2 邮件怎么读

告警就是非零退出：unit 挂 `OnFailure=nhms-node27-unit-failure-alert@%n.service`，handler 邮寄
`journalctl -n 30`。stdout/stderr **留在 journal**（没有 `append:`），所以报告就是邮件正文：

```text
parse-failure-residency now=… watched=<N> resident=<R> newly_alerted=<A> already_alerted=<K> threshold=2h realert=24h liveness=6h runbook=…
resident run_id=<run> error_code=OUTPUT_PARSE_DB_ERROR first_observed=<UTC> residency_h=<h> alert=due
… 至多 10 行 resident …
... and <M> more
```

`alert=due` 是本次触发告警的 run，`alert=within-realert` 是已在重告间隔内告过警、仍在驻留的 run。
退 2 时正文是一行 `PARSE_FAILURE_RESIDENCY_{CONFIG_INVALID|STATE_CORRUPT|OBSERVATION_FAILED} reason=…`。
**退 2 每个 tick 都会发信、不去重**——坏掉的观察者本身就是要人处理的事（与 §11 同一口径）。

### 13.3 处置

退 1（有驻留 run）：

1. 看这些 run 的最新错误与失败时间：

   ```sql
   SELECT run_id, error_code, left(error_message, 200) AS error_message, updated_at
   FROM hydro.hydro_run
   WHERE status = 'failed' AND error_code LIKE 'OUTPUT\_PARSE\_%'
   ORDER BY updated_at DESC;
   ```

2. 看最近几趟 tick 是否一直 `rc=1`、哪一段慢：
   `grep -E 'autopipe: (start|done|phase=)' /home/nwm/autopipe-logs/autopipe.log | tail -30`。
3. 57014（statement timeout）重现时，**在下一次失败的 tick 进行中**抓一份当时的语句 / 锁快照
   （这是 #2529 AC1 "replace-chain 实测耗时" 事后无法复原的那部分证据；只读）：

   ```sql
   SELECT pid, application_name, state, wait_event_type, wait_event,
          now() - query_start AS running_for, left(query, 160) AS query
   FROM pg_stat_activity
   WHERE datname = current_database() AND state <> 'idle'
   ORDER BY query_start;
   ```

   同时对照 retention（`06:36 UTC`）/ compression（`04:25 UTC`）unit 的实际起止：
   `journalctl --user -u nhms-node27-timeseries-retention.service -u nhms-node27-timeseries-compression.service --since '<UTC 起点>' --no-pager`。
4. **不要以调大 `statement_timeout` 结案**——那是掩盖，不是修复（#2529 非目标）。

退 2：`CONFIG_INVALID` → 修 env（见 `infra/env/node27-parse-failure-residency-alert.example`）；
`OBSERVATION_FAILED` → 只读 DSN / 库本身；`STATE_CORRUPT` → 状态文件**不会自动重建**（重建会把所有
驻留计时清零、把一个永久失败再藏一个阈值），先看内容，确认后删掉
`/home/nwm/node27-parse-failure-residency-alert/state.json`，下一 tick 从空状态开始。

**被放弃的历史失败**（不在存活界内，因此**不告警**）：2026-09-23 实测有 2 行
`OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`，最后触碰于 2026-08-28，7 天内触碰 0 行。清理它们是运维动作，
不是告警。先查 autopipe 为什么不再重投——#1781 的 decline 记录会让同一产物（`init_state_id` +
`product_mtime`）不再重试：

```sql
SELECT h.run_id, h.error_code, h.updated_at, d.reason_code, d.init_state_id, d.product_mtime
FROM hydro.hydro_run h
LEFT JOIN ops.ingest_recompute_decline d ON d.run_id = h.run_id
WHERE h.status = 'failed' AND h.error_code LIKE 'OUTPUT\_PARSE\_%'
  AND h.updated_at <= now() - interval '6 hours';
```

有 decline 记录 = 已被 autopipe 记账（产物一旦被重写即自动重投）；要真正清出 `failed` 集合，是按该
run 的处置决定重新产出 / 重新解析，或按业务口径将其 supersede（autopipe 对 `superseded` 无条件跳过）。
没有 decline 记录却不再被触碰的 run，按 §13.3 第 1–2 步查原因。

**认领的盲区**：一趟 tick 挂住超过存活界（6 h）时，它的失败会老出被观察集——那个形状归 §10 的 4 h
前沿停摆车道。

### 13.4 阈值旋钮

全部可选，写在 `infra/env/node27-parse-failure-residency-alert.env`（0600；unit 以 `-` 前缀读取，
文件不存在即全默认），CLI 同名参数优先：

| env | CLI | 默认 | 约束 |
|---|---|---|---|
| `NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS` | `--threshold-hours` | 2 | ≥ 0；0 = 每个被观察 run 立即驻留（仅 live receipt 用） |
| `NHMS_PARSE_RESIDENCY_REALERT_HOURS` | `--realert-hours` | 24 | > 0 |
| `NHMS_PARSE_RESIDENCY_RETRY_LIVENESS_HOURS` | `--retry-liveness-hours` | 6 | > 0 |
| `NHMS_PARSE_RESIDENCY_REPORT_RUNS` | `--report-runs` | 10 | 1..20（邮件正文要装进 30 行 journal 尾巴） |
| `NHMS_PARSE_RESIDENCY_STATE_PATH` | `--state-path` | `/home/nwm/node27-parse-failure-residency-alert/state.json` | 绝对路径；锁文件为 `<path>.lock` |

### 13.5 安装（部署步骤，#2529 的 PR 不做）

DSN 用只读角色 **`nhms_display_ro`**（对 `hydro.hydro_run` 只有 `SELECT`），直接复用 §10 的
`infra/env/node27-frontier-alert.env`（同一份密钥，0600 契约由 §10 的 wrapper 每 30 分钟校验）。
与 §10/§11 两条已启用的车道不同，**本 timer 不随 PR 启用**：`/home/nwm/NWM` 的部署 checkout 落后于
master、还没有这个脚本；启用是下一次 node-27 部署的一部分：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
install -m 644 infra/systemd/nhms-node27-parse-failure-residency-alert.service ~/.config/systemd/user/
install -m 644 infra/systemd/nhms-node27-parse-failure-residency-alert.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemd-analyze --user verify nhms-node27-parse-failure-residency-alert.service
systemctl --user start nhms-node27-parse-failure-residency-alert.service   # 先手跑一次
journalctl --user -u nhms-node27-parse-failure-residency-alert.service -n 30 --no-pager
systemctl --user enable --now nhms-node27-parse-failure-residency-alert.timer
systemctl --user list-timers 'nhms-node27-parse-failure-residency-alert.timer' --no-pager
```

手工在 shell 里跑与 unit 同口径：

```bash
cd /home/nwm/NWM
set -a; . infra/env/node27-frontier-alert.env; set +a
PYTHONPATH=/home/nwm/NWM .venv/bin/python scripts/node27_parse_failure_residency_alert.py; echo "rc=$?"
```
