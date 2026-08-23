## Risk Triage

- **Fixture level**: high。改动位于生产调度器核心判定路径上的一个**安全门**，
  且推翻一条被 spec 明文记录的决定。需要完整 cross-review。
- **风险轴**：(1) 放宽一个 gap 判定 —— 误判 complete 会让回填跳过真正没跑完的 cycle；
  (2) 判定顺序变更 —— 可能改变未预期候选类别的路径；(3) 生产全停下的紧急修复，
  时间压力本身是风险。

## Must-Preserve Behavior

- `terminal_init_state_match` 判出的**真实不一致**（present 字段互相矛盾）仍然是硬 gap，
  一步都不放宽。
- `absent` 分支的现有语义与容忍条件完全不变。
- successor 未 ready / 不 usable 时，无论比较结果如何仍判 `gap`。
- 老流域（`init_state_verdict=match`）路径与改动前逐字节一致。
- 从未运行过的 cycle（`decision=None` 或非 terminal）仍判 `gap`。

## Seams Under Test

- `_cycle_completion_verdict`（`scheduler_discovery.py:279-341`）的判定顺序。
- `_terminal_init_state_verdict`（`scheduler_discovery.py:529-546`）的返回值域。
- `scheduler_candidates.py:425-438` 的 warm-admission / classify 顺序。

## D1 — terminal 终态优先于 warm-start 准入

warm-start 准入是**发起**决策；完成度判定问的是**已发生**的事实。今天的顺序让前者
否决后者。改为先取 `candidate_state_decider` 的结论：非 terminal 才落回原有的
strict/successor 早退。

同函数 :360-381 的兜底分支做的正是这个纯 terminal 判定 —— 它今天被 :311 挡住永远不可达。
本改动等于让第一分支与兜底分支的判定基准一致。

## D2 — 拆开 `conflict` 的两种含义

`_terminal_init_state_verdict` 今天对两种情况都返回 `CONFLICT`：

```python
selected = strict_evidence.get("candidate_state")
if not isinstance(selected, Mapping) or init_state_field(selected, "state_id") in (None, ""):
    return TERMINAL_INIT_STATE_CONFLICT      # (a) 无从比较
return terminal_init_state_match(selected, terminal_evidence.get("hydro_run"))  # (b) 真实不一致
```

其 docstring 明确写着 (a) 判 gap 是**有意为之**（"keeps today's gap, which `conflict`
expresses here"）。该函数成文时这条路径**只在 strict-ready 下可达**，那时 (a) 几乎不会
发生；D1 的 reorder 扩大了可达性，"未指名 state"的含义随之改变——从"异常"变成
"warm-start 尚未 ready 的正常情形"。

因此 (a) 获得独立 verdict `unverifiable`，而**不是**并进 `absent`：PR 必须**可见地**
推翻那条被记录的决定，而不是把两种语义糊在一起。`absent` 的含义（terminal 证据本身
不带 init-state 字段）与 `unverifiable`（strict 侧没给出可比对象）是不同的事实，
合并会让后续读者无法区分。

## D3 — `unverifiable` 的容忍规则

当且仅当 `_successor_state_proves_continuity` 为真时判 `complete`——沿用 `absent`
分支已有的同一条物理连续性标准。**不发明新的宽松度**：后继 checkpoint 存在且 usable，
本身就是这一趟确实跑完并产出了可用 state 的物理证明。

## D4 — 候选循环

`scheduler_candidates.py:425-438` 是同型缺陷，只贡献 blocked 噪声、不决定窗口，
但必须与 D1-D3 一起改：只改 D1-D3 会留下 28 个无意义 blocked 记录，只改 D4 窗口照样不动。

注：同函数 :417-424 的 `completed_duplicate_pipeline` 分支要求
`not callable(state_provider)`，node-22 db-free 下 `state_provider` 可调用，
该分支永不触发，与本修复无关。

## 行为变更面（node-22 实测，非断言）

只读 dry-run 探针（隔离 workspace-root + 独立 lock-path，生产 journal）逐 model 补算
`decision` / `strict_ready` / `init_state_verdict` / `successor_proves_continuity`。

满足 `init_state_verdict=conflict ∧ successor_proves_continuity=true ∧ decision=terminal`
—— 即本改动唯一会改变结论的组合 —— 的行，在整个探针窗口内只有一类：
`2026-08-08 00:00` 的 7 个新流域（每 source）。其余全部是 `match`（老流域）或
`decision=None`（未运行、无 terminal），路径与改动前完全一致。

## D5 — 根因：history-existence 按 valid_time 收窄

`packages/common/state_manager.py:1470-1482` 今天的注释写得很明白：

> For §8 history-existence semantics we accept ANY usable entry for this `model_id + source_id`
> regardless of valid_time — a state snapshot at `valid_time == cutoff` (the exact-predecessor
> location) still counts as history because it proves the model was previously exercised.

它论证的是 `== cutoff` 该算数，但实现顺带把 **`> cutoff` 也算了进来**。而 `> cutoff` 的条目
只可能由候选**自己那趟**（或更晚的 cycle）产出——把一趟运行自己的输出当成"它有前驱"的证据，
是循环论证。

后果：新上线 model 的第一个 cycle 跑完写出 `valid_time = cutoff + lead` 的条目后，
`exists_any_generation` 永久翻 True，`scheduler_generation.py:1057` 的 packaged-IC bootstrap
分支从此永远关闭，而它需要的前驱 cycle 从来不存在。

修法：`entries_for_model` 按 `valid_time <= cutoff` 过滤。`expected_key` 落在
`valid_time == cutoff`，仍在范围内，精确前驱查找与 wrong-generation 隔离路径完全不受影响。

### 这不是新发现的缺陷类

#1735 已经识别过**同一失效模式**（`scheduler_backfill_predecessor.py:78`）：

> A recalibrated model's clone row makes `exists_any_generation` True at every cycle, so a
> pre-`t*` submission blocks as `block_predecessor_pending` and this path would answer by
> synthesizing a candidate at an EVEN EARLIER cycle — walking backward out of the window with
> no reachable base case.

当时的解法是给重标定 model 加 lineage-cutover scope-out——一个 per-case workaround。
新上线的流域没有 lineage cutover，于是漏网。D5 是那个 workaround 的通用形式；
带 cutover 的 model 行为不变。

### D5 的改动面（node-22 真实 state index 实测）

收窄只对"最早条目 valid_time 晚于候选 cycle"的 (model, source) 对生效。全量清点
（`index-last.json`，3912 条 usable，90 对）：

| 最早 valid_time | 对数 | 影响 |
|---|---|---|
| 2026-06-25 / 06-26 / 07-05 | 72 | 零——远早于任何窗口 |
| **2026-08-08T12:00** | **14** | **正是被钉死的 7 个新流域 ×2 source** |
| 2026-08-22T00:00 | 4 | 零——`basins_huai_main` / `basins_jialingjiang`（#1698 的 M1→M1′ 切换），cutover 前被 `lineage_scoped_out_pre_cutover` 排除在完成度 scope 外 |

**这张表的判别式只管布尔量。** 它证明的是 `history_exists_any_generation` /
`history_exists_current_generation` 这两个**分支选择信号**只对那 14 对翻转，
原先担心的"全机队语义变更"（state 丢失的 model 静默从 packaged IC 重新 bootstrap）
在实测下不成立：72 对既有 model 的历史条目全部远早于窗口内任何 cutoff。

**但收窄同时改变了摘要值，这不在上表覆盖内**（独立 verifier 实测确认）：
`entries_for_model` 还喂 `latest_any` / `latest_current` →
`latest_any_generation_checkpoint` / `latest_current_generation_checkpoint`。
对**任何**在候选 cutoff 之后还有条目的 (model, source) 对——回填老 cycle 时几乎是全部——
这两个摘要从"史上最新"变成"截至候选时刻最新"。实测：某既有对持有 08-18..08-21 的条目，
cutoff=08-19 时 `latest_any` 由 08-21 变为 08-19；布尔量与 `has_exact_predecessor` 不变。

这些摘要在绝大多数路径上**只进 evidence、不改决策**：分支由（未翻转的）布尔量选定
（`scheduler_generation.py:1056` 分支 (c)、`:1108` 分支 (d)），warm-continue 的前驱身份取自
`valid_time == cutoff` 的 `exact_predecessor_entry`（始终在范围内），故 `:1267-1285` 不变。

**唯一的决策相关面**是分支 (d) 的 stale-declaration old-checksum 比较
（`scheduler_generation.py:1164-1167`，`latest_old = history.latest_any_generation_checkpoint`），
仅当 cutoff 或更早不存在当代历史时可达：收窄前 `latest_any` 可能是 cutover **之后**的新代条目
→ `BLOCK_DECLARATION_STALE` / `old_checksum_mismatch`；收窄后取到的是老代条目 → 声明生效。
方向是**纠正性**的（每个消费者都按"候选时刻"求值，未收窄的值让更晚 cycle 的产出去回答关于更早
候选的问题——正是 D5 要消灭的那种循环证据），并已补回归测试钉住。

## Evidence Mapping

见 `tasks.md` Evidence Floor。
