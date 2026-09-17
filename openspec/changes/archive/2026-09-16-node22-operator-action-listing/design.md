# Design — node-22 运维待办列举面（#1186）

## Risk triage

- **fixture level：`high`**（与父 PR #2398 一致，不降级）。理由：本面是**运维契约**——它的 `exit 0` 会被人读成"不用动手"，判错方向即静默失效；而 #1186 已在 #2398 触过轮次上限，`.review-gate-issues.json` 让本 PR 开局即升级。降 fixture 等于在已知高风险面上少买审查，不做。
- **Minimal mergeable slice**：恢复的 listing 模块 + r5-01 的范围判定 + 其覆盖测试。r5-00 / r5-03 的排序表补行与 runbook 段落替换同批交付，但它们不是可合并性的前提。
- **must-preserve**：本面**只读**。不写 journal、不提交、不改决策；`list-operator-actions` 的任何路径都不得产生副作用。退出码三分语义（0 / 1 / 3）是契约本身，不得为了让某条测试变绿而模糊。

### 风险轴与选用的 reviewer pack

| 轴 | 选用 | 理由 |
|---|---|---|
| 运维契约正确性 | ✅ `correctness` | 退出码判错即静默失效 |
| 不变量/状态 | ✅ `invariant-state` | "可判定"是一个跨 pass 的状态机（clear / leave / arm 三态） |
| 取证与规格一致 | ✅ `test-evidence`+`spec-compliance` | round-5 三条全是覆盖/规格类 |
| 安全与性能 | ❌ 不选 | 只读、无网络、无 DB，且**单文件有 5 MB 硬截断**（`operator_action_listing.py:252-255`，超限即判不可读、不解析），读取面因此有界 |

> 上面那条「不选」的理由第一版写的是「pass 文件 4.2 MB × N 的读取成本由 `--passes` 上限约束」，**两个数都没来源，且后半句实测为假**：模块只拒绝 `passes < 1`（`operator_action_listing.py:147-148`），**没有上界**。真实体积也不是"4.2 MB"这个拍出来的数，实测见 D2b。理由已换成真正成立的那条（单文件硬截断）。fixture 审查的 Note-01 抓的就是这句。

## D1 — 为什么需要「范围完整」这个概念（r5-01）

`exit 0` 的含义是**"扫过的窗口里没有待办，且窗口本身可信"**。round 5 发现第二个分句在窄范围 pass 上不成立：

- 列举的决策里，`blocked_journal_predecessor_identity_quarantine` 条目**只在 backfill 腿产生**（breaker 释放的 cycle 不进候选清单，只作为 not-selected source cycle 出现）。一趟 `--disable-backfill` 的 pass 因此结构性地看不见它。
- `--model-id` / `--basin-id` / `--expression` 收窄同理：没列出待办，只说明**这个子集里**没有。
- `--source` 是第三个收窄维度（round 1 A2）：`scheduler_evidence.py:268` 无条件写顶层 `sources`，一趟 `--source gfs` 的 pass 从没看过 IFS。它**判不了空**——`cli.py:421` 在没给 `--source` 时回填全集，`sources` 恒非空——所以按集合与权威全集比较。全集在 `scheduler.py:292` 的 `DEFAULT_PRODUCTION_SOURCES`，但 `scheduler.py` 有 35 个顶层 import 与 lease-compat facade，不能被 DB-free 的列举面拖进来：本模块留本地字面量 `SCOPE_COMPLETE_SOURCES`，由测试里的三方一致性 pin（AST 只读 `scheduler.py` / `cli.py`，不 import）防漂移。

原实现把这类 pass 当作可判定，于是它会**清零** `hidden_after_decidable`，把一个更早的隐藏 pass 的警报抹掉，给出假 `exit 0`。

### 三态而非两态

隐藏标志有三种动作，本 change 给窄范围 pass 选第三种：

| pass 类别 | 对 `hidden_after_decidable` 的动作 | 理由 |
|---|---|---|
| 范围完整且可判定 | **清零** | 它确实看过全部 |
| `TRANSPARENT_PASS_STATUSES`（`lock_contended` / `preflight_blocked`） | **保持** | 已知没有隐藏任何东西 |
| **范围被收窄** | **保持**（本 change 新增） | 没看全，不能清零；但收窄是运维自己下的指令，不构成"意外隐藏"，不该拉警报 |
| 其余（不可读、size fallback、未知 status…） | **置位** | 可能藏了东西 |

**可求值但 scope 键缺失，归到最后一行（置位），不是单独的第五态。** 这是 fixture 审查第二轮的裁决点，写明取舍：一趟 status 可求值、却读不到范围的 pass，范围无从判断，因此它**可能**只看了一部分——与"size fallback 可能藏了东西"是同一类不确定性，用同一个动作（置位）表达，并以 reason `scope_unknown` 计入 `non_evaluating_passes`。

**"读不到"的判据是键的有无，不是值。** 第一版只查了 `backfill` / `operator_filters` 两个**顶层**键在不在，Phase 2 的探针实测出两个洞：`operator_filters` 是**空 mapping** 时（三个过滤键都不在，`.get` 全返回假值）被判成范围完整 → 清零标志 → 可产生 `exit 0`；`backfill` 是空 mapping 时（没有 `enabled`）被判成 `scope_narrowed` → 不置位。两者都是 r5-01 的失败类换了个入口。修正后的判据是：`backfill.enabled`、`operator_filters` 的 `basin_ids` / `model_ids` / `expression`，以及顶层 `sources`（round 1 A2 补的第六个）**六个字段有任一不在即 `scope_unknown`**；键在而值收窄才是 `scope_narrowed`。`sources` 的"不在"还包括**不是字符串列表**——writer 写的是 `list(config.sources)`，别的形状既造不出也没法比较（并且 `set()` 碰到不可哈希元素会抛 `TypeError`，直接逃出 0/1/2/3 契约）。

这条分界线有源头，不是拍的：`scheduler_evidence.py:248-253` 把顶层 `operator_filters` 写成无条件的四键 dict 字面量，`scheduler_evidence.py:268` 无条件写 `sources`，`scheduler_runtime.py:1394-1402` 的 `if/else` **两条腿都写** `backfill` 且都带 `enabled`——所以字段缺失不是 pass 记录下来的一种收窄，而是 writer 造不出的形状。**易错点**：`scheduler_evidence.py:995` 的 `empty_model_discovery()` 只写两个键，看着像反例，但那是 `model_discovery.operator_filters` 这个**嵌套镜像**，本模块读顶层键，不受影响。

这个选择有可观察后果，必须说清楚：置位是**位置相关**的，一趟更新的范围完整 pass 会把它清零。所以 `[缺键, D] → 0`（更新的那趟确实看过全部，旧的不确定性被取代），而 `[D, 缺键] → 3`。另一种写法是把它做成与 `dropped` 同类的**全局**否决（一旦出现就 exit 3，不论位置）——那需要自己的分支、自己的判据和自己的覆盖行，而 D2 实测 188/188 生产 pass 两个键都在，这是纯防御路径。

**"纯防御路径"这句查过 D2b 那条压缩阶梯，不是断言**：`scope_unknown` 若是生产可达的，D2b 的 89.6% 余量就意味着它随时会到，runbook 与 EF-6 的预期退出码都得改写。实测三级阶梯都不剥 scope 键——`_compact_admissible_pass_payload`（`scheduler_evidence_payload.py:309-348`）从 `dict(payload)` 起手，只动 `skipped_candidates` / `retention` / `evidence_compaction`，两个 scope 键原样留下；`bounded_evidence_payload`（`:1093-1155`）的白名单投影确实**丢掉**两个键，但它同时把 status 改写成 `resource_limit_blocked`，在 scope 判之前就被 size-fallback 分支接走（这也是 F-04 删掉两行 `[SF, scoped]` 的同一条证据）。因此体积压力不会把生产 pass 推成 `scope_unknown`。按 KISS 取位置相关的那条，D3 表里用 `[缺键, D]` 这一行把两种读法区分开。

## D2 — 范围完整性必须读**过滤值**，不是读那个 dict 是否存在（实测）

这是本 change 唯一一处**先实测再落笔**的地方，因为写反了会让线上每一趟都判成收窄。

在 node-22 活动证据根 `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence` 上只读探查（`/scratch/frd_muziyao/NWM/.venv/bin/python`，不连任何 DB、不写任何文件）。**下面是 EF-7 / EF-7b 在本 PR 修复后的 head 上的重测值**，取代初稿那次 169 趟的快照——证据根随保留计时器滚动，趟数会变，形状不会：

```
188 个 pass（EF-7b 重测；EF-7 时 184，初稿时 169），unreadable = 0
operator_filters 取值完全一致：
  (basin_ids, model_ids, expression, excluded_runnable_count) = ((), (), None, 0)   （188/188）
  backfill.enabled  = True                        （188/188）
  sources           = ['IFS','gfs']（全集）        （188/188，零缺键）
  cycle_window      = 五个子键全在                 （188/188）
    lookback_hours = 96 · cycle_lag_hours = 16 · max_cycles_per_source = 1
  duplicate_exclusions = list(len=0)              （188/188）
  status            = "planned"                   （在 EVALUATING_PASS_STATUSES 里）
```

**文件口径**用模块自己的 `is_scheduler_pass_evidence_filename`，不是手搓的 `*.json` 过滤——第一版探针用松过滤多算进 9 个外来 JSON（`no-progress-tracker.json`、若干 `repair_stale_*` / `stale-lock-clear-*`），得出过错误的计数。

即：**生产 pass 从不收窄，但 `operator_filters` 这个 mapping 恒为非空**（四个键，取值都是空默认值）。`expression` 也**不在顶层**——它在 `operator_filters.expression`（并在 `filters.expression` 与 `model_discovery.operator_filters.expression` 各有一份镜像）。

推论，写进规格：**范围完整性按过滤值判，不按 mapping 的有无或大小判**。若按 mapping 为空判，188/188 都会被判成收窄、`hidden_after_decidable` 永不清零、`exit 0` 永不可达——线上 EF-6 的实机收据会直接给出 exit 3。拆分计划要求"实现前必须在真实 pass 文件上只读确认该键路径"，这条要求抓到的就是它。

顺带澄清两条**被这次实测否掉的担心**（记下来免得下一轮重走）：

- `limit` / `limit.pre_limit_status` 在真实 pass 上**不存在**——但模块只在 `status == resource_limit_blocked` 的 size-fallback 分支里用 `limit.get(...)` 读它，正常 pass 走不到，无碍。
- `status` 在 188 趟上全是 `"planned"`，一度看着像"没有 terminal pass"——但 `planned` 本就在 `EVALUATING_PASS_STATUSES` 里，是可判定状态。

## D2b — 生产 pass 已占读取上限的 89.6%（实测，不是本 PR 的缺陷，但要写明）

同一次只读探查顺带量了体积（EF-7 重测，184 个文件）：

```
min = 4 278 545 B   median = 4 317 401 B   max = 4 479 122 B
MAX_EVIDENCE_BYTES = 5 000 000 B
max / limit = 89.6%      超限 0 个      超过上限 90% 的 0 个
```

`_read_pass`（`operator_action_listing.py:253-254`）把大于 `MAX_EVIDENCE_BYTES` 的文件判为**不可读**，不可读即置位隐藏标志。按当前体积没有一趟触线，所以**这不是本 PR 的缺陷**，而且写侧与读侧共用同一个常量：真要超限，写侧会先把它降级成 size-fallback 产物，两边行为是一致的、不是矛盾的。

但余量只有约 10%，而它的另一头是运维语义。**这里有两处我第一版写过头、已按代码更正**：

- 越线**不必然**直接变成 size-fallback。`scheduler_evidence_payload.py` 是一道阶梯：先剥 `no_progress_circuit`，再 `_compact_admissible_pass_payload`（`:163-169`，**保住真 status**），放不下才进 bounded fallback（`:172`），再放不下才抛 `SchedulerEvidenceWriteError`（`:190-196`）。所以越线先吃掉的是压缩档的余量，不是立刻塌成 size-fallback。
- 运维**并非完全看不到成因**：receipt 的 `non_evaluating_passes` 每条都带 reason，size-fallback 那条还带 `pre_limit_status`。真正的缺口是**退出码 `3` 不区分成因**——"窗口里没有可求值的 pass"与"证据太大被降级了"给出同一个 3。

修正之后结论仍然成立、只是弱一档：余量约 10%，越过后本面会更频繁地给出 `exit 3`，而 `3` 本身不告诉运维是体积造成的。这条不在本 PR 修，已作为实测证据补进 #1905（它提议的非阻塞摘要中间档正是"缩小 payload"那条路）。

## D2c — 样本取自一台**已知停摆**的机器，以及由此测到的一条边界

诚实交代取样条件：上面两次探查都在 2026-09-16 做，而 node-22 的 `raw → forcing → runs` 自 2026-09-15 13:45 CST 起不再推进（#2432）。所以 188/188 的 `status == "planned"` 描述的是**停摆态**，不是健康态的样子。

这对 D2 的结论**不构成削弱**：`operator_filters` 的四键形状是写侧 schema 的事实，与管线是否推进无关；而 `planned` 与健康态可能出现的 `submitted` 同在 `EVALUATING_PASS_STATUSES` 里，两种取值都不改变"这些 pass 是可求值的"这一判断。

但顺带测到一条**必须写进 runbook 的边界**。最近 20 趟的 **760 条 blocked candidate 取值完全一致**：

```
error_code                           = FORCING_VERSION_ROW_ABSENT   (760/760)
retry_policy.automatic_retry_allowed = False                        (760/760)
retry_policy.manual_retry_required   = False                        (760/760)
retry_policy.attempt                 = 0                            (760/760)
```

即：在管线已经死了一天多的当下，**`list-operator-actions` 会按契约返回 `exit 0`**——因为 `FORCING_VERSION_ROW_ABSENT` 不在本面列举的决策集合里，而且它自己写着 `manual_retry_required: False`。

这**不是本面的缺陷**：它忠实反映了调度器的声明。但 `exit 0` 的含义因此必须被写死成「**没有在册的那几类待办**」，而不是「调度器健康」——否则运维会拿它当健康检查，而它在这次停摆里会给出绿灯。C1 的 runbook 段落必须明写这一条，并给出这次停摆作为实例。

另有一条**越界观察，不在本 PR 修**：这 760 个候选 `automatic_retry_allowed=False` 且 `manual_retry_required=False` 而 `attempt=0`——不是次数耗尽，是这个决策既不自动重试也不声明需要人工。它们处在"没有任何人会来管"的状态，而这个状态在任何运维面上都不会自称需要关注。这可能说明 `manual_retry_required=False` 在这个 error_code 上定错了，但那是调度决策侧的事，已作为观察提在 #2432。

## D3 — 排序表补行（r5-00 / r5-03）

round 5 判定排序表（pass 类别 × 收窄状态 → 期望退出码）缺行，且对应的变异当前**存活**：把 size-fallback 分支改成"按 `limit.pre_limit_status` 置位"不会让任何测试变红。补的行：

| # | 组合（旧 → 新） | 期望退出码 | 来源 | 对 r5-01 变异的判别力 |
|---|---|---|---|---|
| 1 | `[D, size_fallback(原 status preflight_blocked)]` | 3 | r5-00 | 不适用（r5-00 的行） |
| 2 | **`[D, SF, scoped]`** | **3** | **本轮补**（fixture 审查 F-03） | **唯一一行有** |
| 2b | **`[D, SF, source-narrowed]`** | **3** | **round 1 A2** | 同第 2 行（`sources` 维度） |
| 3 | `[unreadable, scoped]` | 3 | r5-03 | 无 |
| 4 | `[D, scoped]` | 0 | r5-03 | 无 |
| 5 | 全窄范围窗口 | 3 | r5-03 | 无 |
| 6 | 窄范围 pass 带 blocked action | 1 | r5-03 | 无 |
| 7 | `[D, lock_contended(无 backfill 键)]` | 0 | 本轮补（F-02） | 无 |
| 8 | `[D, 可求值但缺 backfill 键]` | 3 | 本轮补（缺键语义） | 无 |
| 9 | **`[可求值但缺 operator_filters 键, D]`** | **0** | **本轮补（缺键语义的判别行）** | 无（区分的是缺键读法，不是 r5-01） |

**第 2 行是整张表里唯一锁得住 r5-01 的一行，必须有。** 拆分计划原本给的六行加 r5-00 一行，**没有一行**能让「收窄 → 保持」被改成「收窄 → 清零」时变红——因为窄范围 pass 计入 `non_evaluating` 后，退出码由 `evaluating_count < 1` 决定，与隐藏标志无关（行 3、5：`evaluating_count = 0` → 3，变异前后一致；行 4：变异后标志仍是 clear → 仍 0；行 6 在 `:202` 就返回 1）。`[D, SF, scoped]` 不同：D 清零、SF 置位、scoped **保持** → 标志 armed，而 `evaluating_count = 3 - 0 - 2 = 1` 不触发那个分支，于是退出码只由标志决定；把「保持」改成「清零」即得 0，变红。这正是 D1 叙述的机制——"把一个更早的隐藏 pass 的警报抹掉"——它的钉子此前不存在。

**被删掉的两行**：拆分计划里的 `[SF, scoped(backfill disabled)]` 与 `[SF, scoped(basin filter)]` 要求一趟 size-fallback pass **同时带 scope 键**，而 `bounded_evidence_payload`（`scheduler_evidence_payload.py:1093-1155`）把 `operator_filters` 与 `backfill` 都丢掉——真实 SF pass 不可能被判成 scope-narrowed，这两行既不真实也无判别力。测试模块自己的约定也是"每一趟 size-fallback 都用**真的** `bounded_evidence_payload` 写"（`tests/test_operator_action_listing.py:3-8`、`:715-722`），照那两行写只能手搓 payload 违约。删掉，理由记在这里而不是默默少写。

**第 8、9 行成对存在，缺一不可。** 第 8 行只证明缺键会置位，它在「缺键 = 全局否决」的读法下同样是 3，因此单独存在时对两种读法零判别力；第 9 行把缺键 pass 放在**更旧**的位置，位置相关读法给 `0`、全局否决读法给 `3`，是整张表里唯一区分这两种实现的一行。D1 已裁决取位置相关，所以第 9 行的期望值是 `0`；实现若写成全局否决，第 9 行必红。

`[D, scoped] → 0` 是其中唯一的 `0`，也是最容易写反的一行：**更早**的可判定范围完整 pass 已经清零了标志，其后一趟窄范围 pass 按 D1 的三态「保持」，因此标志仍是零，`exit 0` 成立。

**第 2b 行与第 2 行判别力相同，但钉的是另一个维度**：删掉 `_scope_reason` 里的 `sources` 收窄分支后，那趟 pass 变成范围完整可求值 → 清零标志 → 得 0，第 2b 行必红；反过来第 2 行对 `sources` 分支零判别力。两行都要。

### 这几行对着现有机制逐行验过（实测，非断言）

迭代方向先定死：`_newest_pass_files`（`:245`）以 `reverse=True` 排序，**新在前**；判定循环 `for name, path in reversed(selected)`（`:160`）因此是**从旧到新**走。`hidden_after_decidable` 的终值由**最新**那趟决定——`[D, scoped] → 0` 的推理方向成立，不是反的。

退出码由 `:201-206` 三行决定：`evaluating_count = len(selected) - len(unreadable) - len(non_evaluating)`；`listed` 非空 → `1`（先返回）；否则 `dropped or evaluating_count < 1 or hidden_after_decidable` → `3`；否则 `0`。把窄范围 pass 计入 `non_evaluating` 之后：

- `[D, scoped]`：2 趟、1 趟非求值 → `evaluating_count = 1 ≥ 1`，标志被 D 清零、scoped 保持 → **0** ✓
- 全窄范围窗口：`evaluating_count = 0` → **3** ✓
- 窄范围 pass 带 blocked action：`listed` 非空，在 `:202` 就返回 → **1** ✓

即三态改动**不需要动退出码逻辑**，落点只有两处：`_non_evaluating_entry`（`:209`）要能识别范围收窄并给出 reason `scope_narrowed`（缺键则 `scope_unknown`），以及 `:174-176` 那个「非透明即置位」的分支要把 `scope_narrowed` 一并豁免。**`scope_unknown` 不在豁免之列**——它走的就是那个分支的默认动作（置位），这正是 D1 把它归到第四态的意思。现有机器件足够承载这条规则，这是本 change 体量小的原因。

`_non_evaluating_entry` 现签名是 `(name, status, limit)`，判不了 scope——它必须改成接收整个 payload（或额外接收 `backfill` / `operator_filters` 两段）。这条写在这里，免得实现时当成"顺手改的签名"而漏掉调用点。另一条必须落死的顺序：**status 判在 scope 判之前**（`spec.md:54`），写反即 F-02 那个坑——透明 pass 结构性无 `backfill` 键，会被当成 `scope_unknown` 置位，第 7 行必红。

## D4 — round 1 交叉审查落下的三条判定（除 `sources` 外）

- **决策集合闭合（A1/E2）**：`OPERATOR_ACTION_DECISIONS` 原本只有四条，漏了
  `blocked_operator_reentry_restart_stage_refused`（写侧 `scheduler_candidates.py` 的
  `OPERATOR_REENTRY_SINK_REFUSAL_DECISION`），而同一份 runbook 第二步一直在处置它——
  一趟只含该 decision 的完整 pass 会返回 `exit 0`（"没有待办"）。真正的交付物不是补那条字面量，
  而是**闭合 pin**：测试用 `ast` 只读扫 `scheduler_candidates.py` 与 `scheduler_state_failure.py`，
  取所有带 `"manual_retry_required": True`（含嵌套 `retry_policy`）的 dict 字面量自己的 `decision`
  值（字符串字面量或模块级常量 Name 都解析），断言集合与 `OPERATOR_ACTION_DECISIONS` 相等、且
  `unresolved == []`。**只读源码不 import**：列举面要保持 DB-free，且不给 CI 选择器带进新的
  importer 对。
- **跨 pass 合并取最新趟（C1）**：原实现首次出现即定型，只更新 `last_seen_pass` /
  `seen_in_passes`，于是 `recorded_init_state_id` 等值字段来自**最旧**趟而 `last_seen_pass` 指向最新趟，
  receipt 自相矛盾。危害具体：runbook 让运维把 receipt 的 `recorded_init_state_id` 喂给
  `confirm-operator-reentry`，陈旧 token 在 dry-run 分支**之前**就被拒（不会写错确认物，但恢复被阻断
  且看不出是 pin 不对还是 token 过期）。本 diff 删掉的旧 jq 是 `ls -t | head -1`（只读最新一份，token
  天然新鲜），换成 `--passes 6` 的合并 receipt 才出现这个问题。**判定：最新趟全胜**——除
  `first_seen_pass` / `seen_in_passes` 外所有字段取 `last_seen_pass` 那趟，`candidate_id` 为 None
  时（breaker 释放腿没有）回退到已知值。顺带更正一条措辞：`occurrences` **不是**逐趟递增的，写侧
  （`scheduler_candidates.py:2600-2606`）写的是当趟实测 live 计数，稳态 fail-stop 下是平的；真正的
  漂移风险集中在 `recorded_init_state_id`。
- **单文件在扫描期消失不得中止整次扫描（B1）**：`_newest_pass_files` 原本把 per-entry 的
  `is_file` / `stat` 放在根级 `try` 里，任一 `OSError` 变成 `evidence root unreadable` → exit 2。
  可达性是真的：同一 evidence root 上 `nhms-scheduler-evidence-retention.timer` 在删这批文件。危害是
  exit 2 的消息指向 **root**，而 runbook 把 `2` 解释成"多半是 root 取错"，运维被导向一个不存在的配置
  问题，且全部可读 pass 被整体丢弃。**判定**：per-entry 各自 `try`，根级只保留 `os.scandir(root)`；
  消失的文件计入 `unreadable_passes`（spec 要求"SHALL be reported"），并在退出码里作**全局否决**
  （与 `dropped` 同档）——它的 mtime 没读到，**无法定位在时间序里**，位置未知时不能像主循环里
  `_read_pass` 返回 None 那样按位置置位。由此产生一处**必须写明的不对称**：`unreadable_passes` 这个
  receipt 列表与 `evaluating_count` 的减项**不是同一个集合**，因为扫描期消失的文件从来没进过
  `selected`，减它会算错。

## D5 — 收窄维度的**处置表**（round 2 的 same-invariant 门产出）

round 2 撞了 review 流程的 same-invariant 门。复发的不变量是：**本面每一个「闭合集合」都必须从它的权威处反算，而不是手工誊写一份副本再寄望它不漂移。** 同一条规则在两轮里失败了五次——A1（决策集合手抄四条漏第五条）、A2（收窄维度手工列举漏 `sources`）、R2-02（仍是手工列举，漏时间窗）、R2-05（A1 那条 pin 自己的 writer 文件清单也是手抄的）、R2-06（三方一致性 pin 的耦合边对 CI 选择器不可见，因为选择器目标清单同样手工列）。

所以 round 2 的纠正动作不是「再补一条 `if`」，而是把判据从**列举**改成**处置**：writer 发布的每一个键都必须拿到一个归类，并由一条从 writer 侧反算的测试保证漏一个就变红。

### D5.0 — 闭合权威的准确表述（**不是**「42 个键」）

> **闭合权威 = 一趟处于「求值态」的 pass 所发布的顶层键集合**，由**真跑一次 writer** 取得，不由阅读 writer 源码列举。

两条支撑，缺一不可：

1. **为什么只算求值态**：`_scope_reason` 只在求值态 pass 上运行；非求值态由四态 status 逻辑各自处置（`lock_contended` / `preflight_blocked` → TRANSPARENT 保持标志，其余 → arm）。而每一条早退分支都**重写了 `status`**（例：progress_guard 跳闸 → `scheduler_runtime.py:1473` 字面量 `"resource_limit_blocked"`）。所以早退分支少写的键**永远到不了范围判据面前**，不欠一行。
2. **为什么必须真跑而不是读源码**：本表的第一版正是**阅读两个 writer 文件**（`scheduler_evidence.py` 的 `base_evidence` + `scheduler_runtime.py` 的 `backfill` 块）得出的，覆盖 14 个顶层键。实测证明写侧是 **42 个**——通过阅读源码列举权威，只是换个地方手抄。**这条不变量因此在纠正动作自身上复发了一次**，记录见 `.workplans/pr-2440/deviations.md` DEV-2。

**实测背书**（node-22 生产证据根，head `c76a69d0`，194 趟 live pass）：全部 `status='planned'`、`execution_boundary='planning_only'`、**42 个顶层键，无一参差**（「本状态下并非趟趟都有的键：无」）。源码上写作条件写入的 `retention` / `no_progress_circuit` / `restart_reconcile` / `restart_reconcile_proof`，在生产求值态 pass 上同样趟趟都在。

**闭合深度 = 处置深度**：本表在**子键级**处置的键（`backfill` / `cycle_window` / `operator_filters` / `counts` / `runtime_config` / `model_discovery`），测试就从真实 payload 下钻枚举其子键并施加同一套双向断言；整体判 (iii) 的键到此为止。否则往 `cycle_window` 里新加一个旋钮不会变红——同一条不变量下沉一层原样复发。

**双向断言**：(a) 产出的每个键都有处置（新写者加键 ⇒ 红）；(b) 处置表每个键都出现在产出里（陈旧表项 ⇒ 红）。

**I-4 —— fixture 的可达面也是一份副本（round 3 补，C1 的教训）**：
「跑 writer」本身**不等于**闭合。它只是把枚举从「手抄一份清单」搬成「手抄一条路径」——
**fixture 走到哪儿，权威就只到哪儿**。本表第一版的闭合测试是绿的，而
`slurm_preflight`（`scheduler_runtime.py:1389-1390`，求值态主路径上与已处置的
`submit_overlap_receipt`/`evidence_pre_execution` 并列）**一行处置都没有**——
因为两条 fixture 腿都没打开 `slurm_execution_enabled`，双向断言的两边同时为空。
更糟的是 `stale == []` 那一半**封死了显而易见的补救**：只往表里加一行会立刻红成「已处置但从未发布」。

**所以闭合纪律有第二层**：凡是会改变「求值态 pass 发布哪些键」的配置开关，
fixture 必须**逐个打开**，权威取各条腿产出的**并集**。今天已知的开关是
`slurm_execution_enabled`（`scheduler_config/config.py:130-133`，环境变量
`NHMS_PRODUCTION_SLURM_ENABLED`）；仓内**已有**能走通该路的 db-free double——
`tests/test_production_scheduler.py:24246-24275` 的 `FakeProductionOrchestrator`
断言了 `result.status == "submitted"` 且 `evidence["slurm_preflight"]["status"] == "ready"`，
而 `submitted ∈ EVALUATING_PASS_STATUSES`。
**因此不需要 conditional 标注行，也不需要回退到 AST**：这条腿是开得起来的，开它就是了。
（规格里原有的「造不出来的键单独标注 conditional」条款据此删除——它既与
`assert stale == []` 结构性互斥，也会把一条本可打开的 fixture 腿用文档掩盖过去。）

### D5.1 — 顶层键处置表（生产 planning-only 面：42/42）

> **本表的覆盖面不等于闭合测试的权威面。** 这 42 个键是 node-22 生产证据根上
> `execution_boundary='planning_only'` 的求值态 pass 实际发布的全集。
> 打开 `slurm_execution_enabled` 的那条腿会**多发布**若干键（今天已知的是
> `slurm_preflight`，与 `submit_overlap_receipt`、`evidence_pre_execution` 同在
> `scheduler_runtime.py:1380-1442` 的主求值路径上）。
> **这些键不在本表里逐个列举，是故意的**——按 I-4，权威是各条 fixture 腿产出的**并集**，
> 由测试在运行时取得；本表只承担「逐键给出处置理由」的说明职责。
> 若有人想知道「到底有几个键」，正确答案永远是「跑一次闭合测试」，不是读这一行标题。

| 键 | 处置 | 理由 |
|---|---|---|
| `sources` | **(i) 判**（本轮改判定形状） | 改**覆盖**判定（`not COMPLETE <= set(sources)`）而非集合相等：超集确实看过每个生产源。拼写无需归一化——`ProductionSchedulerConfig.__post_init__`（`config.py:448`）已把每个 `--source` 过 `normalize_source_id`（大写查闭表、未知值抛错），大小写变体落不了盘；读侧 casefold 反而会给 db-free adapter 那条大小写敏感的清单路径开一扇**假 exit 0** 的门 |
| `cycle_window` | **下钻**，见 D5.2 | |
| `operator_filters` | **下钻**，见 D5.2 | |
| `backfill` | **下钻**，见 D5.2 | |
| `counts` | **下钻**（本轮新增读取），见 D5.2 | 承载 `selected_model_count`，即「这趟到底求值了几个模型」 |
| `runtime_config` | **下钻**（本轮新增读取），见 D5.3 | 由 `base_evidence`（`scheduler_evidence.py:293-297`）**无条件写、此后从不被覆盖**，是运行时配置的唯一顶层权威；`scheduler_evidence.py:860` 的嵌套镜像不读 |
| `model_discovery` | **下钻**，见 D5.4 | |
| `duplicate_exclusions` | (iii) | 语义是「**重复的** source 折叠掉」（唯一写者 `scheduler_runtime_roots.py:539-548`，`reason: "duplicate_source"`），`--source gfs --source gfs` 使其非空而覆盖面一点没少。判「非空即收窄」是**假 exit 3** 入口。**编排者初测把它列为收窄维度，错了** |
| `filters` | (iii) | `operator_filters` 的第二份拷贝（`scheduler_evidence.py:278` 建、`scheduler_runtime.py:1321` 重赋）。判两遍只会在将来分叉时产生互相矛盾的裁决 |
| `progress_guard` | (iii) | 它确是真断路器（`scheduler_runtime.py:57-63` 连续无进展达上界即 raise），但**跳闸必改顶层 `status`**：异常唯一落点 `:1468-1502` 在 `:1473` 字面量写死 `"resource_limit_blocked"`；未跳闸时 `:1374` 又把 `status="passed"` 硬编码传入。**不存在「跳闸却仍为求值态」的第三态**，故由 status 处置覆盖 |
| `no_progress_circuit` | (iii) | 模块自述 observe-only（`scheduler_no_progress.py:1-6`「never feeds a scheduling decision」）；`observe_pass` 在 `:1434` 调用，**晚于** `:1328-1379` 把候选列表写进 evidence，读的是成品，结构上不可能删掉某一行。开路条目的 `reason`/`decision` 原样留在 `blocked_candidates` 里。`truncated` 截的是本趟证据里 `open` 数组的展示条数（cap 50），不是跟踪状态本身 |
| `status` | (iii) | 由四态 status 逻辑处置，不是范围判据的输入 |
| `candidates`、`blocked_candidates`、`skipped_candidates`、`model_run_evidence`、`source_cycles`、`slurm_cancellation_evidence`、`timing` | (iii) | **记录这趟扫出了什么，不声明这趟被允许扫什么**。`source_cycles` 尤其容易误判：它是 `allowed_cycle_hours_utc` + `lookback_hours` + `cycle_lag_hours` + `max_cycles_per_source` 共同作用后的**结果**清单，判它等于判各旋钮的影子 |
| `no_mutation_proof`、`execution_write_proof`、`restart_reconcile_proof`、`slurm_cancellation_proof`、`slurm_status_sync_proof`、`restart_reconcile`、`execution_boundary` | (iii) | 事后证明与阶段标签：证明的是「本趟没有副作用 / 在哪一步封顶」，与扫了多少东西无因果。`execution_boundary` 由字面量或 `scheduler_evidence_proofs.py:22-27` 派生 |
| `schema_version`、`review_contract`、`production_contract`、`pass_id`、`started_at`、`finished_at`、`artifact_path` | (iii) | 元数据、时间锚与落盘路径。本面的时序判定用文件 mtime，不用这些 |
| `execution_mode`、`readiness_interpretation`、`dry_run` | (iii) | 三者同源于 `config.dry_run`（`scheduler_evidence.py:244/247/267`）。`dry_run` 只 gate 提交/取消/保留（`scheduler_runtime.py:954,1013,1123,2081`），窗口与候选构造不变。「dry-run pass 该不该背书 exit 0」是**另一条 requirement，已路由出去** |
| `readiness` | (iii) | 「这份证据能否被当作最终生产就绪证明」的元判断标签，与本趟扫描量无关 |
| `lock`、`root_preflight`、`resolved_runtime_roots` | (iii) | 互斥锁状态、根目录可用性、根目录身份。三者都是**全有全无式二元阻断**（阻断即整趟归零并改 `status`），不存在「部分放行」的分级收窄 |
| `retention` | (iii) | 清理多老的历史产物，输入维度是**文件年龄**，与调度窗口正交 |
| `journal_read_attribution` | (iii) | 本趟 journal I/O 计数快照（每趟入口 `reset_journal_read_counters()` 清零），伴生观测指标 |

### D5.2 — 已有四组的子键处置

| 子键 | 处置 | 理由 |
|---|---|---|
| `backfill.enabled` | (i) 已有 | 见 D1 / D2 |
| `backfill.lookback_hours` | (iii) | 与 `cycle_window.lookback_hours` 同源 `config.lookback_hours`，实测两者恒等；且它**只存在于 `enabled=True` 那条腿**（`scheduler_runtime.py:1401` 的 else 腿不带），升为必需字段会让存在性检查与 `backfill.enabled` 判定产生顺序耦合 |
| `backfill.audit` | (iii) | 事后审计明细；size-fallback 会清空它，判它会与 `scheduler_evidence.py:303-309` 的 size-fallback 路径打架 |
| `cycle_window.lookback_hours` | **(i) 本轮新增** | 只判**退化值** `<= 0`：零宽发现窗使 backfill 腿对窗口边界以外的所有更旧 cycle 全盲，而断路器释放按构造坐在最旧那侧（`scheduler_discovery.py:824-832`）。可达输入精确是 `0`（`cli.py:431-435` 不校验下界，`config.py:458` 只把负值夹成 0）。读顶层 `cycle_window` 而非 `backfill.lookback_hours`，理由同上 |
| `cycle_window.cycle_lag_hours` | **(ii) 成文边界** | 平移窗口而非归零，负值不可达（`config.py:462` 的 `max(...,0)`），无客观退化点。与 `lookback > 0` 的一般情形**合并成一条**：`exit 0` 只对该 pass 自己的窗口 `[start_time_utc, end_time_utc]` 作答；生产 96h/16h 意味着最近 16 小时的 cycle 不在任何窗口内。仓内不存在可比的「完整窗口」权威（生产值与代码默认 24 不一致），所以不发明阈值，只把边界写明 |
| `cycle_window.max_cycles_per_source` | (iii) | `< 1` 在 `config.py:464-465` 直接抛错；且对任何 `backfill.enabled=True` 的 pass **完全 inert**——截断在 `_select_legacy_source_cycles`（`:904-906`）里，只在不走 backfill 腿时执行，而 legacy 腿的前提 `backfill_enabled=False` 已被判 narrowed。**编排者一度据「生产恒为 1」推断该截断始终生效，被裁决席证伪**：生产恒为 1 不是因为截断在起作用，而是因为它对生产 pass 无关紧要 |
| `cycle_window.start_time_utc` / `end_time_utc` | (iii) | `started_at - lag - lookback` 的派生值（`scheduler_evidence.py:244-245`），判源头即可 |
| `operator_filters.model_ids` / `basin_ids` / `expression` | (i) 已有 | 见 D1 / D2 |
| `operator_filters.excluded_runnable_count` | (iii) | 顶层这份恒为字面量 `0`（真实计数只进 `model_discovery`）。**且即便读真实那份也无新信息**：`scheduler_models.py:303-306` 的 `_matches_filters` 在 `model_ids=()` 且 `basin_ids=()` 时双重短路恒返回 `True`，故 `excluded_runnable_count > 0` 与「三个过滤全空」**代数互斥**——它纯由已判的三个输入派生，不存在第三条排除路径 |
| `counts.selected_model_count` | **(i) 本轮新增** | 「这趟求值了几个模型」。`== 0` 时这趟**什么都没观测到**，不是运维指令收窄，落 arm 分支而非 `scope_narrowed`（详见 D6）|
| `counts` 其余 12 个子键（`candidate_count`、`blocked_candidate_count`、`skipped_candidate_count`、`source_cycle_count`、`submitted_count`、`failed_count`、`partial_count`、`slurm_status_sync_count`、`slurm_status_sync_unknown_count`、`slurm_cancelled_count`、`slurm_cancellation_blocked_count`、`slurm_cancellation_unknown_count`） | (iii) | 均为**产出计数**而非覆盖面声明。**记一条同名陷阱**：顶层 `counts.candidate_count` 是三个列表长度之和（`scheduler_runtime.py:1327`），而 `progress_guard.checkpoints[].details.candidate_count`（`:942`/`:1112`）只是 `len(candidates)`——同名不同义，混用会算错 |

### D5.3 — `runtime_config` 的 26 个子键

| 子键 | 处置 | 理由 |
|---|---|---|
| `allowed_cycle_hours_utc` | **(i) 本轮新增** | **`runtime_config` 里唯一别处没有第二份声明的收窄旋钮**。`discover_cycles` 用 `_filter_allowed_cycle_hours` 把不在允许小时集合里的 cycle 直接过滤掉，默认 `(0, 12)` 意味着每天 24 个潜在 cycle 时刻只有 2 个进入候选评估。判**对代码侧默认值的覆盖**（`scheduler.py:293` 的 `DEFAULT_ALLOWED_CYCLE_HOURS_UTC`，**import 而非敲字面量**——这是本轮第六个闭合集合）。完整性权威取默认值而非 0-23 全域：按全域判会把每趟生产 pass 打成 narrowed ⇒ 假 exit 3 |
| `sources`、`lookback_hours`、`cycle_lag_hours`、`max_cycles_per_source`、`model_ids`、`basin_ids`、`dry_run` | (iii) | 顶层同名键/块的第二份拷贝（`scheduler_evidence.py:856-865`）。判权威那一份即可；判两遍只会在将来分叉时产生互相矛盾的裁决 |
| `continuous` | (iii) | 纯多趟循环控制，代码在 `run_once()` **外层**（`cli.py:400-414`）；`config.py:492` 只用它做与 `repair_missing_forcing` 的互斥校验。`scheduler_discovery.py` / `scheduler_candidates.py` / `scheduler_models.py` **无任何读取点** |
| `missing_forcing_repair.enabled` | (iii) | 两处消费点都在候选**已被发现且已被状态机判过之后**（`scheduler_candidates.py:1890-1908` 自述 "evaluated **after** the normal candidate-state decision... can only **reclassify**"；`:433` 是分类循环内的 `continue` 分支），不产生也不删除候选。该模式确实强制 `lookback_hours=0` 且 `max_cycles_per_source=1`（`config.py:496-499` 构造期校验），但这个收窄**完全由那两个字段本身承载**，不是这个布尔独立产生的 |
| `missing_forcing_repair.exact_cycle_time` | (iii) | 派生；`--cycle-time` 同时把 `lookback` 置 0 **且** `disable_backfill=True`，两个后果都已被判——且 `backfill.enabled is not True` **先命中**，`lookback <= 0` 对这类 pass 根本不会被求值。因此 `lookback <= 0` 这条判据的**可达入口只有**「`--lookback-hours 0` 且不带 `--cycle-time`」（即 R2-02 那条） |
| `missing_forcing_repair.plan_only` | (iii) | `config.dry_run` 的第六份派生 |
| `missing_forcing_repair.default_policy` | (iii) | `scheduler_evidence.py:877` 是**写死的字符串常量** `"fail_closed"`，不读任何 config 属性——连派生值都不是 |
| `require_direct_grid` | **(iii)，但理由必须写全** | 两条机制效果不同。候选层（`scheduler_candidates.py:1971`）只决定已发现的候选能否走精确重试路径，候选行照写、只换 `reason`，**不收窄**。注册表层（`scheduler_file_providers.py:908-919`）则是：manifest 里**只要有一个**模型不合直连网格契约就 `raise`，被 `:213-221` 接住后整个 `_models` 清空、`registry.status="blocked"`，进而触发 `scheduler_runtime.py:892-914` 的 `db_free_registry_blocked` **早退分支**（改写 `status`，非求值态）。即这条机制的收窄效果由 `registry.status` 与早退 status 承载，**这个字面量本身只是开关声明** |
| `require_runtime_roots`、`service_role`、`database_url_configured`、`scheduler_db_free_required`、六个 `scheduler_*_backend` | (iii) | db-free 模式的后端选择与契约声明。后端不可用时走 `root_preflight` / `registry` 的二元阻断并改 `status`，不产生分级收窄 |
| `db_free_runtime` 整块 | (iii) | `config.py:677-707` 的 `db_free_runtime_evidence()`：各后端是否切到 `file`、必需 env/路径是否配置的**契约自检快照**。八个子键无一出现在 `scheduler_discovery.py` / `scheduler_candidates.py` / `scheduler_models.py` 的筛选逻辑里 |
| `interval_seconds`、`retry_limit`、`concurrent_submit_bound`、`slurm_array_concurrency_bound` | (iii) | 吞吐与重试参数，决定「多快 / 并发多少 / 重试几次」，不决定「扫了哪些东西」 |

### D5.4 — `model_discovery` 的 8 个子键，以及第七扇门

| 子键 | 处置 | 理由 |
|---|---|---|
| `selected_model_count` | (iii) | `counts.selected_model_count` 的第二份拷贝（`registry.selected_model_count` 是第三份）。判权威那一份，见 D5.2 |
| `operator_filters.excluded_runnable_count` | (iii) | 见 D5.2：与「三个过滤全空」代数互斥 |
| `operator_filters.expression` | (iii) | `scheduler_models.py:309-315` 的 `filter_expression([], [])` 在两列表皆空时返回 `None`，与顶层 `operator_filters.expression` 同源同构 |
| `excluded_model_count` / `exclusions` | (iii) | 排除原因是 inactive / not_runnable / not_shud_model / 元数据不全 / 重复身份——这些模型**本来就跑不了，没有可列的待办**，不是「本可以扫却被过滤掉」。且 `discover_models`（`scheduler_models.py:65-121`）两段循环**穷举无旁路**：`active_model_count = selected_model_count + excluded_model_count` 是该函数内部的不变式 |
| `models` | (iii) | 选中模型明细，产出记录 |
| `active_model_count` / `runnable_model_count` | (iii) | 计数，其为零的退化情形已由 `counts.selected_model_count == 0` 判据覆盖（D6）|
| `registry` 整块 | **(ii) 成文边界**，见下 | |

**第七扇门（勘察 Q5 发现，实测后判 (ii)）**：`registry.model_count` **不是** `active_model_count` 的同层拷贝，而是**更上游的更大数**——它取自 `_validate_registry_manifest` 的 `len(rows)`（`scheduler_file_providers.py:936`），是 manifest 里登记的模型**总数，含 inactive**；而 `active_model_count` 的 `rows` 来自 `list_models(active=True, ...)`，该过滤（`scheduler_file_providers.py:152-157`，按 `active_flag` 与 `lifecycle_state`）发生在 `discover_models` **拿到行之前**。因此 `registry.model_count − active_model_count` 这段落差，`exclusions` 数组**结构上记录不到**。（这与 `coerce_registered_model` 的 `inactive_model` 排除原因不是同一处——那是 list 与 get 之间状态翻转的 TOCTOU 二次复查，只覆盖已进入 `rows` 的模型。）

**为什么判 (ii) 而不是 (i)**：判 `active_model_count == registry.model_count` 为必要条件，等于规定「manifest 里不许留退役模型」——manifest 里退役一个模型就会把生产**永久**翻成 exit 3。仓内不存在「manifest 应含多少模型」的权威，这正是本 change 对 `cycle_window` 已经拒绝过的**发明阈值**。成文边界的内容是：`exit 0` 对 manifest 标为 inactive 的模型不作任何断言，且该差额在证据里**只能**通过 `registry.model_count` 与 `active_model_count` 的差值看见，`exclusions` 反查不到。

**实测**（node-22，195 趟 live pass）：`registry.model_count = active = runnable = selected = 76`、`excluded_model_count = 0`、`exclusions = []`，**195/195 落差为零**。即这条边界今天在生产上是空的，但它结构上存在。

### D6 — 零模型 pass：`counts.selected_model_count` 判 arm 而非 narrowed

一趟模型注册表解析为空的 pass 会正常发现 cycle、写证据、以求值态终止，**却什么都没求值**。按 D5 其余各行它会是「运维过滤全空 + 源集完整 + 窗口为正 + 默认 cycle 小时」⇒ 判为范围完整 ⇒ **清掉隐藏标志**、背书 exit 0。这就是本 PR 的假 0。

- **判据**：`counts.selected_model_count == 0` ⇒ 不判范围完整，用**一条与 `scope_narrowed`/`scope_unknown` 都不同**的 reason 报出，并 **arm** 隐藏标志。
- **为什么是 arm 不是 narrowed**：沿用本 change 已定的 presence/value 纪律——字段在且**值在收窄** = 运维自己的指令，保持标志；一趟**什么观测都没有**，正是 arming 存在的理由。
- **为什么判进本轮**（范围扩张，记在 `.workplans/pr-2440/deviations.md` DEV-3）：与 round-1 A1 同一把尺子——本 PR 赋予 `exit 0` 运维语义，同一把尺子不能对 A1 用、对这条不用。
- **写侧留在 #2443**：evidence 的 `backfill.enabled` 记的是 `config.backfill_enabled` 的**意图**，而实际走哪条腿由 `scheduler_discovery.py:700` 的 `backfill_mode = bool(config.backfill_enabled and models)` 决定，零模型时两者不一致。那半边不在本轮。
- **可达性**（issue-scribe 本地复现 + 勘察确认）：db-free 清单 `models: []` 合法（`scheduler_file_providers.py:883-940` 只校验 schema/freshness/checksum/上限，**无下限**，返回 `status:"ready"`）；清单非空但被 `scheduler_models.py:137-145` 全部排除同样落到零模型而 registry 仍 ready。只有 `registry.status=="blocked"` 才走早退（那条改写 `status`，安全），零模型**不触发**。
- **实测安全**（EF-7c/7d/7e/7f）：生产 195/195 趟 `selected_model_count = 76`，新判据不翻动生产退出码。

**真正的限流器不在这张表里，必须单独成文**：backfill 腿每源每趟只求值**最旧的一个**未完成 cycle，更新的 gap 记为 `backfill_deferred_waiting_for_prior_cycle`。减轻因素要一并写，否则会被读得比实际悲观——gap 是老→新排序，被推迟的是**更新的** cycle；未解决的待办会让它自己那个 cycle 保持为 gap、继续占槽，于是每趟都重新求值、重新列出，而更新 cycle 上的待办在旧 cycle 清空前根本无法被创建。且 `blocked_journal_predecessor_identity_quarantine` **不受此限**：断路器释放在单槽切分**之前**完成，释放的是「从最旧起连续的全部」，所以这条决策的可见性是完整的。

## 已测 / 待测（未测的地方写"未测"，不写推断）

**已测（EF-6 / EF-7 / EF-7b，全部在本 PR 修复后的 head 上取）**：

- 端到端退出码**已在 node-22 实机取到**（EF-6）：`--passes 6` 与 `--passes 200`（整个证据根）都返回 `exit 0`，`candidate_lists_dropped_passes` / `non_evaluating_passes` / `unreadable_passes` 三个列表全空。这是本 change 最要紧的一条收据——round 1 给列举面加了第五条决策与 `sources` 判据，round 2 又加了时间窗判据，三者都可能把线上 pass 判成"范围不完整"，实测**没有**。
- `sources` 的取值分布已并入 D2 正式重测：**188/188 趟为全集、零缺键**（round 1 A2 裁决时的单独实测是 177/177，两个数字各自带日期，证据根随保留计时器滚动）。
- `cycle_window` 五个子键 **188/188 全在**（EF-7b），所以 round 2 新加的时间窗判据同样不会把线上打成 `scope_unknown` 或 `scope_narrowed`。

**仍未测**：

- 该样本里**没有**任何 size-fallback、不可读或收窄的 pass，因此 D3 各行全部只能靠构造的 fixture 立论，线上无对照样本。D2b 实测 **184/184** 趟处在字节上限的 85.6%–89.6%，即 size-fallback 分支离线上只差一成多，但今天确实 0 趟触发。（这个 184 是 D2b 自己那次取样的趟数，与上面 `sources` / `cycle_window` 的 188 不是同一次测量——证据根随保留计时器滚动，本 change 前后六次测量得到六个总数：184/188/191/192/194/195。绝对趟数一律只在它自己的测量语境里成立。）
- `blocked_operator_reentry_restart_stage_refused`（round 1 A1 补的第五条决策）线上发生率为 **0 行 / 184 趟**（EF-7）。它是**潜伏**缺陷而非正在发生的故障；这不削弱修它的理由（runbook 第二步为它立了专门处置行，一旦发生旧实现的 `exit 0` 就是在说谎），但收据要诚实：EF-6 的 `exit 0` 因此是**真**的 0，不是漏判出来的 0。
- `scope_unknown`、零宽时间窗（`--lookback-hours 0`）两条路径线上均未出现，只有构造 fixture 的覆盖。
