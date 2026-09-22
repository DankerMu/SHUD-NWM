# node-22 Control-Plane Manual Recovery Runbook

display API 在 `display_readonly` 模式下对控制面动作返回 409，payload 的
`recovery_runbook` 指向本文（slug `node22-control-plane-manual-recovery`，
`apps/api/routes/pipeline.py`）。DB-free scheduler 在五类终态决策上写
`retry_policy.manual_retry_required: true`，本文给出"怎么找到它们"和"每一类走哪个
入口"。

## 执行纪律（node-22）

- 维护窗口前 node-22 活动 checkout 仍是 Python 3.12.7：**禁止** `uv sync` 与裸
  `uv run`（会按 3.11 pin 重建共享 `.venv`），系统 Python 也不是替代。一律用活动
  解释器 + `-m`：

  ```bash
  cd /scratch/frd_muziyao/NWM
  /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli <subcommand> ...
  ```

- 验证未合入的分支时不要 `git pull` 共享 checkout：`git worktree add --detach` 到
  `/scratch/frd_muziyao/tmp/<wt>`，**先 `cd <wt>`** 再
  `PYTHONPATH=<wt> /scratch/frd_muziyao/NWM/.venv/bin/python -m ...`，并确认
  `services.orchestrator.__file__` 位于 worktree 内；用完 `git worktree remove`。
- 本文所有命令都不连 DB。写 journal 的命令默认 dry run，只有显式 `--attest` /
  `--execute` 才写。

## 第一步：列出等待 operator 的候选（`list-operator-actions`）

`list-operator-actions`（#1186）是只读列举面：不连 DB、不读 journal（决策只在 evidence 里）、
不写任何字节。它扫 evidence root 顶层按 mtime 最新的 `--passes` 份终态 pass 文件
（`scheduler_*.json`；`*.pre_execution.json` 不是终态 pass，不计），**按 decision 字面**列出第二步那张表里
**五类**等待 operator 的候选（不看 `manual_retry_required` 布尔，所以 bounded 摘要 pass 里的条目照样
列出），并额外列出 breaker 释放了执行槽的 backfill cycle 的每个模型——这类 cycle 不构造候选，
只出现在 not-selected `source_cycles` 里。

- **evidence root 取值**（#2399）：`NHMS_SCHEDULER_EVIDENCE_ROOT` 必须取自 systemd unit
  `nhms-compute-scheduler.service` 实际加载的 `infra/env/compute.scheduler-dbfree.env`。
  node-22 checkout 里的 `infra/env/compute.env` 已漂移，**不要**用它。下面的命令若报 exit 2 或
  `passes_scanned: 0`，说明 root 取错或该 root 下没有 pass——结论无效，先核对 root。

```bash
cd /scratch/frd_muziyao/NWM
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli \
  list-operator-actions \
  --evidence-root "$NHMS_SCHEDULER_EVIDENCE_ROOT" \
  --passes 6
echo "exit=$?"
```

`--evidence-root` 省略时取 `$NHMS_SCHEDULER_EVIDENCE_ROOT`；`--passes` 默认 6，必须是 ≥ 1 的整数
（否则 exit 2，与 root 取错同码）。输出是一行 sorted-key JSON receipt，退出码带语义：

| exit | 含义 | 处置 |
|---|---|---|
| `1` | 列出了至少一条待办（`operator_actions` 非空） | 按第二步逐条处置 |
| `0` | 扫过的窗口里**没有在册的五类待办，且窗口本身可信** | 不用动手；但它不是健康检查，见下 |
| `3` | **无法判定**——命令本身跑成功了，只是这批 pass 不足以下结论 | 读 receipt 定位原因，等下一趟范围完整的 pass 再跑；**不得**当成 `0` |
| `2` | **root 级**用法错误：evidence root 缺失/不可读（多半是 root 取错），或 `--passes` 不是 ≥ 1 的整数 | 核对上面的 root 口径与 `--passes` 后重跑 |

**`2` 只表示 root 级失败。** 单个 pass 文件读不了——半写、超 5 MB、扫描过程中被 retention
删掉——都**不是** `2`：它们计入 `unreadable_passes`，扫描继续，其余 pass 照常列出，结果最差是 `3`。
所以看到 `2` 就去查 root 与 `--passes`，不要去查单个文件。

receipt 字段：`operator_actions` / `operator_action_count`（每条带 `candidate_id`、`source_id`、
`cycle_time`、`model_id`、`decision`、`reason`、`attempt`、`retry_limit`、`occurrences`、
`recorded_init_state_id`、`first_seen_pass` / `last_seen_pass` / `seen_in_passes`）、
`unreadable_passes`、`candidate_lists_dropped_passes`、`non_evaluating_passes`（每条带
`pass` / `status` / `reason`）。

- **合并了多趟的条目，值来自哪一趟**：同一候选在窗口里出现多趟时只列一条，除
  `first_seen_pass` / `seen_in_passes` 外**所有值字段都取 `last_seen_pass` 那一趟**（`candidate_id`
  在最新趟没有时——breaker 释放腿本来就没有——回退到已知值）。这条对
  `recorded_init_state_id` 尤其重要：拿它去 `confirm-operator-reentry` 的 pin 必须是**当前**
  token，陈旧 token 会在 dry run 之前就被拒。

- **`3` 是"无法判定"，不是"出错"**。命令正常跑完并打了 receipt，只是窗口里没有一趟
  「候选构造跑过、证据完整、范围完整」的 pass 可以为"没有待办"背书。成因看
  `non_evaluating_passes[].reason`：
  - `status_not_evaluating`——终态 status 不在候选构造之后写的闭合白名单里（`lock_contended`、
    `preflight_blocked`、`lease_lost`、异常路径的 `resource_limit_blocked`，以及任何未来新增的
    未知 status 一律落在这一侧）。**凡是不在该白名单里的 status，一律按不可判定处理并升级上报，
    不是只向前等**：白名单是闭合集合，将来新增的候选构造后终态 status 若不在表里且成为常态，
    本面会**持续** `3`，向前等永远等不到；口径与下面 `scope_unknown` 一致；
  - `size_fallback_source_cycles_absent`——超 5 MB 预算的 size fallback 产物，写入器清空了
    `source_cycles`，breaker 释放的 cycle 因此看不见（它自己摘要里的 blocked 候选仍会被列出）；
  - `scope_narrowed`——这趟 pass 的范围被收窄了，见下一条；
  - `scope_unknown`——status 可求值，但 pass 文件里下面这些字段**有任一不在**，范围无从判断，
    按"可能只看了一部分"处理：
    - `backfill.enabled`；
    - `operator_filters` 的 `basin_ids` / `model_ids` / `expression`；
    - 顶层 `sources`；
    - `cycle_window.lookback_hours`；
    - `counts.selected_model_count`；
    - `runtime_config.allowed_cycle_hours_utc`。

    整个 `backfill` / `operator_filters` / `sources` / `cycle_window` / `counts` / `runtime_config`
    键缺失、键在而字段残缺、`sources` 不是字符串列表、`lookback_hours` 或 `selected_model_count`
    不是整数（`true` 也**不算**整数）、`allowed_cycle_hours_utc` 不是整数序列，**都算**。
    分界线：**字段在、值收窄 → `scope_narrowed`**（运维自己下的指令）；**字段不在 →
    `scope_unknown`**（读不到，缺的字段不会被当成空默认值）。
    这些字段是写入器**无条件**写出的（`services/orchestrator/scheduler_evidence.py:248-253` 是
    四键 dict 字面量，同文件 `:268` 写 `sources: list(config.sources)`、`:270-276` 写五键
    `cycle_window`、`:293-297` 写 `runtime_config` 且此后从不被覆盖；
    `services/orchestrator/scheduler_runtime.py:1346-1350` 写 `counts`，`:1394-1402` 的 if/else
    两条腿都写 `backfill` 且都带 `enabled`），所以线上真出现 `scope_unknown`，说明这份 pass 文件
    被截断或被外部改过——**属于要查的异常，不是正常状态**，按异常上报而不是只等下一趟。
  - `no_models_evaluated`——`counts.selected_model_count == 0`：这趟 pass **一个模型都没求值**，
    既不是范围收窄（没有任何运维指令），也不是读不到（值是明确的零）。可达且不需要任何运维操作：
    db-free 注册表清单允许 `models: []`（`services/orchestrator/scheduler_file_providers.py:883-940`
    只校验 schema/新鲜度/校验和/上限，**没有下限**），清单非空但被
    `services/orchestrator/scheduler_models.py:137-145` 全部排除（inactive / not_runnable /
    not_shud_model / 元数据不全 / 重复身份）同样落到零模型，而 registry 仍是 `ready`。
    这类 pass 的证据里 `backfill.enabled` **仍是 `true`**（写侧记的是**配置**的腿，不是实际走的腿），
    所以只有这个计数说实话。处置：**查注册表清单**（它为什么筛空了），与 `scope_unknown` 同档上报，
    不要只等下一趟。线上历史：2026-09-16 实测每一趟 `selected_model_count` 恒为 76，无一例外，从未为零。

  另外 `candidate_lists_dropped_passes` 非空（`limit.candidate_lists == "dropped"`）也是 `3`：
  丢了候选列表的 pass 既不能列出待办，也不能证明没有待办；`unreadable_passes` 同理。
  node-22 的 pass 文件接近 5 MB 上限，size fallback 现实中会出现。
  `unreadable_passes` 里还会出现**扫描期被删掉**的文件（retention timer
  `nhms-scheduler-evidence-retention.timer` 与本命令并发时的正常现象）：这类文件连 mtime 都没读到，
  在时间序里定不了位，因此按**全局否决**处理——不论它落在哪个位置，本趟结果都是 `3`。隔一会儿重跑
  即可，不必去查 root。
  **除上面明确要求升级上报的三条**（不在白名单里的未知 status、`scope_unknown`、
  `no_models_evaluated`）外，其余成因的处置是
  **向前等**：隔一会儿重跑本命令，让窗口里出现新的范围完整 pass，**不要**自己去翻更旧的
  pass 判"没有待办"——断路器可能在那趟旧 pass 写完之后才释放了某个 cycle。
- **窄范围 pass 不构成"没有待办"的证据**。`plan-production` 带 `--disable-backfill`、
  `--model-id` / `--basin-id`、`--source`、`--lookback-hours 0`，或把允许的 cycle 小时收得比生产
  默认还窄（pass 文件里分别落成 `backfill.enabled=false`，`operator_filters` 的 `model_ids` /
  `basin_ids` 非空或 `expression` 非 null，顶层 `sources` **没覆盖**全集 `gfs` + `IFS`，
  `cycle_window.lookback_hours <= 0`，以及 `runtime_config.allowed_cycle_hours_utc` **没覆盖**
  代码默认 `(0, 12)`）跑出来的 pass，没列出待办只说明**这个子集里**没有；尤其 breaker 释放的条目
  **只在 backfill 腿产生**，一趟关掉 backfill 的 pass 结构性看不见它；同理零宽时间窗（`lookback 0`，
  `--cycle-time` 也会把它设成 0）对**所有更旧的 cycle 全盲**，而 breaker 释放按构造坐在最旧那侧
  （`services/orchestrator/scheduler_discovery.py:824-832`）；cycle 小时集合则直接在
  `discover_cycles` 里把不在集合内的 cycle 过滤掉。`--source` 与 cycle 小时判的都是**覆盖**不是
  相等：`--source gfs --source IFS --source ERA5` 确实把 gfs/IFS 都看过了，算范围完整；
  `allowed_cycle_hours_utc = [0, 6, 12, 18]` 同理把默认的 0/12 都看过了。只有子集才算收窄。
  **cycle 小时的完整性权威是代码默认值，不是 0-23 全天**：生产就跑默认 `[0, 12]`，按全天判会把
  每一趟生产 pass 打成收窄，制造假 `3`。
  命令把这类 pass 以 `scope_narrowed` 计入 `non_evaluating_passes`（它自己范围内的待办照样列出），
  既不清除也不拉响更早那趟隐藏 pass 的警报。**整窗口都是窄范围 pass 时结果是 `3`，不是 `0`**。
- **`0` 的四条已知边界（已裁决，不是缺陷，但读 `0` 时必须知道）**：
  1. **时间窗**：`0` 断言的是「这趟 pass 在**它自己的**时间窗
     `[cycle_window.start_time_utc, cycle_window.end_time_utc]` 内没有待办」。生产跑
     `lookback_hours=32` + `cycle_lag_hours=16`，即最近 16 小时的 cycle 不落在任何窗口里。仓内
     不存在可比的「完整时间窗」权威（生产 32、代码默认 24），所以窗口宽度本身不判收窄，只有零宽
     那个退化值判。
  2. **单槽**：backfill 腿每源每趟只求值**最旧的一个**未完成 cycle
     （`scheduler_discovery.py:836`），更新的 gap 记为
     `backfill_deferred_waiting_for_prior_cycle`。**不要读得比实际悲观**：`remaining_gaps` 是
     老→新排序（`:733`），被推迟的是**更新的** gap；一条没解决的待办会让它自己那个 cycle 一直是
     gap、继续占着槽，于是——**只要那个 cycle 仍落在后续各趟的发现范围内**（见第 4 条）——它每趟
     都被重新求值、重新列出。真正被推迟的是「更新 cycle 上的待办」，
     而它们在旧 cycle 清空之前也无法被创建。
     **`blocked_journal_predecessor_identity_quarantine` 不受此限**：断路器释放在 `:824-832`
     完成、逐条证据在 `:844-853` 写出，**都在 `:836` 的单槽切分之前**，且释放的是「从最旧起连续
     的全部」，所以这条决策的可见性是完整的。
     这条边界**与 `max_cycles_per_source` 无关**——那个参数对任何 `backfill.enabled=true` 的
     pass 完全 inert（截断在 `:904-906` 的 `_select_legacy_source_cycles` 里，只在不走 backfill
     腿时执行，见 `:700`）。
  3. **inactive 模型**：`0` 对注册表清单里标为 inactive 的模型**不作任何断言**。
     `model_discovery.registry.model_count` 数的是清单里**登记的总数**（含 inactive，取自
     `services/orchestrator/scheduler_file_providers.py:936` 的 `len(rows)`），而
     `model_discovery.active_model_count` 数的是 `list_models(active=True, …)` 的返回，**那道过滤
     发生在 `discover_models` 拿到行之前**（`:152-157`）。因此这段落差 `model_discovery.exclusions`
     **结构上记录不到**——`exclusions` 只能解释「已经进了 rows 之后」被丢弃的模型。
     **为什么不判**：要求两数相等等于规定清单里不许留退役模型，清单退役一个模型就会把生产**永久**
     翻成 `3`；仓内不存在「清单该含多少模型」的权威，与本 change 拒绝为时间窗发明阈值同理。
     查法：直接比 `registry.model_count` 与 `active_model_count` 的差值，别去 `exclusions` 里找。
     2026-09-16 实测每一趟 `model_count = active = runnable = selected = 76`、`exclusions = []`，
     无一例外落差为零——这条边界今天在生产上是空的，但它结构上存在。
  4. **发现面收回（discovery retraction）**：`0` 对「更早、更宽的配置能看见而当前配置看不见的
     cycle 上的待办」**不作任何断言**。第 2 条的缓解之所以带「只要 cycle 仍在发现范围内」这个限定，
     就是因为它依赖**重新发现**，而重新发现跨配置变更不成立：小时过滤
     （`scheduler_discovery.py:728` 的 `_filter_allowed_cycle_hours`）跑在 `:744` 的单槽
     `_select_backfill_source_cycles` **之前**，而候选只从本趟已发现的 cycle 构造
     （`scheduler_candidates.py:257` 的 `for cycle in cycles:`）。于是一个 cycle 的小时离开
     `allowed_cycle_hours_utc`（或 `lookback_hours` 调小把它移出窗口）之后，它不是被「推迟」，而是
     **不再被发现 → 不再产生候选 → 它已经列出过的未决待办从 `blocked_candidates` 与
     `source_cycles` 里同时消失**。
     **为什么不判**：读侧只看得到当前这趟 pass 自己的配置，仓内不存在「上一次配置是什么」的权威，
     与第 1、3 条拒绝发明阈值同理。`allowed_cycle_hours_utc` 是**环境变量专有**旋钮
     （`cli.py` 无对应 flag），要走到这一步需要刻意地把服务环境放宽再收回，不是普通一跑。
     这条边界**与 `max_cycles_per_source` 无关**（理由同第 2 条：它对 `backfill.enabled=true` 的
     pass 完全 inert）。
     **与第 2 条不同，这一条对 `blocked_journal_predecessor_identity_quarantine` 没有豁免**：
     断路器释放走的是 `_select_backfill_source_cycles` 里**已经过小时过滤**的 `discoveries`
     （`:728` 在 `:744` 之前），所以被收回的 cycle 对它同样不可见。
     查法：怀疑收回过就把 `runtime_config.allowed_cycle_hours_utc` 与 `cycle_window.lookback_hours`
     在窗口内各趟之间比一遍，变过就别把 `0` 读成「全都没待办」。
- **`0` 的含义是"没有在册的五类待办"，不是"调度器健康"**。本面只认第二步表里那五个 decision 字面；别的
  error_code 再多、再红，它也不会出现在 `operator_actions` 里。实例（#2432）：2026-09-15 13:45 CST
  起 node-22 的 `raw → forcing → runs` 停止推进，2026-09-16 实测最近 20 趟共 760 条 blocked
  candidate 全部是 `error_code=FORCING_VERSION_ROW_ABSENT` 且 `manual_retry_required=False`——
  这批候选不在那五类之列，于是本命令在管线已经死了一天多的情况下**照契约返回 `exit 0`**。
  **不要拿它当健康检查**，管线是否推进看各自的监控面。
- **窗口之外看不见**：只扫最近 `--passes` 趟，这是定义不是缺陷；`3` 的存在就是为了不把"窗口内
  没看见"说成"没有"。需要更宽的窗口就加大 `--passes`（无上限，但每份 pass 文件接近 5 MB，按需
  加）。
- **摘要 pass 的 `null` 字段怎么读**。bounded 摘要（`limit.candidate_lists=summarized`）丢了
  `state_evidence`，但保留 `decision`、`recorded_init_state_id`，以及 `retry_attempt` /
  `retry_limit` / `retry_occurrences` / `manual_retry_required` 里**产出臂实际写过的那几个**
  （保留判据是 `value is not None`，所以 `0` 和 `false` 也留得住）。这几个键不是每臂都有：
  - 预算臂（`blocked_strict_warm_start_init_state_mismatch`）写 `attempt` / `retry_limit` /
    `manual_retry_required`，**从不写** `occurrences`；
  - 断路器臂（`blocked_journal_predecessor_identity_quarantine`）写 `occurrences` /
    `manual_retry_required`，**从不写** `attempt` / `retry_limit`；
  - sink 拒绝臂（`blocked_operator_reentry_restart_stage_refused`）的 `retry_policy` 是
    `{"automatic_retry_allowed": false, "manual_retry_required": true, **_OPERATOR_REENTRY_POLICY}`
    （`services/orchestrator/scheduler_candidates.py:1673-1678`），而 `_OPERATOR_REENTRY_POLICY`
    （`:2665-2668`）**只有两个字符串键、不含任何数字键**，所以 `attempt` / `retry_limit` /
    `occurrences` **三个全是 `null`**。这一臂本来也不走 `confirm-operator-reentry`（见第二步的表）。

  receipt 把缺的那些渲染成 `null`。这里的 `null` 意思是**这一臂从来没写过这些字段**，不是
  「scheduler 把数字弄丢了」——不要据此判断摘要有损、更不要据此去翻非摘要 pass 找那个数。
  `recorded_init_state_id` 曾经是个例外（摘要没投影它，读侧也只从 `state_evidence` 单读，于是
  最新一趟一旦是 size fallback 就报 `null`）；#1186 round 2 起写侧保留、读侧行级双读，摘要 pass
  上**也会给出真值**。
  breaker 释放的 backfill cycle 不构造候选，它列出来的条目没有 `candidate_id`（为 `null`），
  同理不是丢了数据。

## 第二步：按 decision 处置

| decision | 入口 |
|---|---|
| `permanent_failure` | `scripts/node22_manual_retry_failed_runs.py`（manual-retry marker） |
| `cancelled_manual_retry_required` | `scripts/node22_manual_retry_failed_runs.py`（manual-retry marker） |
| `blocked_journal_predecessor_identity_quarantine` | `confirm-operator-reentry`（§8.7 断路器） |
| `blocked_strict_warm_start_init_state_mismatch` | `confirm-operator-reentry`（strict warm-start 预算） |
| `blocked_operator_reentry_restart_stage_refused` | **不要再签一次**；带外修好 forecast 之前的输入，见下面「已知限制」的 sink 拒绝那条 |

### `permanent_failure` / `cancelled_manual_retry_required`

失败/取消的 run 不会自己重试。先修掉原因（例如 forcing 回补），再打一次性
manual-retry marker（`FileJournalRetryService.record_manual_repair`，取 cycle 写锁，
run 在飞或不存在时拒绝）：

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python scripts/node22_manual_retry_failed_runs.py \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --run-id "<run_id>" \
  --reason "<why>" \
  --requested-by "<operator>"
# 预览无误后追加 --execute
```

### 两类 completed-skip fail-stop：`confirm-operator-reentry`

断路器与预算两类 blocked 的 journal 行是 terminal-success，manual-retry marker
**到不了**它们（`record_manual_repair` 拒绝 terminal-success 行，且 completed skip
在读 marker 之前就返回）。它们的 `retry_policy` 带
`operator_reentry_command: "confirm-operator-reentry"` 与
`recovery_runbook: "node22-control-plane-manual-recovery"`。

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli \
  confirm-operator-reentry \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --source-id gfs --cycle-time 2026-05-21T00:00:00Z --model-id <model_id> \
  --decision blocked_journal_predecessor_identity_quarantine \
  --pin <quarantine_rerun_count> --recorded-init-state-id <recorded_init_state_id> \
  --operator "<operator>" --reason "<why>"
# dry run 默认；核对 receipt 后追加 --attest
# 预算：--decision blocked_strict_warm_start_init_state_mismatch --pin <budget_reentry_count>（不带 token）
```

- 写入一条 `forecast_cycle` pipeline event，`event_type=operator_reentry_confirmation`
  （不是 `retry` / `manual_retry`，不会被当成 manual-retry marker）。
- **一次一授权**：`--pin` 必须等于 rerun 会推动的那个 live 值——
  - 断路器：该模型的 **quarantine rerun 计数**（provenance 命名该模型的 cohort master 数，
    不看终态、不分 token）。它**不是** blocked evidence 里的
    `occurrences`（那是按 token 的带戳计数，只用来判断断路器是否触发）。先不带
    `--attest` 跑一次，从 dry-run receipt 的 `live.quarantine_rerun_count` 读出 pin；
    `--recorded-init-state-id` 取 `recorded_init_state_id`，必须等于 live 记录 token（写侧
    意图前置条件，读侧不再比较 token）。**取这个 token 时第一步的命令要加 `--passes 1`**：
    要的是**当前**这趟的 token。合并 receipt 的值字段虽然已经取自 `last_seen_pass`，但只扫最新
    一趟能让"这条待办现在还在不在"与 token 新鲜度一起确定；token 过期会在 dry run 之前就被拒
    （`reason=recorded_init_state_id_mismatch`），而拒绝消息不会告诉你它只是旧了。
  - 预算：该模型的 **预算重入计数**（预算重入 provenance `strict_warm_start_budget_reentry_model_ids`
    命名该模型的 cohort master 数，不看终态、job id、retry 后缀），不需要 token。它**不是**
    blocked evidence 里的 `attempt`（`attempt` 只决定是否 blocked）。
    先不带 `--attest` 跑一次，从 dry-run receipt 的 `live.budget_reentry_count` 读出 pin。
    **写侧看不到预算是否已耗尽**（#2400 残余，本 PR 不关闭：pin 对了、时间错了）：耗尽前写入的
    确认物在 pin 仍等于计数时一直有效，直到被消费——预算一耗尽就会在没有新签字的情况下放行一次。
    所以只确认**最新** pass evidence 文件当前列为
    `blocked_strict_warm_start_init_state_mismatch` 的目标——第一步的命令加 `--passes 1`（只扫最新
    一趟），取 receipt 里 `decision` 等于该值的 `operator_actions` 条目——并逐字核对 `source_id` /
    `cycle_time` / `model_id`；该模型的 rerun 仍在飞时不要确认。写错（目标、pin 或时机）时停止并
    上报，**不要**再写一条覆盖（旧确认物仍有效）。
  scheduler 只在 pin 严格相等时放行一次。确认物在 rerun **被接受提交**时即被消费：
  provenance 戳在 accepted-submit（reservation）时写入 cohort master，对应计数当场 +1
  （无论 rerun 之后成功、失败，断路器也无论记录了哪个 token，预算也无论 rerun 落在哪个 job-id
  前缀下）。fail-stop 自行重新接管，不需要撤销。
- 拒绝时不写任何字节、打印 `decision=refused` receipt 并 exit 2。`reason` 取值：
  `required_argument_blank`、`decision_not_reentry_eligible`、`cycle_time_invalid`、
  `pin_invalid`（pin 为负）、`completed_identity_absent`、`recorded_init_state_id_mismatch`、
  `breaker_not_engaged`、`pin_mismatch`。`recorded_init_state_id_mismatch` 与
  `breaker_not_engaged` 只适用于断路器；`pin_mismatch` 两类都适用（断路器比
  `quarantine_rerun_count`，预算比 `budget_reentry_count`）。断路器的检查顺序为 `breaker_not_engaged` →
  `recorded_init_state_id_mismatch` → `pin_mismatch`，拒绝 receipt 的 `live` 同样带
  `occurrences` 与 `quarantine_rerun_count`；预算拒绝 receipt 的 `live` 带 `budget_reentry_count`。
  journal root 不可信时 stderr 为 `FILE_JOURNAL_INVALID_ROOT: ...`，exit 2。
- 不改 `NHMS_SCHEDULER_RETRY_LIMIT`，也不改两处 forced-resubmit 白名单。

预期 evidence：

- 确认后的下一 pass：该候选进 `candidates[]`，decision 回到
  `retry_journal_predecessor_identity_mismatch` /
  `retry_strict_warm_start_terminal_init_state_mismatch`，`state_evidence` 带
  `operator_reentry_confirmation: {request_id, operator, reason, pin, decision}`；
  断路器几何下该 cycle 保留 backfill 执行槽（不再出现 breaker not-selected 条目）。
- rerun 在飞期间：候选是 active，`submitted_count` 不因它增加。
- rerun 被接受提交后（在飞、完成或失败）：断路器 `quarantine_rerun_count` / 预算
  `budget_reentry_count` 已 +1，pin 不再相等，之后的 pass 不再放行（回到 blocked；Slurm 层失败的
  预算 rerun 可能改落为 `permanent_failure`）。

各决策的判读细节见
[`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md)
（§8.7 断路器）与 [`failed-basin-retry.md`](failed-basin-retry.md)（strict 预算）。

## 已知限制

- **Slurm 层失败的 rerun 不会恢复确认物**：计数在 rerun 被接受提交时已经 +1，失败不回退。
  需要再次重入时，重新跑 dry run，用新的 live 计数再确认一次。
- **forcing 见证闸（#1844）**：确认物匹配但该模型没有自己的 forcing 时，候选落到
  forcing 缺失的具名 blocked（reason `forcing_version_row_absent` /
  `missing_forcing_package_uri`），不提交；断路器几何下该 cycle 会持续占用执行槽。确认物
  **没有撤销手段**——先回补 forcing。两条 fail-stop（断路器、strict 预算）同此。
- **确认物与 `--repair-missing-forcing` 互斥**（r2-01；两条 fail-stop 都适用）：同一 cycle
  开着单 cycle 修复时，带 `operator_reentry_confirmation` 块的候选**不会被改判、不提交、
  不消费**，确认物保持待用。证据按车道分：
  - strict warm-start 车道（预算臂，以及落在该车道的断路器重入）：修复策略被调用并拒绝，
    `state_evidence.missing_forcing_repair = {status: rejected, reason:
    operator_reentry_confirmation_present, confirmation: {decision, request_id}}`，候选留在
    上面那个 missing-forcing blocked 上。
  - 非 strict 车道的 §8.7 断路器重入：修复策略**根本不会被调用**（调用点在
    `strict_warm_start is not None` 之内），候选只带见证闸的 missing-forcing blocked，
    **没有** `missing_forcing_repair` 键——结果相同，别去找那个键。
  理由：被改判的 `retry_repair_missing_forcing` 从 `forcing` 阶段重启，而重入 provenance 只在
  forecast cohort 的 reservation 处写；forcing 跑成功才顺带戳到，跑失败就是「真提交了、计数没动、
  确认物还在，且新 run-id 前缀把 stage 域 attempt 打回 0/2 让预算判定失效」——一次签字放行两次
  forecast 重入。所以这条路被整体拒绝，而不是赌 forcing 会成功。
  **正确顺序：先把该模型自己的 forcing 补回来，再让确认过的重入跑**——它从 `forecast` 重启、在
  reservation 处被戳、计数 +1，**恰好消费一次**，之后的 pass 回到 blocked、旧 pin 返回
  `pin_mismatch`。`current-production-ops.md` 里 strict 车道走 `--repair-missing-forcing`
  的处置流程同理：带确认物的候选不走那条通道。

  **"补回来"有前提，别默认就是回补脚本**：`scripts/node22_backfill_forcing_for_model_ids.py`
  是**纯改名工具**——它要求同时给出改名前后两份 registry manifest
  （`scripts/node22_backfill_forcing_for_model_ids.py:605-606`），待办**完全**由改名集推导
  （`discover_work` 在 `:409`，`for rename in renames` 在 `:420-421`；`main` 的
  `resolve_renames → probe_coverage → discover_work` 流水线在 `:648-651`）。
  **只有"该模型的 forcing 以另一个 model id 存在、即改名集非空"时它才有活儿**。不是改名造成的
  forcing 缺失，它返回 `work_item_count: 0`（receipt 字段在 `:705` / `:715`），而这个形状与
  `--forcing-root` 指错、NFS 没挂、环境不对**在条目数上分不开**——所以先读 receipt 的
  `coverage`，见 `current-production-ops.md` §3.1.1。非改名成因是真实存在的：retention 会删除
  primary root 下的 `forcing/<source>/<cycle>/...`（`services/orchestrator/retention.py:75`、
  `:684-687`），即 fail-stop 生效之后 forcing 仍可能在带内消失。

  **改名集为空时的升级路径**：回补脚本不是通道，`--repair-missing-forcing` 也不是。只能**带外**
  把 forecast 之前的那份输入修好（重新产出该模型的 forcing 包并落进 object store；或修好
  canonical readiness / raw manifest 身份），修好之前确认物保持待用、候选保持 blocked。修好后的
  下一趟自然 pass 让确认过的重入从 `forecast` 重启、被戳、计数 +1，恰好消费一次。
  （retention 的删除前沿是否钉住 blocked / 带确认物的候选**尚未实测**，两个方向都不要断言；
  设计缺口记在 #2412。）

- **§8.7 quarantine 后代与 `--repair-missing-forcing` 互斥**（#2408；未带确认物的候选）：
  strict warm-start 车道上，§8.7 journal-predecessor quarantine 重试
  （`retry_journal_predecessor_identity_mismatch`）落到 missing-forcing blocked 后，修复策略
  同样拒绝改判：`state_evidence.missing_forcing_repair = {status: rejected, reason:
  journal_predecessor_quarantine_present, recorded_init_state_id, expected_init_state_id}`
  （两个 id 即 `journal_predecessor_identity` 里的陈旧前驱 token 与 journal 期望的 token，排查
  用这两个字段），候选留在 missing-forcing blocked 上、不提交。理由同 r2-01：改判后从 `forcing`
  重启，而 quarantine provenance 只在 forecast cohort 的 reservation 处写，forcing 失败就是
  「真提交了、断路器计数没动」。**处置同上**：不用 `--repair-missing-forcing`，先把该模型自己的
  forcing 补回来——改名集非空用 `scripts/node22_backfill_forcing_for_model_ids.py`，否则按上面的
  升级路径带外修复；之后 quarantine 重试从 `forecast` 重启、在 reservation 处被戳并计数。

- **sink 拒绝 `blocked_operator_reentry_restart_stage_refused`**（#1555 round 4）：确认物匹配，
  但候选实际会重启的阶段不是 `forecast`（典型是 canonical 不 ready + raw manifest 就绪触发的
  `convert` 改写）。判定在候选清单构建完成后统一做一次，读候选自己的 `state_evidence`，正向比较
  `== "forecast"`，阶段缺失/`null`/空串以及 `fresh_ingestion.mode == "full_chain"`（run manifest
  会被剥掉 `restart_stage`）一并拒。**不提交、不消费，确认物保持待用**，该 decision 不在两处
  forced-resubmit 白名单里。处置：读 `operator_reentry_sink_refusal.refused_restart_stage` 找到改写
  源头，**带外**修好 forecast 之前的那份输入（canonical readiness index 或 raw manifest 身份），
  下一趟 pass 自己从 `forecast` 重启并消费一次签字。**不要重复签一次**——旧确认物仍然有效。
  **evidence 被有界摘要压过时**：`operator_reentry_sink_refusal` 这个块里只有
  `refused_restart_stage` 被保留（摘要里是同名的行级键），块内**其余字段一律消失**——
  普通拒绝里是 `refused_restart_from_stage` / `effective_restart_stage` /
  `fresh_full_chain` / `confirmation`，确认物损坏那类还多一对 `malformed_confirmation` /
  `confirmation_type`。要看这些字段必须回到未摘要的整份 pass evidence。
  字段与逐项处置见
  [`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md)。
- **候选仍显示 blocked ≠ 确认物未生效**：先看 dry-run receipt 的 live 计数是否已 +1（file
  journal 的 `hydro_run` 在同一 `run_id` 重跑时不更新，#2397，即使 rerun 拿到正确 lineage
  候选也仍显示 breaker-blocked）；已 +1 就**不要重复确认**——除非该 rerun 已到失败终态且
  operator 仍要重入，此时重新跑 dry run，用新的 live 计数再确认一次。**rerun 在飞期间不要写
  新的确认物**：写侧不检查在飞状态，按新计数写下的确认物会在该 rerun 结束后放行下一次。
- **`recover-released-identity-blocked-reservation` 列表模式的逐行隔离**：receipt 的
  `skipped[]` 只覆盖 flat `pipeline-jobs/` 行在首轮扫描与逐 cycle 确认 replay 中的"单行内容
  校验"类原因（`ROW_CONTENT_SKIP_REASONS`），逐行跳过、按 `(path, reason)` 去重，同 cycle 的
  wedged 行照常列出；cycle 的 `journal/` 日志或 `latest/` 视图损坏不属于"行"，照旧 raise；预算拒绝、不可读、containment 类故障照旧 fail closed。
  没有 cycle scope 时走的 unscoped 全树 fallback **不隔离**，受 `full_tree_replay`
  预算契约约束，生产规模下会先撞预算。
