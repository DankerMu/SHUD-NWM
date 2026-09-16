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

这个选择有可观察后果，必须说清楚：置位是**位置相关**的，一趟更新的范围完整 pass 会把它清零。所以 `[缺键, D] → 0`（更新的那趟确实看过全部，旧的不确定性被取代），而 `[D, 缺键] → 3`。另一种写法是把它做成与 `dropped` 同类的**全局**否决（一旦出现就 exit 3，不论位置）——那需要自己的分支、自己的判据和自己的覆盖行，而 D2 实测 169/169 生产 pass 两个键都在，这是纯防御路径。

**"纯防御路径"这句查过 D2b 那条压缩阶梯，不是断言**：`scope_unknown` 若是生产可达的，D2b 的 89.6% 余量就意味着它随时会到，runbook 与 EF-6 的预期退出码都得改写。实测三级阶梯都不剥 scope 键——`_compact_admissible_pass_payload`（`scheduler_evidence_payload.py:309-348`）从 `dict(payload)` 起手，只动 `skipped_candidates` / `retention` / `evidence_compaction`，两个 scope 键原样留下；`bounded_evidence_payload`（`:1093-1155`）的白名单投影确实**丢掉**两个键，但它同时把 status 改写成 `resource_limit_blocked`，在 scope 判之前就被 size-fallback 分支接走（这也是 F-04 删掉两行 `[SF, scoped]` 的同一条证据）。因此体积压力不会把生产 pass 推成 `scope_unknown`。按 KISS 取位置相关的那条，D3 表里用 `[缺键, D]` 这一行把两种读法区分开。

## D2 — 范围完整性必须读**过滤值**，不是读那个 dict 是否存在（实测）

这是本 change 唯一一处**先实测再落笔**的地方，因为写反了会让线上每一趟都判成收窄。

2026-09-16 在 node-22 活动证据根 `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence` 上只读探查（`/scratch/frd_muziyao/NWM/.venv/bin/python`，不连任何 DB、不写任何文件），**169 趟全部保留的 pass**：

```
169 个 pass，operator_filters 取值完全一致：
  (basin_ids, model_ids, expression, excluded_runnable_count) = ((), (), None, 0)
  backfill.enabled = True （169/169）
  status            = "planned" （169/169，在 EVALUATING_PASS_STATUSES 里）
```

即：**生产 pass 从不收窄，但 `operator_filters` 这个 mapping 恒为非空**（四个键，取值都是空默认值）。`expression` 也**不在顶层**——它在 `operator_filters.expression`（并在 `filters.expression` 与 `model_discovery.operator_filters.expression` 各有一份镜像）。

推论，写进规格：**范围完整性按过滤值判，不按 mapping 的有无或大小判**。若按 mapping 为空判，169/169 都会被判成收窄、`hidden_after_decidable` 永不清零、`exit 0` 永不可达——线上 EF-8 会直接 exit 3。拆分计划要求"实现前必须在真实 pass 文件上只读确认该键路径"，这条要求抓到的就是它。

顺带澄清两条**被这次实测否掉的担心**（记下来免得下一轮重走）：

- `limit` / `limit.pre_limit_status` 在真实 pass 上**不存在**——但模块只在 `status == resource_limit_blocked` 的 size-fallback 分支里用 `limit.get(...)` 读它，正常 pass 走不到，无碍。
- `status` 在 169 趟上全是 `"planned"`，一度看着像"没有 terminal pass"——但 `planned` 本就在 `EVALUATING_PASS_STATUSES` 里，是可判定状态。

## D2b — 生产 pass 已占读取上限的 89.6%（实测，不是本 PR 的缺陷，但要写明）

同一次只读探查顺带量了体积（170 个文件）：

```
min = 4 278 545 B   median = 4 317 412 B   max = 4 479 122 B
MAX_EVIDENCE_BYTES = 5 000 000 B
max / limit = 89.6%      超限 0 个      超过上限 90% 的 0 个
```

`_read_pass`（`operator_action_listing.py:253-254`）把大于 `MAX_EVIDENCE_BYTES` 的文件判为**不可读**，不可读即置位隐藏标志。按当前体积没有一趟触线，所以**这不是本 PR 的缺陷**，而且写侧与读侧共用同一个常量：真要超限，写侧会先把它降级成 size-fallback 产物，两边行为是一致的、不是矛盾的。

但余量只有约 10%，而它的另一头是运维语义。**这里有两处我第一版写过头、已按代码更正**：

- 越线**不必然**直接变成 size-fallback。`scheduler_evidence_payload.py` 是一道阶梯：先剥 `no_progress_circuit`，再 `_compact_admissible_pass_payload`（`:163-169`，**保住真 status**），放不下才进 bounded fallback（`:172`），再放不下才抛 `SchedulerEvidenceWriteError`（`:190-196`）。所以越线先吃掉的是压缩档的余量，不是立刻塌成 size-fallback。
- 运维**并非完全看不到成因**：receipt 的 `non_evaluating_passes` 每条都带 reason，size-fallback 那条还带 `pre_limit_status`。真正的缺口是**退出码 `3` 不区分成因**——"窗口里没有可求值的 pass"与"证据太大被降级了"给出同一个 3。

修正之后结论仍然成立、只是弱一档：余量约 10%，越过后本面会更频繁地给出 `exit 3`，而 `3` 本身不告诉运维是体积造成的。这条不在本 PR 修，已作为实测证据补进 #1905（它提议的非阻塞摘要中间档正是"缩小 payload"那条路）。

## D2c — 样本取自一台**已知停摆**的机器，以及由此测到的一条边界

诚实交代取样条件：上面两次探查都在 2026-09-16 做，而 node-22 的 `raw → forcing → runs` 自 2026-09-15 13:45 CST 起不再推进（#2432）。所以 169/169 的 `status == "planned"` 描述的是**停摆态**，不是健康态的样子。

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

## 待测（未测的地方写"未测"，不写推断）

- 本设计尚未在 node-22 实机跑过 `list-operator-actions`（EF-8）。D2 的取证只覆盖**输入形状**，不覆盖命令在真实证据根上的端到端退出码。
- 169 趟样本里**没有**任何 size-fallback、不可读或收窄的 pass，因此 D3 各行全部只能靠构造的 fixture 立论，线上无对照样本。
- D2 的**原始**三次只读探查没有采集顶层 `sources` 的取值分布（round 1 A2 之前 `sources` 不是判据），但该分布在 round 1 裁决 A2 时已由编排者在 node-22 活动证据根上单独实测：**177/177 趟 `sources` 均为 `("gfs","IFS")` 全集，零趟缺键**——这正是"加 `sources` 判据不会把线上打成 exit 3"的依据，不是推断。EF-7 在本 PR 修复后的 head 上复核时把它并入 D2 正式重测一次，届时以重测值为准。
