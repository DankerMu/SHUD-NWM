# ADR 0003: Keep review-lens rotation in follow-up cross-review rounds

Date: 2026-08-02

## Status

Accepted (autonomous default-keep; revisit at the next audit sample or on
maintainer override)

## Context

The subagent-workflow review loop rotates additional reviewer lenses into
follow-up (post-fix) cross-review rounds instead of re-running only the
round-1 lens mix. `scripts/loop_log_audit.py` (the cross-run accountability
audit over `docs/review-loop-log.jsonl`) tracks whether later-round verified
catches come from the pinned-core round-1 lenses or from rotated-in lenses,
and flags a keep/cut decision once the attribution sample reaches ~8
multi-round merged PRs.

After PR #1236 (issue #1153) merged, the audit reached the sample and
returned DECIDABLE: **8 multi-round merged PRs, later-round catches
core=2 vs rotated=8**.

## Decision

**Keep rotation.** Later-round catches concentrate in rotated-in lenses
(8 of 10), which is precisely the audit's own keep criterion: rotation is
buying real union recall that re-running the round-1 mix would miss. This
is also the workflow's default (correctness over cost) and changes no
behavior.

Recorded under the run's autonomous default-keep rule — keep/cut is a human
call, and this ADR is the recorded default pending any maintainer override,
not a new policy.

## Consequences

- Follow-up rounds continue to rotate lenses per
  `risk-adaptive-cross-review`; no change to reviewer briefs or budgets.
- Revisit when the audit next flags rotation attribution (larger sample or
  a shifted core/rotated ratio), or immediately on maintainer decision;
  cutting later means reverting follow-up rounds to the round-1 mix as
  described in the workflow's rotation criterion.

## Revisit 2026-08-06 (post PR #1286)

The audit re-flagged rotation attribution at the larger sample: **31
multi-round merged PRs, later-round catches core=2 vs rotated=57**. The
direction is unchanged and stronger (rotated-in lenses carry essentially
all later-round recall), so the keep decision stands under the same
autonomous default-keep rule. No behavior change; next revisit on the
audit's next flag or maintainer override.

## Revisit 2026-08-07 (post PR #1293 / issue #1287)

Audit re-flagged DECIDABLE at the larger sample: 32 multi-round merged
PRs, later-round catches core=2 vs rotated=78. The attribution moved
further in the keep direction (rotated share 80% → 97.5%). Decision
unchanged: **keep rotation**. Next revisit on maintainer override or a
materially changed attribution ratio.

## Revisit 2026-08-11 (post PR #1366 / issue #1203)

Audit re-flagged DECIDABLE at 48 multi-round merged PRs, later-round
catches core=2 vs rotated=156 (rotated share 98.7%). Attribution is
unchanged in direction and still overwhelming, so the **keep rotation**
decision stands. #1203 is itself a data point for it: the round-2 P1
that would have shipped an inert fix (a 64 KiB sidecar read cap against
1.6-2.0 MB production records) came from a rotated-in
production-reachability lens, not from any core lens. Next revisit on
maintainer override or a materially changed attribution ratio.

## Revisit 2026-08-15 (post PR #1390 / issue #1365)

Audit re-flagged DECIDABLE at 53 multi-round merged PRs, later-round
catches core=2 vs rotated=171 (rotated share 98.8%). Direction unchanged
and still overwhelming: **keep rotation** stands under the same
autonomous default-keep rule. Next revisit on maintainer override or a
materially changed attribution ratio.

## Revisit log

- 2026-08-18（PR #1531 / issue #1152 合并后 audit 再次 DECIDABLE）：样本扩至
  75 个多轮 merged PR，later-round catches core=16 vs rotated=180。轮换收益
  比首次裁定（2:8）更悬殊，**维持 keep**，无行为变更。仍为 autonomous
  default-keep，maintainer 可覆盖。
- 2026-08-20（PR #1602 / issues #1180+#1187+#1188 合并后 audit 再次 DECIDABLE）：
  样本扩至 81 个多轮 merged PR，later-round catches **core=34 vs rotated=197**
  （rotated 占比 85.3%）。**维持 keep**，但本次不照抄「方向不变且压倒性」——
  增量本身变了：相对上一条（75 PR，core=16 / rotated=180），core **+18**、
  rotated **+17**，即最近这批里两者边际收益基本持平，累计 rotated 占比自
  98.8%（08-15）→ 91.8%（08-18）→ 85.3% 单调下滑。

  累计证据仍明确支持 keep，故按 autonomous default-keep 维持，无行为变更；
  但这是**连续第三次下滑**，属前几条自己写的「materially changed attribution
  ratio」触发条件。**提请 maintainer 注意**：若下次审计边际上 core ≥ rotated，
  keep 的原始理由（轮换在买真实的 union recall）就不再由数据支撑，届时应作为
  真正的人的裁定而非 autonomous default 处理。本条只报趋势、不替 maintainer 定夺。

  本 PR 自身的数据点与该趋势一致：round-2 三透镜全部 CLEAN（0 条 P0/P1），
  唯一的 P1 级发现（G6 真空腿）出自 **round-1**，而 round-2 轮换进来的透镜只
  产出 P2/P3 级产物精度问题。Phase 7 独立终审同样零阻塞。

- 2026-08-20（PR #1618 / issues #1394+#1397 合并后 audit 再次 DECIDABLE）：样本扩至 82 个
  多轮 merged PR，later-round catches **core=40 vs rotated=197**。本 PR 边际为
  **core +6 / rotated +0**。

  **上一条自己写下的触发条件在数值上触发了**（「若下次审计边际上 core ≥ rotated，keep 的
  原始理由就不再由数据支撑」）。但**据此下结论会是错的，本条明确不这样用**：

  本 PR 的 `rotated = 0` **不是"轮换进来的透镜没抓到东西"，而是"根本没有轮换"**——
  round-2 我用的是与 round-1 **同一组三透镜**（correctness / discriminative-power /
  artifact-accuracy），只换了 agent 实例、没换透镜类型。所以这个数据点**不检验轮换**，
  它只反映编排者的选择。把它读成"轮换不再买到 recall"是把**未施加的处理**当成**无效的处理**。

  故：**维持 keep**，且**不认为**触发条件已被有效满足；上一条设的那道闸**仍然悬着**，
  要等一个**真正做了轮换**的多轮 PR 来检验。

  **同时登记一条工具缺口（本条的主要价值）**：`loop_log_audit.py` 的
  `rotation_attribution()` 把「round-2 透镜 ∉ round-1 透镜集」判为 rotated，于是
  **"没轮换"与"轮换了但没抓到"在指标上不可区分**——前者会持续把 core 推高、稀释
  rotated 占比，让这条 DECIDABLE 逐次逼近一个**由编排习惯而非由证据驱动**的翻转。
  该指标要可信，须能分辨这两种情形（例如让 `round_lenses` 相同时不计入分母，或单列
  `rotation_applied: false`）。在修好之前，**任何基于该比值的 keep/cut 翻转都应视为
  需要 maintainer 复核的人的裁定**，不得走 autonomous default。

  附：本 PR 自身的证据分布与"轮换未被检验"一致——round-2 的 6 条 later-round catches
  全部出自与 round-1 相同的三个透镜（其中 discriminative-power 镜 round-2 为 CLEAN），
  唯一的 P1 级发现出自 round-1，Phase 7 独立终审零阻塞。

- 2026-08-20（PR #1624 / issues #1547+#1549+#1544+#1546+#1545 合并后 audit 再次
  DECIDABLE）：样本扩至 **83** 个多轮 merged PR，later-round catches
  **core=40 vs rotated=200**。相对上一条（PR #1602 之后的 81 PR / core=34 /
  rotated=197）本 PR 边际为 **core +0 / rotated +3**。

  **维持 keep**，但与上一条同理，本数据点**同样不检验轮换**——而且是**反方向**的
  同一个度量伪影：本 PR 的 3 条 later-round catches 全部出自 **Phase 7 独立终审**
  （`round_lenses` 记作 `final-review`），那不是「轮换进来的交叉评审透镜」，而是
  工作流里本来就独立于 round-1 透镜集的终审环节。`rotation_attribution()` 只按
  「round-K 透镜 ∉ round-1 透镜集」判定，于是把它整段计进 `rotated`。

  上一条登记的工具缺口因此得到**第二种独立的表现形式**，且方向相反：
  - PR #1602：**没轮换**（round-2 复用 round-1 同一组透镜）被计进 `core`，把 core 推高；
  - PR #1624：**Phase 7 终审**（结构上不属于轮换）被计进 `rotated`，把 rotated 推高。

  两者叠加意味着这条比值**同时受两个与轮换无关的编排选择污染**，比上一条估计的更不
  可信。修复方向随之扩一条：除「`round_lenses` 相同时不计入分母 / 单列
  `rotation_applied: false`」外，还需**把 Phase 7 终审轮从轮换归因中排除**（或单列
  `final_review` 桶），否则任何长期做 Phase 7 的项目都会自动积累 `rotated`。
  在修好之前，**基于该比值的 keep/cut 翻转仍须 maintainer 人的裁定**，不走
  autonomous default。

  另附一条本 PR 的证据分布：round-1 双透镜共 6 条 later-round-可比的 catch，两侧
  各有产出（判别力镜 3 条、正确性镜 3 条），无单镜独大；真正买到修复的两条
  P2 都出自判别力镜（恒等 mutant 存活、`except` 吞掉被测步骤），与 keep 的原始
  理由方向一致。

- 2026-08-20（PR #1626 / issues #1400+#1427 合并后 audit 再次 DECIDABLE）：样本扩至
  **84** 个多轮 merged PR，later-round catches **core=45 vs rotated=202**。相对上一条
  （PR #1624 之后的 83 PR / core=40 / rotated=200）本 PR 边际为 **core +5 / rotated +2**
  ——core 增量方向与 keep 相反（同序列内 #1602 为 +18、#1618 为 +6，本条不是最大的一次）。

  **维持 keep，并把这条边际明确记为不可采信。** 理由：本 PR 是**第一个同时触发上面
  登记的两种度量伪影**的数据点（#1602/#1618 只呈现前者，#1624 只呈现后者）：
  - round-2 的透镜集 `{correctness-live-probe, artifact-doc-consistency}` 是 round-1
    三镜集的**真子集**（覆盖/变异镜在 round-1 的覆盖缺口修完后被我撤下），属于
    **收缩而非轮换**；`rotation_attribution()` 把这 5 条 later-round catches 全判进
    `core`——与 PR #1602 同一伪影。
  - round-3 的 Phase 7 独立终审（`round_lenses` 记作 `final-review`）贡献的 2 条被判进
    `rotated`——与 PR #1624 同一伪影。

  也就是说 **core +5 / rotated +2 这个比值里，7 条没有一条来自"轮换与否"的对照**，
  它完整地由两个与轮换无关的编排选择决定。工具缺口的两条修复方向（`round_lenses`
  为 round-1 子集/相同时不计入分母；Phase 7 终审轮排除出轮换归因或单列 `final_review`
  桶）在此得到合并验证：**任一未修，比值即被污染；两者都未修时，同一个 PR 可以同时
  向两个方向拉。** 在修好之前，基于该比值的 keep/cut 翻转仍须 maintainer 人的裁定，
  不走 autonomous default。

  附本 PR 与 keep 之实质相关的证据分布（与上面那个被污染的比值无关）：round-1 三镜
  各有 load-bearing 产出（正确性镜 1、覆盖/变异镜 2、文档一致性镜 3），真正**买到**
  修复 pass 的是覆盖/变异镜的一条覆盖缺口（errno 映射表三个规范输出只钉了一个，
  2208 全绿）——按门规则"覆盖缺口一律买 pass"，与 keep 的原始理由方向一致；撤掉该镜后的
  round-2 仍产出 4 条 P2（全为文案/口径类，零行为缺陷），与"覆盖镜的边际价值集中在
  首轮"这一读法相容，但**单个数据点不足以支持撤镜**。另：三轮全部漏掉的 F-1
  （tasks 5.2/5.5 勾了，而承接单 #1627 里三项义务一条都没有）是 Phase 7 终审抓的——
  这条支持的是"保留独立终审"，不是"轮换"。

- 2026-08-20（PR #1625 / issue #1513 合并后 audit 再次 DECIDABLE）：样本扩至 **85** 个
  多轮 merged PR，later-round catches **core=45 vs rotated=205**。本 PR 边际为
  **core +0 / rotated +3**。

  **维持 keep。** 但这条边际的成色需要拆开说，因为它是本序列里**第一个真轮换**的
  数据点：

  round-1 用双镜 `{correctness-invariant, production-blast-radius}`；round-2 我换成
  单镜 `completeness-second-order`，与 round-1 两镜**完全不相交**——不是 #1602/#1626
  那种"收缩为子集"，也不是复用。所以这 3 条 rotated 里有 2 条（两条被本 PR 自己
  证伪的注释、一条 master 合并后变陈的测量）**确实来自轮换进来的镜**，是这条比值
  第一次拿到未被伪影污染的增量。第 3 条仍是老伪影：Phase 7 终审被计进 `rotated`
  （与 #1624 同型），工具缺口未修。

  **然而这条"干净的 +2"恰恰不支持把 keep 读强。** 轮换镜产出的 2 条全是 P3 文档
  准确性，零行为缺陷；本 PR 唯一的 **P1——也是整单最大价值的一条**——出自 round-1 的
  `production-blast-radius` 镜（`test_publish_scheduler_file_registry` 在 umask 002 下
  仍红，不在我测的 12 个文件里）。也就是说：**轮换买到的是文档精度，首轮买到的是
  正确性。**

  更值得记的是，那条 P1 的真正来源**不是镜的选择，而是枚举方法**：我按失败 trace
  反查文件，reviewer 按"模式断言 / provider lock / `SHARED_PROVIDER_MODE` / `LocalObjectStore`"
  grep 出候选再扫 47 个 suite。同一个"生产影响面"镜，换一种候选枚举法，结果差一条 P1。
  round-2 把这一点推到极致：用 AST 建全仓库对 `provider_atomic` 的反向 import 闭包
  （86 个文件）实跑，得到"扫干净了"的**可证伪结论**，而不是"我看了一圈没发现"。

  **对 keep/cut 的实质启示**（与那条被污染的比值无关）：真正决定 later-round 产出的
  变量可能不是"镜是否轮换"，而是"该镜是否被要求给出可证伪的枚举"。若要让这条 ADR
  的比值有判别力，除已登记的两条工具修复方向外，建议再记一条**度量方向**：区分
  catch 是来自"新视角"还是来自"同视角 + 更强的枚举方法"。本 PR 提供的读法是后者权重
  更大，但**单个数据点不足以支持撤镜**，在工具缺口修好之前 keep/cut 翻转仍须
  maintainer 人的裁定，不走 autonomous default。

## Revisit 2026-08-20 (post PR #1443 / issue #1341)

审计在更大样本上再次 DECIDABLE：**86 个多轮 merged PR，后轮命中
core=46 / rotated=211**（轮换占比 82%）。方向未变，**keep rotation 维持**
（仍走 autonomous default-keep；翻转仍须 maintainer 裁定）。

本 PR 的数据点对这条 ADR 的判别力有直接影响，且与上一条 revisit 的论点同向
但更极端：

- **本单唯一的 P1 不来自任何审查镜——来自 Evidence Floor 的实机 EXPLAIN 门。**
  round-1/round-2 全绿、round-3 的 reviewer 也在 SQL 语义四轴上返回零候选；
  真正抓到 45 倍性能退化（national z4 瓦片 0.77s → 34.7s，planner 丢失逐段
  索引探针改走 1.98 亿行 join filter）的是 tasks 2.5 要求的 node-27
  `EXPLAIN (ANALYZE, BUFFERS)` before/after 对照。**没有活库、没有生产规模
  数据，任何镜都看不见它**——两种形态在 SQL 层面语义完全等价。
- round-3 reviewer 产出的 1×P1 + 2×P2 全部落在规格/文档层（spec delta 与新
  SQL 自相矛盾、时延闸在其自己强制的 pin 上不可满足、迁移头注释过期 +
  #1342 顺序约束无处记账）。这些有价值且必须修，但**没有一条是行为缺陷**。

**对 keep/cut 的实质启示**（延续上一条 revisit 的 thesis）：决定 later-round
产出的变量，排序上可能是「(1) 是否有实机/生产规模 oracle 可跑 > (2) 该镜是否
被要求给出可证伪的枚举 > (3) 镜本身是否新」。这条 ADR 目前只度量 (3)，因此
其比值的判别力被 (1)(2) 稀释——与上一条记的度量方向缺口是同一个问题的两面。
建议的度量修复不变，并追加一条：**区分 catch 来自"读代码/读规格"还是来自
"在生产 oracle 上实测"**。在这三类被分开计数之前，比值只能支持"不撤镜"，
不能支持"镜是主要功臣"。

## Revisit 2026-08-20 (post PR #1639 / issue #1119)

审计仍 DECIDABLE，样本与上一条 revisit 同量级（**86 个多轮 merged PR，后轮
命中 core=46 / rotated=211**）。方向未变，**keep rotation 维持**（autonomous
default-keep；翻转仍须 maintainer 裁定）。

本单提供的是一个**此前未出现过的形状**：**综合交叉审查那一轮产出零发现，而
全部 9 条 catch 来自它前后的两个门。**

- **Phase 0.5 fixture 只读审查：6 条**，含本单唯一的 P1——我写的 Evidence
  Floor 与 tasks 1.3 互斥（要求"证明没有断言被改"，而
  `tests/test_timescale_write_guard_wired.py:413` 的
  `assert delete_calls[0][1] == ("fv_a",)` 在 DELETE 加边界后必然红，因为
  1-tuple 参数在语义上只能由无界 DELETE 满足）。按字面执行会把实现者逼向
  "不加边界"。另有一条 P2 推翻了 design D1 的**假必要性论证**（声称扩展
  `_replace_values` 是唯一过 meta-guard 的形状，实则把 bounded DELETE 搬进
  `_guard` 同样全过），一条 P2 补上 purge 语义回归的 oracle 缺口。
- **Phase 4 综合交叉审查（两路四包）：0 条。**
- **Phase 7 终审：3 条**，全 P3，含 in-range 扫描查不出的引用漂移。

**对这条 ADR 的度量启示（第三个候选变量）。** 前两条 revisit 已指出
「镜是否新」判别力不足，并提出「是否有实机 oracle」「是否被要求可证伪枚举」
两个更强的变量。本单再加一条：**门读的是什么工件**。

- 读**规格、且在实现之前**的门（Phase 0.5），本单命中率最高且抓到唯一 P1。
  它能抓到的是"规格自相矛盾/论证造假/覆盖缺口"——**这类缺陷在代码写出来之后
  就不再是缺陷，而是既成事实**，任何读代码的镜都看不见它们（代码会忠实实现
  一个错的规格，然后全绿）。
- 读**代码、在实现之后**的门（Phase 4/7），本单只产出文档层 P3。合理的解释
  是：因为 Phase 0.5 已经把规格修对了，实现照做即可，留给代码审查的只剩
  文书。**这正是"前置门有效"的表现，而不是"后置门无用"的证据**——两者不可
  由单点区分。

⇒ 度量修复建议追加第三条：**按门读的工件分类计数**（spec-before-impl /
code-after-impl / 实机 oracle）。在这三类与前两条 revisit 提出的分类被分开
统计之前，rotated 比值仍只能支持"不撤镜"。

**一条与本 ADR 相邻但独立的方法论账**（#1513 记在这里的爆炸半径方法，本单
发现仍有缺口）：ADR 0003 引入的 AST 反向 import 闭包解决了"间接 importer"，
但解决不了**内容耦合**——`tests/test_node27_timeseries_compression_capture.py`
把 `workers/forcing_producer/store.py` 的字节拷进 fixture repo 并断言
`"check_batch_targets_uncompressed" in source`
（`scripts/node27_timeseries_compression_capture.py:400`），它**不 import**
store，任何 import 图都必然漏掉。闭包需要第三条腿：grep 出把源码当数据读的
消费者。同理，`file:line` 引用核对必须按**锚点内容**而非行数范围——本单证明
范围扫描是安全错觉（change 自己的编辑推移引用后，引用仍在文件范围内）。

---

## 2026-08-20 复访（PR #1637，#1418 + #1451 纯 oracle 合批）

样本 87 → 多轮 merged PR，later-round catches **core=46 / rotated=222**
（上次 84 / 45 / 202）。本条 PR 贡献 later-round 11 条（round 2 八条 + 终审三条），
按脚本口径**全部**记为 rotated、core 增 0。

**但这次不能照 DECIDABLE 的字面结论走——rotated 这个数本身有测量缺陷，且量级不小。**

`loop_log_audit.py:63-76` 的 `rotation_attribution` 只把 `round_lenses[0]` 当作
「钉住的 core 镜」：

```python
core_lenses = set(lenses[0]) if lenses else set()
```

实测全语料（96 条多轮、catches 为字典型的记录）：

| 形状 | 条数 | 其 later-round catches | 后果 |
|---|---|---|---|
| `round_lenses[0]` 是**裸字符串** | **50** | **163** | `set("fixture-review")` 把字符串**按字符**拆成 `{'f','i','x',…}`，任何镜名都匹配不上 → core 恒为 0，全部强制记入 rotated |
| `round_lenses[0]` 是单元素 `["fixture-review"]` | 2（含本条）| 14 | 形式正确，但 core 集合 = {fixture 评审镜}，后续交叉评审镜结构上**不可能**是 core |

**222 条 rotated 里有 177 条（80%）来自这两类。** 也就是说
"catches concentrate in rotated-in lenses" 这个 DECIDABLE 的标题句，
大部分是**记录 schema 不一致 + 索引 0 取的是 fixture 镜**这两件事的产物，
不是"轮换镜真的更能抓"的证据。

一个具体的误记例子就在本条：本 PR 的 `round_lenses` 是
`[[fixture-review], [A, B, C], [A', C], [final-review]]`，其中
`durable-text-truth` 这面镜**在 round 1 与 round 2 都在**——它是**被钉住的**，
不是轮换进来的；而它在 round 2 抓的 4 条全部被记成 rotated。

**裁定：维持 keep，但理由与前几次不同。**
前几次是"rotated 高但分类不足以支持撤镜"；这次是"**rotated 这个数在修好之前
不能用来支持任何方向的决定**"。撤镜与保镜都不该以它为据。

**度量修复建议（第四条，且这条是先决条件）：**

1. **先修 schema**：`round_lenses` 的每一项必须是**列表**。`evidence_check.py`
   目前不校验这一层（它放行了本条 PR 最初写成字符串 `catches` 的版本，也放行了
   那 50 条裸字符串 `round_lenses[0]`），应补两条校验：`round_lenses` 元素为
   list[str]，`catches` 元素为 `{round, lens, class, severity}` 字典。
2. **core 的定义要改**：应是「**在 round 1 出现过、且在 later round 仍在**的镜」，
   即 `set(lenses[1]) & set(lenses[i])`（当 index 0 是 fixture 评审时），
   而不是无条件取 `lenses[0]`。现口径把"前置 fixture 门"错当成了"钉住的 core 镜"。
3. 在 1 与 2 落地并**回填**已有记录之前，本 ADR 的 keep/cut 只能维持 keep。

**本单在镜有效性上的实际观察（与上条 revisit 的"门读什么工件"一致）：**
Phase 0.5 fixture 评审（读规格、在实现之前）两轮各出 P1，其中一条指出
fail-closed 只钉了拒绝清单、从未钉放行集——**这类缺陷在代码写出来之后就不再是
缺陷**。Phase 4 round 1 抓到三条真实覆盖缺口（各自活过全量 1773 且翻转生产判据），
Phase 4 round 2 抓到的最贵一条是**round 1 的修复自己制造的假红**
（钉 `FunctionType` 会让 `@functools.lru_cache` 装饰既有函数打红）。
后者是"**修复审查**"这一类门的价值证据：它读的既不是规格也不是原始代码，
而是**上一轮的修复**。建议把它作为第四类工件单独计数。

---

## 2026-08-20 复访（PR #1636，#1592 + #1589 durable 写边界）

样本 88 → 多轮 merged PR，later-round catches **core=46 / rotated=223**
（上次 87 / 46 / 222）。本 PR 边际 **core +0 / rotated +1**。

**这 +1 暴露了同一个测量缺陷的第三种机制，前一条没点出来。**

本 PR 的 `round_lenses` 是
`[[fixture-review, fixture-review-2], [correctness-durable-state, blast-radius-oracle-integrity],
[repair-two-sidedness, durable-text-truth], [phase7-final-and-delta]]`。
`rotation_attribution` 只数 `round >= 2` 的 catch，本 PR 落在该区间的只有终审那一条
（`phase7-final-and-delta`）。它被记为 rotated。

但 **Phase 7 终审在结构上永远不可能是 core**：core 集合取自 `round_lenses[0]`，
而索引 0 恒为 fixture 评审轮；`final-review` / `phase7-*` 这类镜按工作流定义只出现在最后一轮，
**任何 PR 的终审 catch 都必然记入 rotated，与轮换策略是否有效完全无关**。
这不是前一条记录的两种形状（裸字符串按字符拆解、索引 0 取 fixture 镜）中的任何一种，
是第三条独立通道：**轮次角色被当成了镜身份**。

前一条统计过 222 条 rotated 里 177 条（80%）来自前两种形状。加上这一条：
终审轮的 catch 是**系统性**记入 rotated 的第三个来源，进一步稀释
"catches concentrate in rotated-in lenses" 这句话的信息量。

**裁定：维持 keep，理由同前一条——rotated 这个数在修好之前不构成撤镜的证据。**
本 PR 自身的经验也不支持撤镜：真正值钱的那条（咽喉 strip 在比较侧缺失导致重放永不收敛，critical）
是 round 1 的 `correctness-durable-state` 抓的，那面镜在本 PR 里只上过一轮，
按脚本口径连 `round >= 2` 的门都进不去，**根本没进这个统计**。
⇒ 该指标不仅高估 rotated，还漏掉了最高价值的那一类 catch（首轮 critical）。

**修复口径的前置条件（累计三条，仍未排期）**：
(1) `round_lenses[0]` 的 schema 统一为数组（50 条裸字符串记录需回填或标注不可用）；
(2) core 集合不能取 fixture 轮，应取"首个交叉评审轮"的镜集；
(3) 轮次角色镜（`final-review` / `phase7-*` / `fixture-review*`）从 core/rotated 二分里剔出，
单独成类——它们是轮次属性，不是轮换选择。

## Revisit 2026-08-20（post PR #1643 / issue #1378）

审计口径：89 个多轮 merged PR，later-round catches core=46 / rotated=228。**决定：keep 不变**，
且三条口径修复前置条件仍未排期（见上一节）。本 PR 的经验数据继续支持"轮次角色镜单列"：
全场唯一致命 P1（ride-along ANALYZE 清零 TimescaleDB 保留的 origin relstats）出自 Phase 7
终审读上游源码，三轮常规透镜全部漏过——终审不是"轮换进来的透镜"，而是有独立方法论
（外部权威源交叉验证）的轮次角色，佐证把它从 core/rotated 二分里剔出的必要性。

## Revisit 2026-08-21（post PR #1655 / issue #1442）

审计口径：90 个多轮 merged PR，later-round catches core=46 / rotated=231。**决定：keep 不变**，
三条口径修复前置条件仍未排期。本役再添一例轮次角色镜的独立价值：round 2 clean 之后，
Phase 7 终审用索引 DDL 清单法从结构推出"segment 谓词清零丢失全部逐 segment 索引路径"的
P1（与 E4 live EXPLAIN 独立收敛），终审重跑又以 receipt 的 BUFFERS 逐字段比对证伪了
修复自身的机理文本——连续第三个 issue 的最重发现均出自终审的独立方法学而非轮换透镜，
"轮次角色镜单列"的口径修复必要性进一步坐实。

## Revisit 2026-08-21（post PR #1651 / issue #1633）

审计口径：91 个多轮 merged PR，later-round catches core=46 / rotated=231。**决定：keep 不变**，
三条既有口径修复前置条件仍未排期。本 PR 的 Round 2 是证据措辞修复后的 clean 轮，0 candidates；
因此样本数 +1 而两类 catch 均 +0。这个数据点既不证明轮换有效，也不证明轮换无效——没有
later-round finding 就没有可归因的 catch，不能把零产出反读成撤镜证据。

本 PR 真正有价值的新增信号仍来自不同门读不同工件：最终 fixture review 在旧 CI 全绿后抓到
non-daemon worker 会在 deadline assertion 之后继续卡住 `threading._shutdown()` 的 P1 级
contract gap；Round 1 则由 test-evidence 镜 + 独立 verifier 抓到两条 exact-mutant 证据错误。
它们支持保留多层门，而不是加强当前已知受污染的 core/rotated 比值。故维持 autonomous
默认 keep；任何翻转仍须先修 ADR 已登记的归因 schema/轮次角色污染，再由 maintainer 裁定。

## Revisit 2026-08-21 (post PR #1657 / issue #1596)

Audit re-flagged DECIDABLE at 92 multi-round merged PRs, later-round
catches core=46 vs rotated=242 (rotated share 84.0%, down from 98.8% on
2026-08-15 — 43 of the 46 core catches come from PRs merged 2026-08-17
to 08-20 (#1531, #1551, #1566, #1574, #1591, #1618, #1626, …) whose
round-2+ catches carry round-1 lens names, i.e. re-review rounds re-ran
the same lenses and the audit counts those as core; rotated-in lenses
did not stop catching). Direction unchanged: **keep rotation** stands under
the same autonomous default-keep rule. #1596 is a cautionary data point
on the other axis: its rounds 3-5 catches all came from rotated-in
delta-verification lenses, but every one was a fix-propagation miss
across carriers of one narrative — rotation found them, it did not
prevent them; the corrective lives in fix-brief carrier checklists, not
in the lens mix. Next revisit on maintainer override or a materially
changed attribution ratio.

## Revisit 2026-08-21 (post PR #1666 / issue #1468)

Audit re-flagged DECIDABLE at 93 multi-round merged PRs, later-round
catches core=46 vs rotated=244 (rotated share 84.1%, unchanged from the
same-day revisit above). #1468's two later-round catches both came from the
rotated-in fix-correctness lens (fixture residue; an oracle setup-proof gap).
Direction unchanged: **keep rotation** stands; no new analysis warranted for a
one-PR delta. Next revisit on maintainer override or a materially changed
attribution ratio.

## Revisit 2026-08-21 (post PR #1668 / issue #1324)

Audit re-flagged DECIDABLE at 94 multi-round merged PRs, later-round catches
core=46 vs rotated=244. #1324 adds one multi-round sample but no later-round
catch: Round 1 found one fixture-named test-evidence gap, and Round 2 closed it
cleanly. The accumulated ratio is therefore unchanged.

**Keep rotation** remains the recorded decision. This sample adds no evidence
for either direction, and the attribution caveats above still apply: lens-name
suffixes, fixture/final-review roles, and non-rotation subset rounds can distort
the core/rotated split. No policy change; a future reversal still requires the
recorded metric fixes plus maintainer review.

## Revisit 2026-08-21 (post PR #1670 / issue #1597)

Audit re-flagged DECIDABLE at the same 94 multi-round merged PRs, later-round
catches core=46 vs rotated=244 — #1597 was a single-round PR (compact fixture,
two lenses, two CONFIRMED prose-only catches repaired as a local-repair), so it
adds no multi-round sample and leaves the ratio untouched.

**Keep rotation** remains the recorded decision; nothing in this sample bears
on the attribution question, and the metric caveats recorded above still
stand. No policy change.

## Revisit 2026-08-21 (post PR #1665 / issue #1650)

Audit re-flagged DECIDABLE at 95 multi-round merged PRs, later-round catches
core=46 vs rotated=244. #1650 adds one multi-round sample but no later-round
catch: its three verified findings all came from Round 1; the post-fix Round 2
used one full-scope invariant lens plus integration and test-evidence delta
lenses and returned zero candidates, and Phase 7 was also clean. The accumulated
catch counts are therefore unchanged from the #1668 revisit.

**Keep rotation** remains the recorded decision, with no increase in evidentiary
strength. A clean post-fix round proves closure for this PR; it does not prove
that rotation caused or failed to cause recall. The existing metric caveats
remain load-bearing: fixture/final-review roles, lens-name variants, and rounds
that shrink or reuse rather than rotate the core can all distort the split.
No policy change; any future reversal still requires the recorded attribution
schema/role fixes plus maintainer review.

## Revisit 2026-08-21 (post PR #1667 / issues #1595 + #1600)

Audit re-flagged DECIDABLE at 96 multi-round merged PRs, later-round catches
core=46 vs rotated=244. #1667 adds one multi-round sample but no later-round
catch: Round 1 produced two verified P2 fixes, while the post-fix Round 2 used
an integration/cache full-scope lens plus a test-evidence delta lens and
returned zero candidates; Phase 7 was also clean. The accumulated catch counts
are therefore unchanged from the #1665 revisit.

**Keep rotation** remains the recorded decision, again with no increase in
evidentiary strength. This PR proves that the rotated/subset follow-up mix
closed the two known findings; zero new catches cannot identify whether lens
rotation added recall. The existing attribution caveats remain decisive, so no
policy change is made and any reversal still requires the recorded schema/role
fixes plus maintainer review.

## Revisit 2026-08-21 (post PR #1682 / issue #1644)

Audit remains DECIDABLE at 96 multi-round merged PRs, with later-round catches
core=46 vs rotated=244. PR #1682 is a single-round clean review: six Round 1
lenses produced zero candidates and Phase 7 produced zero new findings. It
therefore adds no multi-round sample and changes neither attribution count.

**Keep rotation** remains the recorded decision, with no new evidence in either
direction. A single-round zero-catch PR cannot test a follow-up-round rotation
policy. The existing attribution caveats remain load-bearing, so any reversal
still requires the recorded schema/round-role fixes plus maintainer review.

## Revisit 2026-08-21 (post PR #1676 / issue #1674 and PR #1683 / issue #1681)

`loop_log_audit` re-flagged DECIDABLE lens-rotation after these two lines:
98 multi-round merged PRs, later-round catches core=46, rotated=245 (both
PRs add one multi-round sample each, neither changes the attribution
counts). Both ran the same shape: round 1 comprehensive with two lenses
(correctness/data- or plan-semantics + spec-conformance/oracle-integrity),
round 2 a focused fix-delta pass. #1676's single verified catch and #1681's
three came in round 1 from the round-1 mix itself; the round-2 focused passes
caught one documentation-truthfulness repeat (#1683) and nothing for #1676.
Neither round-2 pass rotated a new lens in, so these samples say nothing
about rotation either way.

**Keep rotation** remains the recorded decision; no new evidence in either
direction. The attribution caveats above stay load-bearing.

## Revisit 2026-08-21 (after #1414 / PR #1687)

99 multi-round merged PRs, later-round catches core=46, rotated=250 (+5 from
this one PR alone — its rounds 2 and 3 were both focused fix-delta passes,
which the auditor counts as rotated-in).

This is the first sample in a while that says something. #1687 ran three
rounds: round 1 comprehensive (correctness/test-design +
spec-conformance/oracle-integrity, 4 verified catches), then two focused
fix-delta rounds that between them caught 5 more — 4 in round 2, 1 in round
3. Every one of those 5 was the *same* failure class: a claim corrected in
one artifact that never propagated to its upstream design section or to the
published PR body. Round 1's comprehensive mix produced the corrections;
round 1's mix would never have caught the incomplete propagation of its own
fixes, because at round 1 those fixes did not exist yet.

That is the rotation argument in its cleanest form so far: the rotated-in
lens was not looking at a *different part of the change*, it was looking at
*the change made in response to the previous round*. Note the caveat that
keeps this from being decisive — the attribution counts a focused fix-delta
pass as "rotated", and one could argue a fix-delta pass is not a lens
rotation at all but a mandatory re-check that any review loop would run.
Under that stricter reading these 5 catches say nothing about rotating
*subject-matter* lenses.

Also note the diminishing return within the PR: P2 → P2 → P3 across rounds
1/2/3, with round 3 substantively clean. The loop converged rather than
grinding, which is the behavior the ceiling exists to protect.

**Keep rotation** remains the recorded decision, now with one genuinely
supporting sample under the auditor's own attribution rule, and the stricter
reading noted so a future revisit can re-litigate the rule rather than the
decision.

## Revisit 2026-08-22 (after #1398 / PR #1690)

Auditor now reports 100 multi-round merged PRs, later-round catches
**core=53 rotated=257** (was 99 / 46 / 250 at the last revisit). PR #1690 is
the entire delta: 5 comprehensive rounds, the longest loop on record here, and
it hit the 5-round ceiling.

This sample answers the previous revisit's open question directly, and in
rotation's favour. Rounds 2 and 3 were focused fix-delta passes — the case the
stricter reading says should not count as a lens rotation. Rounds 4 and 5 were
genuine subject-matter rotations, and the decisive catch came from one of them:
the `oracle-integrity / regression-safety / script-correctness` lens found a
**P1 in the corrective machinery the documentation lens had just written and
signed off** — a sweep script that silently dropped a declared carrier and still
exited 0, while the Evidence Floor item's pass condition was literally "the
script exits 0". The documentation lens, run in parallel on the same head, read
that same script and reported its self-description accurate. A single-lens loop
would have merged an Evidence Floor oracle that reports PASS with its own stated
scope unmet.

So: under the stricter reading, this PR contributes 2 rounds of *real* rotation
and 1 P1 that only the rotated-in lens could have found, because the lens that
would otherwise own the surface was the same one that authored it. That is the
strongest single argument in the ledger for rotation, and it is a specific
mechanism rather than a count: **rotate at least one lens off the surface its
own previous round produced.**

Counter-note, recorded so the numbers are not read as stronger than they are:
one PR moving the aggregate by 7 core / 7 rotated shows how thin the
multi-round sample still is, and #1690's five rounds were themselves partly
self-inflicted — four of them chased a recurring defect in the fixture's own
meta-checklist, not in the deliverable.

**Keep rotation**, and record the sharper form above as the reason.

## Revisit 2026-08-22 (after #1660 / PR #1696)

Aggregate moved to core=53 rotated=258 (was 53/257). One rotated catch — a
rounding-error contribution to the count. The value of this PR to the decision
is not the count but a clean instance of the mechanism, in a shape the ledger
did not previously contain.

Round 1 ran two lenses on the same 72-line diff. The correctness/concurrency
lens went deep: it measured attempt accounting across six values of `attempts`,
drove 320 iterations of a *real* `os.replace` race to count file descriptors,
and enumerated seven exception shapes against the retry predicate. It returned
clean on every axis, and it was right — the shipped code was correct, and two
later rounds plus a final review never found otherwise.

The rotated-in test-evidence lens looked at the same diff and asked a different
question: not "does the retry work" but "would anything notice if it stopped".
The answer was no. Two mutants that switch the feature off in production —
the module constant set to 1, and the sole production call site pinned to one
attempt — both left the suite fully green, because every test that exercised the
retry injected its own bound and every test that used the default took a
non-retrying path. `grep` for the constant across `tests/` returned nothing.

That is the distinction worth recording: **verifying behavior and verifying that
the behavior is pinned are different questions, and a lens that has just
convinced itself the code is correct is the worst-placed one to ask the second.**
It is the same structural argument as the 2026-08-21 entry (rotate off the
surface your own previous round produced), reached from the opposite direction —
there the risk was auditing your own artifact, here it is auditing your own
conclusion.

Round 2 then rotated both lenses again, onto the corrective action itself and
onto the integration boundary. The audit lens caught a one-word fidelity drift
in the round-1 spec fix that would have been silently folded into the base spec
on `openspec archive`; the integration lens found that the diagnostic token this
PR introduced never reaches server logs, which is where an operator would look
for it (routed to #1704). Neither surface had a prior owner.

Counter-note: two rounds is a cheap sample, and the round-2 findings were P3 and
out-of-scope respectively — neither would have blocked a merge. The honest claim
is about the round-1 coverage gap, which was real, was P2, and was invisible to
the lens best acquainted with the code.

**Keep rotation.** No change to the recorded rule.

## Revisit 2026-08-22 (after #1645 / PR #1689)

Auditor now reports 102 multi-round merged PRs, later-round catches
**core=54 rotated=258** (was 101 / 53 / 258 after PR #1696). PR #1689 is the
entire delta: **core +1, rotated +0**.

This attribution is one of the cleaner `core` samples in the ledger. The
Round 2 P1 was found by the `test-evidence` lens, which was present in Round 1
and intentionally pinned into Round 2. It caught a regression in the same
partial-launch invariant after the implementation had already been repaired:
both explicit-thread tests could still pass when only the exception-path join
was deleted. The independent verifier executed those mutants and observed
Gateway 25/25 and scheduler 30/30 false-green; the corrective parent-side join
proof then made the same mutants deterministically red.

That result establishes that the pinned core still buys fix-regression recall.
It does **not** support reverting to the Round 1 mix: the policy under review is
additive rotation and never rotates the pinned core out. A single `core +1`
sample therefore argues for keeping the core pinned, not for removing the free
rotated slots. Round 3 used full-scope + pinned evidence/concurrency lenses and
was clean, so this PR supplies no rotated catch in either direction.

The accumulated ratio still points toward keep, but all previously recorded
measurement caveats remain: fixture/final-review roles, lens-name variants, and
subset/non-rotation rounds can contaminate the aggregate. **Keep rotation**
under the autonomous default; no policy change. Any future reversal still
requires the recorded attribution schema/round-role fixes plus maintainer
review.

## Revisit 2026-08-22 (after #1664 / PR #1708)

Attribution at 430 log lines: core=54, rotated=258 across 102 multi-round merged
PRs. The DECIDABLE trigger fired again; the decision is again **Keep rotation**,
with no change to the policy.

What this PR adds is not another tally point but a **new failure shape the
rotation caught, and the round-1 mix could not have**.

Round 1 ran three lenses (runtime/DB semantics, oracle strength via executed
mutation, contracts/systemd/docs). The runtime lens returned no P1 and no P2 —
the `lock_timeout` wiring, the SQL interpolation, and the `pgcode` predicate
were all correct on first delivery. Every one of the thirteen verified findings
came from the two rotated-in lenses.

Round 2 rotated again, into "did the round-1 fixes land correctly, and did they
break anything" plus an adversarial/deployment lens. That first lens found a
defect **in the orchestrator's own round-1 fix**: widening the alert wrapper's
soft-exit surface from three paths to seven (each a deliberate `exit 0`) made
the Evidence Floor's channel-smoke criterion — "unit `Result=success` and the
mail channel reported no error" — satisfiable by every silent-failure state it
existed to detect. A missing env variable would have recorded PASS on an alert
lane that mails nothing, and that criterion was the only live-channel check
before the post-merge receipt.

The generalisable point: **a fix pass mutates the very surface the prior round
just certified, and the lens that authored the fix is the worst-placed one to
re-certify it.** Here the author was the orchestrator, so no reviewer had a
stake in defending the change — and it still took a lens explicitly pointed at
the fix delta to see it. This is the same asymmetry recorded in the 2026-08-22
(#1660) revisit, one level up: there, a lens that had just proved behaviour
correct was ill-placed to ask whether the behaviour was pinned; here, the actor
that just repaired a surface is ill-placed to ask whether the repair widened it.

The same round's adversarial lens produced the other independent catch: the new
per-chunk timing `print` sits after a completed `drop_chunks`, so an `OSError`
on the log volume — the disk-full condition retention exists to relieve — would
escape into the uncaught-error path and publish a `refused` receipt that the
schema forbids from recording `dropped_chunks`. A deletion that really happened
would have been recorded nowhere. No correctness- or coverage-shaped lens asks
"what does the failure of a diagnostic do to the evidence record".

Measurement caveats from every prior revisit stand unchanged (fixture and
final-review role contamination, lens-name variants, subset rounds). Off-
vocabulary fixture labels remain excluded from the keep/cut buckets and are
still eight distinct historical spellings; `evidence_check --loop-log-entry`
rejects new ones, so that contamination is bounded and shrinking in share.
**Keep rotation** under the autonomous default. Any future reversal still
requires the recorded attribution-schema and round-role fixes plus maintainer
review.

- 2026-08-22（PR #1706 / issue #1697 合并后 audit 再次 DECIDABLE）：样本扩至 **103** 个
  多轮 merged PR，later-round catches **core=58 vs rotated=261**。本 PR 边际为
  **core +4 / rotated +3**。

  **维持 keep。但本数据点对轮换零信息量**，理由与 #1626 同型且同样是**两个伪影
  同时出现在一个 PR 上**——这已是该组合的第二次实测，不是推测：

  - round-2 用的是与 round-1 **完全相同的六镜**（correctness / integration /
    security-perf / test-evidence / spec-compliance / invariant-state）。这 4 条
    later-round catches 因此全被判进 `core`——与 #1602 同一伪影。
  - Phase 7 独立终审及其两次 rerun（`round_lenses` 记作 `final-gap-sweep`）贡献的
    3 条被判进 `rotated`——与 #1624 同一伪影。

  即 **这 7 条里没有一条来自「轮换与否」的对照**。

  比前几条多出的一点是**为什么没轮换**：这不是编排习惯，而是**规则逼出来的**。
  round-1 的 5 条 verified 里有 P1，按 severity-rationing 买下了修复 pass，
  于是 round-2 的任务被定义为「同一批面的修复是否落对、有没有带坏别的」——
  一个**验证性**而非**探索性**的轮次。对同一组面做再验证时复用同一组镜是正确的选择，
  换镜反而会丢掉「这条修复动的正是我上轮认证过的那处」这个上下文。

  这对已登记的工具缺口是一条**新的限定**：`rotation_attribution()` 不只分不清
  「没轮换」与「轮换了但没抓到」，它还分不清**「不该轮换」**——修复验证轮复用镜集是
  按门规则的正确行为，却被记成轮换失利的证据。修复方向因此再扩一条：除
  「`round_lenses` 为 round-1 子集/相同时不计入分母」「Phase 7 终审轮排除出轮换归因」
  外，还应能标注轮次意图（探索 vs 修复验证），否则**越是严格执行 severity-rationing
  的项目，越会自动积累 `core`**，把这条比值推向一个由门规则而非由证据驱动的翻转。
  在修好之前，基于该比值的 keep/cut 翻转仍须 maintainer 人的裁定，不走 autonomous default。

  附本 PR 与 keep 之实质相关的证据分布（与上面那个被污染的比值无关）：round-1 六镜里
  真正 load-bearing 的是 correctness 镜的一条 P1（中途 abort 后已写入的克隆行无 receipt，
  verifier 实跑 repro 确认）。更值得记的是 **verifier 两次推翻了 reviewer 的支撑论证
  而结论存活**——C1 上 invariant-state 镜断言 `run()` 会预校验整批因而与本缺口不同，
  verifier 证明 `run()` 的循环前检查只校验 warm/cold 划分的**形状**、不校验门结果；
  C3 上 test-evidence 镜断言某测试覆盖「循环内 `M1 == M1'` 检查」，而循环里根本没有该
  检查。这两条支持的不是「轮换」，而是**「reviewer 出候选、verifier 独立裁决」这层分工**：
  镜的数量买不到这个，独立裁决才买得到。

## Revisit 2026-08-22 (after #1326 / PR #1680)

Integrated audit now reports 104 multi-round merged PRs, later-round catches
**core=59 rotated=264**. The branch-local observation before #1697 merged was
103 / 55 / 261 from the shared 102 / 54 / 258 baseline; #1326's contribution
remains **core +1, rotated +3**, while #1697 independently added +4 / +3.

The raw ratio again overstates rotation. Two of the three entries counted as
`rotated` came from the Phase 6.2 `invariant-audit` gate, not from a
comprehensive-review free slot. The script sees only a lens name absent from
Round 1 and therefore folds a different workflow role into rotation, the same
role-attribution defect already recorded for fixture and final-review gates.
Only one rotated catch is a genuine reviewer-lens sample: the Round 2
`invariant-state-machine-compatibility` lens found the P1 multi-hop path where
an empty-ID `submission_failed` hop erased an earlier confirmed Slurm master.

The `core +1` is also real: pinned `test-evidence` found that the production-
reachable normal-start indexed replacement had no committed oracle even though
the restart-at-forecast trailing form did. Both catches bought necessary work:
the first forced the depth redesign from adjacent-result copying to a
stage-loop provenance owner; the second forced a distinct indexed real-path
test so the redesign could not mask caller wiring.

This is a clean argument for the **additive** policy the ADR actually chose:
keep the core pinned for fix-regression/coverage recall, and rotate a free slot
for sibling-state/history depth. It is not evidence that `261 / 55` is a
trustworthy effect size; two-thirds of this PR's apparent rotated increment is
a gate-role classification artifact.

**Keep rotation.** No policy change. The same prerequisites remain before any
autonomous reversal: normalize the `round_lenses` schema, define core from the
first comprehensive round, and exclude or separately bucket fixture,
Phase 6.2 invariant-audit, and Phase 7 final-review roles.

## Revisit 2026-08-22 (after #1648 / PR #1710)

The audit now reports **105** multi-round merged PRs and later-round catches
**core=59 / rotated=264**. Compared with the immediately preceding #1326
receipt (104 / 59 / 264), #1648 adds one PR to the denominator and **zero** to
either catch bucket.

That zero is expected and carries no keep/cut information. #1648's only verified
catch was a Round 1 `concurrency` finding: the newly added post-takeover
regression could parse a lock file during its parent's truncate/write window.
Round 2 was a fix-verification pass using only the selected-risk pinned core and
returned no candidates. No free-slot lens was rotated in, and no later-round
catch exists to attribute.

Therefore this data point is neither evidence that rotation failed nor new
evidence that it worked; treating the added denominator as either would repeat
the measurement error documented above (conflating "no rotation applied" with
"rotation applied and found nothing"). The cumulative counts still rule out a
cut under the ADR's default, so **keep rotation** remains unchanged. Any future
reversal still requires the recorded schema repair and maintainer review; this
revisit adds no policy change.

## Revisit 2026-08-22 (after #1587 / PR #1679)

The audit now reports **106** multi-round merged PRs and later-round catches
**core=64 / rotated=264**. Relative to the preceding #1648 receipt, #1679 adds
one PR and **core +5 / rotated +0**.

This is another pinned-core sample, not a rotation experiment. Round 1 included
`test-evidence`; Rounds 2–4 intentionally kept that same lens while closing one
recurring evidence invariant, and Round 5 was clean. The five later-round
catches therefore show that a pinned lens retains real fix-regression recall:
it caught fallback/provenance holes and then progressively replaced hand-copied
mutation counts with a replay-backed receipt. No free-slot lens was rotated in,
so the zero rotated increment says nothing about rotation's value.

The five-round shape also reinforces the existing measurement caveat: a depth
loop that correctly pins the owner of a recurring invariant mechanically pushes
`core` upward. Reading that movement as evidence to revert to the Round 1 mix
would confuse severity-driven fix verification with a failed exploration
policy. **Keep rotation** remains unchanged; pinned core and additive free-slot
rotation serve different jobs. Any reversal still requires the recorded schema
and round-role fixes plus maintainer review.

## Revisit 2026-08-22 (after #1698 / PR #1721 and #1699 / PR #1724)

The audit still reports **106** multi-round merged PRs and later-round catches
**core=64 / rotated=264** — numerically identical to the preceding #1587
receipt. Both #1698 and #1699 are `fixture: none, rounds: 0` operational
rollouts (production scheduler-registry cutover and new-basin onboarding) whose
repository increment was a runbook section; neither ran a cross-review loop, so
neither contributes a multi-round denominator or a later-round catch.

Two zero-increment lines are not evidence in either direction, and recording
them as a revisit exists only to keep the ledger honest about *why* the counters
did not move: no rotation was applied because no review rounds ran at all. That
is the third distinct shape this ADR has now had to separate from a genuine
rotation experiment (pinned-core depth loops, fix-verification-only rounds, and
now review-exempt ops rollouts). **Keep rotation** remains unchanged; any
reversal still requires the recorded schema and round-role fixes plus maintainer
review.

## Revisit 2026-08-22 (after #1714 / PR #1719)

The audit now reports **107** multi-round merged PRs with later-round catches
**core=66 / rotated=264**. PR #1719 is the first line since #1587 to move the
counters at all, and it moved only the `core` side: **+2 core, +0 rotated**.

The movement is real but does not test rotation. Round 2 of #1719 ran five
lenses — correctness, integration, spec-compliance, invariant-state,
test-evidence — every one of which was already in the round-1 six (the sixth,
security-perf, was dropped, not replaced). **No lens was rotated in.** Both
later-round catches therefore had to land on `core` by construction:
invariant-state found the module-granularity gap in the delegated-connect guard
(#1726) and test-evidence found the untested failure-isolation invariant
(#1725). A round that applies no treatment contributes a denominator and a
`core` numerator while saying nothing about whether rotation buys recall.

This is a fourth shape the ADR must separate from a genuine rotation
experiment, alongside pinned-core depth loops, fix-verification-only rounds, and
review-exempt ops rollouts: a **narrowed** follow-up round, where the round-2
mix is a subset of round 1 chosen to re-verify the specific fix surface. The
narrowing was the right call for this PR — round 1's failure class was a
structural blind spot in the guard, so re-running the lens that found it was the
point — but it means the line should be read as "rotation not applied", not as
"rotation underperformed".

Aggregate attribution remains **66 core vs 264 rotated**, a 4:1 concentration in
rotated-in lenses. **Keep rotation** remains unchanged; any reversal still
requires the recorded schema and round-role fixes plus maintainer review.

An instrumentation note this line makes concrete: `round_lenses` records which
lenses ran, but nothing in the log distinguishes "round N rotated a new lens in"
from "round N re-ran a subset of round 1". Until it does, `core` counts will
drift upward on narrowed rounds for reasons unrelated to rotation's value. That
is a measurement gap, not a decision input.

## Revisit 2026-08-22 (after #1701 step 2 / PR #1727)

`loop_log_audit` still returns DECIDABLE at **107 multi-round merged PRs, later-round
catches core=66 vs rotated=264** — byte-identical to the previous revisit's aggregate,
because this line is **single-round** (`rounds: 1`) and therefore contributes a merged PR
to the denominator of neither counter. A one-round PR has no later rounds; it cannot test
rotation in either direction.

Worth recording anyway, because it is a *fifth* shape distinct from the four already
separated above: a round-1-clean PR whose only net catch was fixed **without buying a
second round**. The verifier returned 3 CONFIRMED/DISCARD + 1 PLAUSIBLE/FIX_NOW; the
FIX_NOW was a spec-governance item (delta filed under a capability that does not govern
the changed code) repaired as an orchestrator-owned spec-only `local-repair`, so the clean
baseline SHA legitimately trails the final head. `gate_net_catch: 1` with `rounds: 1` is
the correct encoding and should not be read as a rotation datum.

It also sharpens the instrumentation gap noted in the previous revisit. That gap was
"`round_lenses` cannot distinguish rotated-in from narrowed re-runs". This line adds:
the schema also cannot distinguish "no later round was needed" from "no later round was
run", even though only the former is evidence about review sufficiency. Both remain
measurement gaps, not decision inputs.

**Keep rotation** unchanged.

## Revisit 2026-08-22 (after #1702 item 4 / PR #1731)

Docs-only PR, `rounds: 0` — no cross-review loop ran, so it enters neither the
multi-round denominator nor either catch counter. Aggregate stays **107 multi-round
merged PRs, core=66 vs rotated=264**. **Keep rotation** unchanged.

Recorded rather than skipped because the audit re-raises DECIDABLE on every line, and a
silent skip is indistinguishable from an overlooked obligation. The `rounds: 0` /
review-exempt shape is already separated above; this line adds no new information about
rotation and is logged only to keep the per-merge revisit chain unbroken.

## Revisit 2026-08-22 (after #1717 / PR #1730)

`rounds: 1`, clean on the first round, so this line contributes to neither
later-round catch counter. Aggregate is unchanged at **107 multi-round merged
PRs, core=66 vs rotated=264**. **Keep rotation** unchanged.

One observation worth logging, because it is the same measurement gap seen from
a new angle. Both round-1 lenses here were *core* lenses (oracle-integrity and
correctness/portability/spec-fidelity), chosen because the PR's literal shape
was "a test red on the production oracle is edited until it is green" — a shape
where the governing risk is knowable in advance. The round returned clean, and
the two P3 notes it did produce came from the core lenses, not from anything a
rotated-in lens would have covered.

That is a case where **not rotating was the right call**, and the schema cannot
record it: `round_lenses` shows which lenses ran, but nothing distinguishes
"core-only because the risk was known and narrow" from "core-only by default".
The previous two revisits logged that the schema cannot tell rotated-in from
narrowed re-runs, and cannot tell "no later round needed" from "no later round
run". This adds a third: it cannot tell a deliberate core-only selection from an
unconsidered one. All three are measurement gaps, not decision inputs; none of
them moves the keep/cut call, which stays on the 66-vs-264 aggregate.

## Revisit 2026-08-22 (after #1472 / PR #1722)

`rounds: 1`, clean on the first comprehensive round, with zero candidate findings
and therefore zero verifier batches. This line enters neither the multi-round
denominator nor either later-round catch counter. Aggregate remains **107
multi-round merged PRs, core=66 vs rotated=264**. **Keep rotation** unchanged.

The operational wrinkle is reviewer availability, not lens attribution: the
original Integration and Test & Evidence invocations produced no text report,
so fresh replacements ran the same two lenses on the same SHA and returned
clean. Those replacements complete Round 1; they are not a later round and must
not be interpreted as rotation. The accountability schema records the effective
lens mix once, correctly. This adds no keep/cut evidence, but keeps tooling
failure separate from the experiment being measured.

### Revisit 2026-08-22 — PR #1738 (#1735, lineage-scoped cycle completion)

`rounds: 3`, fixture `expanded`, merged. Enters the multi-round denominator:
**108 multi-round merged PRs**. `loop_log_audit` reports later-round catch
counters unchanged at core=66 vs rotated=264 — the two round-2 actionable
catches (P1 selector-lane coverage from the rotated-in `ci-blast-radius` lens;
P2 spec-contract drift from the round-2 correctness verifier) did not match the
audit's core/rotated lens labels and so are attributed to neither bucket.
Qualitatively both support keep: the P1 was invisible to every round-1 lens and
only surfaced once the CI-blast-radius lens rotated in. Round 3 (four rotated
lenses incl. mutation testing) returned zero actionable. **Keep rotation**
unchanged. Process note recorded in the loop-log line: mutation-testing lenses
must run in an isolated worktree.

### Revisit 2026-08-22 — PR #1746 (#1707, topology mirror fallback)

`rounds: 3`, fixture `compact`, merged. Enters the multi-round denominator:
**109 multi-round merged PRs**. Later-round catch counters unchanged at
**core=66 vs rotated=264** — the round-2 and round-3 catches carry lens labels
(`detector-recall-invariant`, `acceptance-closure-fixture-truthfulness`,
`predicate-correctness`, `final-state-evidence-closure`) that match neither
bucket, so they land in neither counter. **Keep rotation** unchanged.

Qualitatively this is the strongest keep evidence so far on a `compact` fixture.
Round 1 ran the core mix (oracle integrity, correctness) and produced one P1.
Every later finding — two P1s, two P2s, seven stale citations — came from a
rotated-in lens, and none of them was reachable from the round-1 mix: the
recall-invariant lens exists to enumerate a predicate's input space, and the
final-state-evidence lens exists to re-measure receipts written at earlier SHAs.
On a fixture level whose whole premise is "small surface, few lenses needed",
rotation still paid three rounds running.

Two things worth recording that the schema does not capture:

**Rotation surfaced a decision, not just defects.** The round-2 recall lens did
not merely find missing tokens; running it twice established that the failure
class was *unbounded*, which is what licensed D10 — declaring the boundary
instead of buying a fourth round. A lens whose value was "prove that more
rounds of this lens will not converge" is not a catch in the counter's sense,
and the counter cannot see it.

**Recorded decisions cap a rotated lens's yield, deliberately.** The round-3
reviewer was briefed that D10 closed the vocabulary axis, and it declined to
report findings of that shape — as did the round-2 reviewer against D9. So a
later round's catch count is partly a function of how much the fixture has
already decided, not only of which lens ran. That biases the rotated-in counter
*downward* over a PR's life, which if anything strengthens the keep call; but it
means the 66-vs-264 aggregate should not be read as a clean lens-vs-lens
experiment. Fourth measurement gap, alongside the three already logged.

Neither observation moves the keep/cut call, which stays on the aggregate.

### Revisit 2026-08-22 — PR #1750 (#1646, pytest thread-exception policy)

`loop_log_audit` reports **109 multi-round merged PRs**, with later-round catches
**core=66 vs rotated=264**. PR #1750 itself is a single-round clean review:
six Round 1 lenses returned zero candidate findings, no verifier batch existed,
and the independent Phase 7 Gap Sweep was also clean. It therefore enters
neither the multi-round denominator nor either later-round catch counter.

This sample has **zero information about rotation**: no follow-up comprehensive
round ran, so neither a rotated free slot nor a pinned-core recheck was applied.
Treating the extra merged-PR/log denominator as evidence for keep or cut would
repeat the measurement error already documented throughout this ADR—confusing
"treatment not applied" with "treatment applied and found nothing."

**Keep rotation remains unchanged.** The cumulative ratio still rules out an
autonomous cut, but this PR adds no evidentiary strength in either direction.
Any future reversal still requires the recorded attribution-schema and
round-role fixes plus maintainer review.

## Revisit 2026-08-22 — PR #1751 (#1736)

Audit at 445 lines / 441 merged: 110 multi-round merged PRs, later-round catches
`core=66 rotated=264`. The ratio is unchanged in direction from the previous
revisit (108 multi-round); **keep**.

Unlike the previous revisit, this PR **did** apply a rotated later-round lens and
it **did** catch. Round 2 was a single targeted `spec-conformance-targeted` lens
aimed at the round-1 fix, not a rebroadcast of the round-1 mix. It caught a P2
the round-1 lenses structurally could not have: the round-1 fix itself replaced a
wrong-but-unambiguous line-range citation with a prose locator that resolves to
four gates instead of three. That defect did not exist during round 1 — it was
created by the fix — so only a later round looking at the fix could see it.

This is the cleanest instance so far of the mechanism the ADR claims: rotated
later-round lenses find defects **introduced by the fix pass**, a class the
round-1 mix cannot cover by construction because the class does not yet exist
when round 1 runs. Recorded as supporting evidence, not as a change of ruling.

Caveat carried forward unchanged: the attribution schema still cannot separate
"rotated lens found it" from "lens ran later," and this PR is a single
observation. The keep ruling continues to rest on the cumulative ratio plus the
absence of a recorded cut rationale; any future reversal still requires the
attribution-schema and round-role fixes plus maintainer review.

## Revisit 2026-08-22 — PR #1759 (#1734)

Audit at 446 lines / 442 merged: 111 multi-round merged PRs, later-round catches
`core=68 rotated=264`. **Keep** — unchanged in direction.

This revisit is the first to weaken, rather than strengthen, confidence in the
number the keep ruling rests on.

**An attribution schema gap was found while filing this line.**
`loop_log_audit.rotation_attribution` reads `catch["round"]` and `catch["lens"]`.
A catch object written without them hits `catch.get("round", 1)`, defaults to
round 1, and is **silently skipped by the counter** — `continue`, no warning.

**Correction, 2026-08-22 (same day).** The first version of this revisit claimed
the ratio was "computed from an unknown subset" and that "most earlier lines do
not" carry the keys. **Both statements were false, and they were asserted from
two data points without measuring the log.** Measured over all 446 lines at
`97f8116a`:

| | count |
|---|---|
| catch objects total | 1331 |
| carrying `round`+`lens` (counted) | **1314 (98.7%)** |
| carrying `phase` only (skipped) | **17 (1.3%)** |

The 17 sit in exactly **four** entries — log lines 440, 442, 443, 445 =
PRs #1730, #1738, #1746, #1751. #1730 is `rounds=1` and never entered the numerator,
so the upper bound on later-round catches lost to this is **14 of 332 (≤4.2%)**
across three multi-round PRs. `phase`-only is a **recent write regression**, not
a historical baseline: `round`/`lens` has been written continuously since
PR #1126, and `references/phase-flow.md:566` already specifies `{"round":<n>,
"lens":...}` as the canonical shape — those four entries violate an existing
convention rather than reveal a missing one.

So the ratio is **not** materially undermined, and the keep/cut direction is
untouched. What the gap does show is an enforcement hole worth its own fix:
`loop_log_audit.py:63-75` discards non-conforming catches **silently**, and
`evidence_check.py:74-110` validates only entry-level keys and never descends
into `catches` — which is how the drift ran unnoticed for four entries. Tracked
as **#1764** (report, don't fix: the scripts live in the shared
`subagent-workflow` skill, not project code).

One concrete contradiction does survive the correction, unchanged: the
**2026-08-22 revisit for PR #1751** above narrates a rotated later-round lens
that caught a P2 and calls it "the cleanest instance so far of the mechanism the
ADR claims" — that entry is log line 445, `phase`-only, so the counter never
counted it. The narrative and the number disagree on the single instance this
ADR leans on hardest.

**Attribution for this PR is `core=2, rotated=0`**, and the reason matters more
than the count. Both later-round catches came from lenses already present in the
round-1 mix — `test-oracle-integrity` in round 2, `spec-conformance` in round 3.
They were invisible to round 1 not because a lens rotated in, but because **the
defects did not exist yet**: the round-2 coverage gap and the round-3 false spec
clause were each introduced by the preceding fix pass.

That is evidence for **later rounds** earning their keep. It is not evidence for
**lens rotation** earning its keep. The two have been conflated throughout this
ADR by an attribution schema that cannot separate "a rotated lens found it" from
"a lens ran after the defect was created" — a caveat every prior revisit has
carried forward verbatim, and which this PR now shows is not merely theoretical:
under a correct-key reading, the strongest instance recorded so far attributes to
core, not rotated.

**Ruling: keep.** The ratio survives the measurement correction — at most 4.2%
of later-round catches are uncounted, and the direction is unchanged — so keep
continues to follow from the cumulative ratio plus the absence of a recorded cut
rationale, as in every prior revisit.

What this revisit *does* narrow is a different thing, and it is not about sample
size: the ratio counts **when** a lens ran, not **whether rotating it in** is
what found the defect. This PR is a clean demonstration — `core=2, rotated=0`,
both from lenses already in the round-1 mix, both catching defects that round 1
could not have seen because the fix passes had not yet created them. Every prior
revisit carried the "cannot separate rotated-in from ran-later" caveat forward
verbatim as a theoretical limitation; here it is the whole explanation of the
result. Until the attribution schema can tell the two apart, the ratio supports
"run later rounds", and only ambiguously supports "rotate the lenses".

Reported, not fixed (**#1764**): the enforcement hole that let four entries drift
to `phase`-only keys. `loop_log_audit.py` and `evidence_check.py` live in the
shared `subagent-workflow` skill, not in project code, so the fix lands outside
this repo while the affected log and this ADR are project-local — that split is
part of why the drift went unnoticed. Any future reversal continues to require
the attribution-schema and round-role fixes plus maintainer review.

## Revisit 2026-08-23 (PR #1754, issues #1640 + #1654) — keep, and the sample moved by nothing

`loop_log_audit --log docs/review-loop-log.jsonl` returns DECIDABLE at 111
multi-round merged PRs, later-round catches **core=68 / rotated=264**. The
previous revisit (PR #1746) read 109 PRs at core=66 / rotated=264.

The rotated count did not move, and the core count moved by exactly this PR's
two catches. That is not a signal about lens rotation — it is arithmetic about a
PR that had **one** round. A compact fixture that goes clean in round 1 has no
later rounds, so it contributes to the numerator of neither bucket and only
enlarges the denominator's neighbourhood. Reading the ratio as having "shifted
toward core" would be wrong.

Decision unchanged: **keep** the rotation.

Recorded because it is a fifth way this counter can mislead, alongside the four
already listed above: single-round PRs contribute round-1 catches to the `core`
tally while contributing no opportunity for a rotated lens to catch anything. The
counter's denominator is multi-round PRs but its `core` numerator admits catches
from PRs where rotation was never exercised. Anyone using this ratio to argue
cut should first filter to PRs that actually reached round 2.

## Revisit 2026-08-23 (PR #1773, issue #1743) — keep; a zero-round line cannot move this counter

`loop_log_audit --log docs/review-loop-log.jsonl` returns DECIDABLE at 111
multi-round merged PRs, later-round catches **core=68 / rotated=264** —
identical to the previous revisit (PR #1754) in all three numbers.

That identity is the whole content of this revisit. PR #1773 is a `fixture:
none`, `rounds: 0` line: no cross-review round ran at all, so it contributes to
neither numerator and does not even enter the multi-round denominator. The
counter is unchanged because nothing about it was exercised.

Decision unchanged: **keep** the rotation.

Worth recording alongside the five ways this counter can already mislead: the
audit emits DECIDABLE on every merge once the sample thresholds are met,
*including* merges that carry zero review evidence in either direction. The
obligation to record a keep/cut call therefore fires on lines that are, by
construction, incapable of informing it. That is not an argument to weaken the
obligation — a cheap recorded "unchanged, and here is why it could not change"
is exactly what keeps the ledger honest — but a reader scanning revisit headings
should not mistake the *number* of revisits for the amount of evidence
accumulated. The prior revisit made the adjacent point about single-round PRs
inflating `core`; this one makes the stronger version: zero-round PRs inflate the
revisit count itself.

## Revisit 2026-08-23 (PR #1771, issue #1669) — keep, counter did not move

`loop_log_audit` returns DECIDABLE at **111 multi-round merged PRs, core=68 /
rotated=264** — identical to the PR #1754 revisit above. This PR contributed
nothing to either bucket: one round, and its single catch is a round-1 catch, so
it lands in neither later-round tally.

That is the fifth caveat from the #1754 revisit playing out exactly as described:
single-round PRs enlarge the population without exercising rotation. Decision
unchanged: **keep**.

One observation worth recording anyway, because it is about where catches come
from rather than which lens found them. This PR's most consequential correction
did not come from a lens at all — it came from the **user** challenging the
premise ("production already has too many indexes"), which forced a measurement
that reversed the approach from rebuilding an index to deleting it. The
rotation counter cannot see that, and a reader using this ADR to reason about
where review value originates should know the instrument only counts one of the
sources.

## Revisit 2026-08-23 (PR #1783, issue #1747) — keep, denominator +1, numerators unchanged

`loop_log_audit` returns DECIDABLE at **112 multi-round merged PRs, core=68 /
rotated=264**. The denominator moved by one; both numerators are identical to
the PR #1771 revisit above. Decision unchanged: **keep**.

This PR is the first revisit that adds a *sixth* way the counter misleads, and
it is the one that most directly attacks the instrument's premise. PR #1783 did
rotate — round 2 pulled in `spec-compliance`, a lens not used in round 1 — and
the rotated lens returned zero findings. On the ledger that is indistinguishable
from a PR where rotation was never exercised: both contribute nothing to
`rotated`.

But the two are not the same thing, and here the difference is total. Round 2
reviewed a **byte-identical code head**: round 1's single verified finding was
an orchestrator-authored false claim living only in the PR body, so the
corrective action was a prose edit and the head SHA was unchanged across
round 1, round 2, and Phase 7. A rotated-in lens finding nothing in code that
did not change is not evidence that rotation buys nothing; it is not evidence
about rotation at all. The counter cannot distinguish "the new lens looked and
the code was clean" from "the new lens looked at code no one had touched."

The practical consequence for anyone reading this ledger to decide keep/cut:
`rotated=264` is a floor on rotation's value, not an estimate of it, and the
gap between floor and truth widens every time a round runs against an unchanged
head. The four prior caveats plus this one all push the same direction — the
instrument systematically undercounts rotation and overcounts core — which is
why the default-keep has never been close to a hard call.

Recorded under the run's autonomous default-keep rule; keep/cut remains a human
call and this is the recorded default pending maintainer override.

## Revisit 2026-08-23 (PR #1780, issue #1775) — keep, and the first caveat that cuts the other way

`loop_log_audit` returns DECIDABLE at **113 multi-round merged PRs, core=68 /
rotated=265**. This PR moved the denominator by one and `rotated` by one; `core`
is unchanged. Decision unchanged: **keep**.

On its face this is the cleanest pro-rotation line in the ledger so far. Round 1
ran `relaxation-correctness`, `test-oracle-integrity` and
`root-cause-blast-radius` and returned one P1 (a gate-parity divergence between
`scheduler_candidates.py`'s terminal-skip exit and `_cycle_completion_verdict`).
Round 2 rotated in `allowlist-tightening-reachability` and
`gate-parity-and-test-honesty`, and returned a second P1 — a *second instance of
the same class*, in a different code path. Round 3 went comprehensive and
returned clean. A rotated-in lens found a P1 that the round-1 mix had not.

**But that is not blind rotation, and counting it as such flatters the
instrument.** The round-2 lens was rotated in *because* round 1 had named the
class. It was targeted re-verification of a known failure mode, not exploration
of an unexamined axis. It found what it was pointed at.

That is the seventh caveat, and it is the first one that pushes *against*
rotation rather than for it. The six recorded above all argue that `rotated` is
a floor — that the counter systematically undercounts rotation's value. This one
argues that some fraction of `rotated` is not rotation at all but follow-up
targeting, which the ledger cannot distinguish from a lens rotated in cold. The
honest statement is now two-sided: `rotated=265` is neither a floor nor an
estimate, because it mixes two mechanisms with different value profiles. Both
are worth doing; only one of them is what "lens rotation" names.

The practical read is unchanged — 68 vs 265 is a wide enough margin to survive
the correction in either direction — but a future maintainer deciding keep/cut on
this counter should know it is measuring a union, and that separating the two
would require recording *why* each lens entered a round, which no line currently
does.

One further observation, and it is the same shape as the PR #1771 revisit's
point about the user challenge. This PR's most consequential error was not in
the diff at all: the orchestrator's premise that production backfill was
permanently wedged and would never self-heal was false, and it was written into
the issue body and into every reviewer brief before any lens ran. Three
comprehensive rounds, a verifier gate and a Phase 7 review all executed and all
found real implementation defects; none questioned the premise, because a
premise baked into the brief is upstream of every lens and is not part of what
any of them review. No rotation policy reaches it. That is the second recorded
instance of the ledger's blind spot being *where the review question came from*
rather than *which lens asked it*, and it is a stronger argument for keeping the
count honest than for changing it.

Recorded under the run's autonomous default-keep rule; keep/cut remains a human
call and this is the recorded default pending maintainer override.

## Revisit 2026-08-23 (PR #1784, issue #1781) — keep, and the counter's third blind spot: the catch that came from neither lens

Counter moved `core=68`, `rotated=265 → 267`; 114 multi-round merged PRs. The
margin is untouched and the default-keep stands. What this run adds is not a
number but a category the ledger has no column for.

Three comprehensive rounds ran. The code-facing lens — the pinned core — returned
**zero findings in rounds 2 and 3**, having been correct both times: an
independent verifier separately confirmed the fix round was complete, and the
final review re-derived the same conclusion. Every net catch in those two rounds
came from a rotated-in lens, and every one of them was the same class: an
artifact contradicting the code. A deployment instruction that the PR's own fix
round had falsified. A Must-preserve paragraph describing a placement the design
had already reversed. A spec scenario left un-revisited when its replacement was
added beside it, so that one file both authorized and forbade the same behaviour.
On the counter's own terms this is another clean win for rotation.

But the most consequential finding of the run came from **neither**. It came from
running the change on node-27. The first live tick declined 144 of 158 blocked
runs and left 14 pinned at `failed` with `rc=1` forever, because those runs had
no init-state evidence and the design's fail-closed rule turned "key unobtainable"
into retry-forever — reproducing, for that subset, precisely the loop the change
existed to eliminate. It was as-designed and it was not as-promised, and it would
have shipped as a silent descope of the outcome the user had explicitly chosen.
No lens found it, in three rounds, because nothing in the diff is wrong: the
defect lives in the interaction between a deliberate design rule and a data shape
that only production exhibits (cold-start and packaged-IC runs write a null
`state_id`, so the condition is ongoing rather than historical).

That is the third recorded instance of this ledger's blind spot being structural
rather than allocative. The first two were about *where the review question came
from* — a premise baked into the brief, upstream of every lens. This one is about
*what a review is made of*: reading a diff cannot discover a data shape the diff
does not contain. Rotation policy does not reach it; neither does buying another
round. The marginal review round in this PR found stale prose. The marginal
**deployment** found a functional gap.

The practical implication is a sequencing one and does not change keep/cut: for
changes whose acceptance is a production behaviour, live evidence is not the last
box to tick after review converges — it is a distinct detector that should be
allowed to run *before* the review budget is exhausted, because it answers a
question no lens is capable of asking. Two method notes from this run belong with
that claim, since both nearly produced a wrong live verdict: a checkout does not
swap code in an already-running process, so tick attribution must key on a
structural marker rather than on counting ticks; and acceptance assertions must
never pin absolute counts when the population under test is growing, or they go
red while the change works.

Recorded under the run's autonomous default-keep rule; keep/cut remains a human
call and this is the recorded default pending maintainer override.

## Revisit 2026-08-23 (PR #1786, issue #1671) — keep

Denominator 115 multi-round merged PRs; later-round catches core=68, rotated=270.
Decision unchanged: **keep**.

This run is an unusually clean data point for rotation, because round 2 used two
lenses that round 1 did not have (`gap-sweep`, `evidence-claim-verification`) and
both earned their slot:

- `gap-sweep` found that T11/T12 were ticked with no pasted evidence — the
  *identical* defect class that round 1 had already found in T10/T10b and that I
  had just "fixed". The round-1 mix could not have found it: those same six
  lenses had passed over the file and stopped at the two items they named.
- `evidence-claim-verification` found that the sleep-counting instrument used to
  produce the round-2 evidence is evadable by a test that monkeypatches
  `time.sleep`, and that a live example was sitting in the measured set. That is
  a lens aimed at *the evidence added by the previous round*, which by
  construction does not exist at round 1.

Both are cases where the rotated-in lens was not merely a different reader of the
same artifact but the only reader of an artifact that round 1 predates. That is
the mechanism the `rotated` counter is supposed to be measuring, and here it is
visible rather than inferred.

The caveat recorded in the previous revisit still stands and is not weakened: a
rotated-in lens that reviews a byte-identical head contributes nothing to
`rotated` yet is not evidence about rotation either way. Nothing in this run
changes the fact that the counter cannot distinguish those cases on its own.

## Revisit 2026-08-23 (PR #1788, issue #1734) — keep, with three counted `rotated` that are not evidence

Denominator 116 multi-round merged PRs; later-round catches core=68, rotated=273.
Decision unchanged: **keep**. But this run's three-point contribution to
`rotated` must not be read as support for the decision, for two independent
reasons, and it is recorded here so a later reader does not count it twice.

**First: the attribution is empty, not rotated.** Reviewer lens names were never
persisted for this PR — `.workplans/pr-1788/review/` holds only
`round-ledger.log`, no verdict tables — so its loop-log line carries
`round_lenses: [[], []]` and `lens: "unrecorded"` on every catch. Because
`rotation_attribution` classifies by `catch["lens"] in core_lenses` and the core
set is empty, all three round-2 catches fall to the `else` branch and are
counted as rotated. The script has no *unattributable* state; the honest reading
of this line is core=0, rotated=0, unknown=3. **Subtract 3 from `rotated`
before using this counter as evidence.** This is a persistence failure of mine,
not a property of the review, and it compounds the schema gap already reported
on the PR #1759 line (catches written with `phase` only default to round 1 and
are skipped entirely).

**Second, and more interesting: even with perfect lens records these three would
not have been evidence about rotation.** All three round-2 findings were defects
in code that *round 1's own fix pass had just written* — a pin that spanned one
of two decorated classes, a missing pin on the `resource_limit_blocked` evidence
path, and a 5% threshold where the true value is a deterministic zero. Round 1
could not have caught them under any lens mix, because at round 1 they did not
exist. This is exactly the conflation flagged on the #1759 line and it has now
recurred: **later rounds earning their keep is a different claim from lens
rotation earning its keep, and the counter cannot separate them.** The #1786
revisit above is the clean case precisely because its rotated-in lenses read an
artifact round 1 predates *and were named*; this run is the muddy case.

The standing caveat compounds rather than resolves: the counter over-credits
rotation both when a rotated-in lens reviews a head round 1 never saw (fix-pass
defects, this run) and whenever lens records are missing (also this run). Its
one-directional bias is now documented twice. Keep remains the recorded default,
but the next revisit should be made against lines with **recorded** lens names
only — and the first corrective step is mechanical: persist the verdict tables
and reviewer list at each round, which the pre-merge evidence gate already
nominally requires.

## Addendum 2026-08-23 (PR #1777, issue #1686 — abandoned) — the optimization premise, and a third user challenge

The revisit above named a blind spot and gave it one instance. Later the same day
a second, sharper one arrived, and it is worth appending rather than leaving the
claim resting on a single case.

PR #1777 was finished. Its real-DB oracle was green — 64 passed, 0 skipped on
node-27 — and its measured benefit was *larger* than the issue had claimed: both
compressed backing tables moved from `Seq Scan` to `Index Cond`, `Seq Scan` 5→2.
Nothing in the diff was wrong. It was closed unmerged anyway, because gathering
the last acceptance measurement forced a look at what the optimized query is for,
and two cheap facts settled it: the join's existence half is idle in production
(status mix published 3174 / superseded 959 / succeeded 140 / **parsed 0**, and
the `HAVING` arm is unconditionally true for `published`), and its only
load-bearing output — `MAX(rt.created_at) AS parsed_at` — exists solely because
`hydro.hydro_run` has no parse timestamp column. The query aggregates a
compressed hypertable to derive one timestamp per run, and an `EXPLAIN (ANALYZE)`
on a 50-run sample would not finish inside 180 s. The correct change deletes the
join; it was refiled as #1789.

The generalizable rule is narrower than "question your premises", and it is the
reason this belongs in a lens-rotation ledger rather than a postmortem:

> **An optimization issue carries an implicit premise — that the work being made
> faster needs doing at all — and that premise sits upstream of every reviewer
> lens.** A lens verifies the optimization is correct, safe, and measured. None
> is chartered to ask whether the thing should exist. No rotation policy reaches
> it, and buying another round cannot find it.

That is now the third recorded instance of this ledger's blind spot being
structural rather than allocative, and the second triggered by a **user
challenge** rather than by any lens (the first being #1771's, recorded above).
Three instances, three different upstream positions: a premise baked into the
brief, a data shape that only production exhibits, and now the value question an
optimization never asks about itself. The counter is measuring lens allocation
faithfully; what keeps escaping it is not allocation.

One practical consequence, cheap enough to adopt without ceremony: the evidence
that decided this was a status-mix count and a column list — seconds of work,
available on day one. The reason it was gathered at all is that the acceptance
criteria demanded execution-level measurement (`EXPLAIN (ANALYZE)`) rather than a
plan comparison. Plan-only evidence would have confirmed the optimization and
never raised the question. Where an issue's whole value rests on a cost claim,
requiring the measurement to be *executed* is doing double duty as a premise
check.

Recorded mistake from the same run, kept because this file is also where method
errors go: an unattended `EXPLAIN (ANALYZE)` with a 900 s bound was left running
against production for ten minutes and was cancelled only on the user's
prompting. The bound was set for "let it finish" rather than for "this is
unsupervised on a live database", which are different numbers.

Keep/cut unchanged; still the recorded default-keep pending maintainer override.

## Revisit (2026-08-23, issue #1749 / PR #1793)

Audit re-flagged DECIDABLE at the larger sample: **117 multi-round merged PRs,
later-round catches core=68 rotated=283**. Keep/cut unchanged — the ratio has
only strengthened.

**First, the metric was checked before being believed**, because issue #1764
records a defect in exactly this counter: an entry whose round-1 `core_lenses`
is empty makes *all* its later-round catches count as `rotated`. Measured on
this ledger: 29 of 128 multi-round merged entries (23%) have an empty round-1
lens list, but those entries contribute only **3** later-round catches, against
274 from entries that do record round-1 lenses. The defect inflates `rotated` by
about 1% at this sample size — real, worth fixing, and nowhere near enough to
overturn a 283/68 split. #1764 could not be used to dismiss this DECIDABLE, and
was not.

**The fourth structural instance, and the first where the escape route was a
document rather than a code path.** PR #1793's production change was three
deleted lines. Five rounds produced twelve verified findings and **not one was
in the code** — two independent reviewers confirmed the code, one with a live
mutation bite-proof. Every finding was in orchestrator-authored prose: OpenSpec
proposals, designs, task records, deviation records, the PR body.

Rotation worked exactly as this ADR claims. Round 4's rotated-in
`closed-list-claim-audit` lens caught six falsehoods that three prior rounds and
a targeted grep sweep had all walked past. That is the keep criterion, met again.

But the shape of the failure is the part worth recording. The recurring defect
was "a document asserts something no longer true", and it recurred **five times
in one PR** — including once by the Phase 7 *class sweep* written to end it, and
once by the *correction commit* written to end that. The sweep failed for the
same reason as the point-fixes it was correcting: it grepped the vocabulary its
author already suspected (`archive`, `archived`, `on this branch`) while the
surviving falsehoods were phrased in premise vocabulary (`every inflight
cohort`, `a shape production never writes`). A grep is only as wide as the
suspicion behind it.

So the escaping thing was again not allocation — but unlike the prior three
instances it was not upstream of the lenses either. It was **inside the
orchestrator's own output, on a surface no reviewer is chartered to enumerate
exhaustively.** Reviewers sample prose for plausibility; nobody was tasked to
enumerate every claim and rule on each. The closed-list audit is that task, and
this is the second time it has closed a class no lens closed (the first was
issue #1759).

Two rules adopted from this run, both cheap:

1. When a finding is "document X asserts something untrue", the fix is **neither
   an edit to X nor a grep for X's wording** — it is a closed-list pass over
   every claim-bearing document in the change set, PR body and test comments
   included, with a recorded ruling per claim. The claim you miss is by
   construction phrased in words you did not think to search for.
2. **A citation is a claim.** `file:line` is verified by opening the file. This
   run mechanically extracted all 43 citations across both changes and printed
   the line each actually points at; seven were wrong, one substantively — the
   load-bearing evidence for a deviation record cited the *adjacent* error
   guard.

A boundary this run also had to settle, recorded here because it will recur:
**prose documents must be true at head or carry an in-place correction; a spec
delta need only be true of the specs tree at its landing SHA.** `openspec
archive` folds a delta verbatim, and the mechanism for superseding a landed
clause is a later `MODIFIED` requirement — rewriting a completed change's delta
to match a later head would falsify its own record of what it shipped. The same
reasoning governs line-number drift in a completed change's prose: disclose it,
do not renumber it.

Recorded method errors from this run, kept per this file's convention: (a) round
4 was initially recorded against the post-fix SHA rather than the SHA the
reviewers actually ran on, which would have let an unreviewed head inherit a
clean baseline — self-caught and corrected, and the Phase 7 final review was
consequently still run on the final head; (b) `review_gate.py record-round
--clean` silently zeroes the finding counts and failure classes, so "found eight
and fixed eight" is indistinguishable in the ledger from "found nothing", and
the dropped classes blind cross-round repeat detection — filed as #1794.

Keep/cut unchanged; still the recorded default-keep pending maintainer override.

## Revisit at #1789 merge (PR #1791, 2026-08-23)

`loop_log_audit` again returns DECIDABLE lens-rotation: 119 multi-round merged
PRs, later-round catches core=68 / rotated=285. **Keep**, unchanged, and this
run is a clean confirming instance: of four review passes (two lenses in round
1, a Phase 7 final, a Phase 7 delta final), the `production-loop-safety +
state-machine-correctness` lens returned zero and every net review catch came
from the rotated-in `db-migration + data-loss-and-staleness +
artifact-consistency` lens — the same distribution as #1781.

Two things this run adds that the rotation ledger does not model, both worth
recording because the numbers above will keep looking healthy while they recur:

**(1) The highest-value catch was upstream of every lens.** It landed before
implementation, from the advisor, against the *issue body* rather than the code: #1789
prescribed folding `parsed_at = now()` into `mark_run_parsed`'s existing
UPDATE, which is gated on `PARSE_READY_RUN_STATUSES` and therefore excludes
`published` — the very population recompute detection exists for. The issue
stated the requirement ("重新 parse 必须 bump 它") two lines above a remedy that
does not satisfy it. Nothing in the review track would have caught this, because
by the time reviewers see a diff the fixture has already chosen the mechanism.
ADR 0003 already records that an optimization issue's premise sits upstream of
every reviewer lens; this run extends it: **an implementation-ready issue's
prescribed remedy is itself an unverified claim, and deserves the same treatment
as code.** Verifying it required tracing the actual convergence mechanism —
fact-row `created_at` refreshed by a keyed DELETE + reinsert, not the status-
gated UPDATE — which is exactly the kind of thing that reads as settled
background.

**(2) Four review passes cannot see which oracles execute.** The real-DB run on
node-27 returned 4 failures in an `integration`-marked file whose seed helpers
never wrote the new column. Production code was correct; the seeds were stale.
Every review pass reasons over code and artifacts, and the local full suite
(13774 passed) was *structurally* blind because integration-marked tests skip
without env vars and the PR CI lane does not select them. This is the second
consecutive issue (#1781, #1789) where live deployment was the only oracle for
the decisive defect. The lesson is not "add a lens" — a lens still reads text.
It is that for any change altering a value that seeded fixtures also produce,
**the deployment step is load-bearing verification, not a receipt-taking
formality**, and its evidence floor must run the marked suites the PR lane skips.

Recorded method errors from this run: (a) the six-step deploy sequence was
corrected in `design.md` but not propagated to `proposal.md`, which continued to
state the superseded three-step form *while citing D5 as its authority* — caught
by the Phase 7 final review, i.e. the orchestrator's own fix contradicting a
sibling paragraph; (b) an integration suite was first launched over a foreground
ssh session, so the timeout that moved it to background killed the remote
pytest and produced an empty result that could have been misread as a pass —
re-run detached per the project's own `setsid nohup` discipline; (c) two
process-liveness checks used `pgrep -f <pattern>` from a shell whose own command
line contained the pattern, yielding false "still running" readings — use
`pgrep -f` with a bracketed pattern or check the PID directly.

Keep/cut unchanged; still the recorded default-keep pending maintainer override.

## Revisit (2026-08-23, issue #1185 / parent PR #1753 terminal split)

The appended #1753 line is a terminal `ceiling-split` record, not a merged
sample. With master's #1789 accountability already present, `loop_log_audit`
remains at **119 multi-round merged PRs, later-round catches core=68 rotated=285**.
The parent terminal line contributes neither a merged sample nor a catch. This
does not change the direction already adjudicated above, so the recorded **keep
rotation** decision stands unchanged.

This line does add a cost-boundary signal, but it belongs to the review-gate
sizing ledger rather than the lens-allocation ratio. PR #1753 reached Round 3
with one remaining P2 coverage finding on a behavior-neutral extraction that
had entered the PR only after a deterministic large-file hook fired. The second
gate selected a real breadth split: extraction compatibility moves to
predecessor issue #1799, and the cohort-identity state machine returns in a
successor PR for #1185 after that predecessor merges. The finding and
implementation are not copied into both children.

That is the gate doing the job this ADR does not: stopping an unrelated wrapper
compatibility proof from consuming more high-risk journal review rounds. It
neither supports nor weakens lens rotation, so no keep/cut policy change follows.

## Revisit (2026-08-23, issue #1799 / PR #1803)

The merged Child A line has one comprehensive round, so it does not enter the
multi-round rotation sample. `loop_log_audit` therefore remains at **119
multi-round merged PRs, later-round catches core=68 rotated=285**. The recorded
**keep rotation** decision is unchanged.

The useful signal from this child occurred before that accounting boundary. Its
fixture reviewer found that the first invariant inventory named four downstream
consumers while production had eight. The orchestrator's Phase 2 audit then
found that four import/runtime binding classes still lacked committed evidence,
and that the extracted module's local Protocol described `config` while its
window implementation actually called `discover_cycles`. Both defects were
closed before PR review. Round 1 then ran all six high-risk lenses and returned
zero candidates; the independent final sweep also returned zero.

That sequence is not evidence that rotation is wasted. It is evidence that lens
rotation is not a substitute for the earlier closed-list inventory and
orchestrator verification gates: by review time, those gates had already turned
the missing surfaces into durable tests. Conversely, counting either early catch
as a rotated-lens catch would overstate rotation's value. Keep/cut remains
unchanged; this single-round confirming instance moves neither numerator nor
denominator.

## Revisit (2026-08-23, issue #1748 / PR #1802)

`loop_log_audit` still reports **119 multi-round merged PRs, later-round catches
core=68 rotated=285**. Keep/cut is unchanged. But this time the number is
unchanged for a reason that is itself the finding, so it is recorded rather
than waved through.

PR #1802 is a four-round merged PR with seven catches, three of them in rounds
≥ 2 including one CRITICAL. On the stated criterion it is exactly the kind of
sample this ADR is short of. It contributes **nothing** — neither numerator nor
denominator — because `loop_log_audit.py:124` gates inclusion on
`e.get("round_lenses")`, and I never persisted per-round lens lists for this PR.
`.workplans/pr-1802/review/` holds the round ledger and the retro; it holds no
`round-N-lenses.txt`.

I did not reconstruct the lens lists after the fact. Rounds 1 and 2 ran before
the gate tripped and I have no durable record of which lenses were selected;
writing plausible names now would be precisely the failure this PR's own retro
was about — an assertion that was true when believed rather than when checked.
An omitted sample is recoverable; a fabricated attribution silently corrupts the
statistic this ADR exists to read.

**Consequence for the keep/cut question.** The sample is biased in a direction
that has gone unmeasured: lines are admitted only when the orchestrator
remembered to persist lens lists, and the rounds most likely to skip that
bookkeeping are the ones under gate pressure — which are also the rounds where
rotated-in lenses would be doing their most distinctive work. `core=68
rotated=285` may therefore *understate* rotation's value, but that is a
conjecture from one missing sample, not a measurement. Direction of the recorded
**keep rotation** decision is unchanged; the confidence attached to the 68/285
ratio should be lower than the sample size suggests.

**Mechanical gap.** `evidence_check --loop-log-entry` validated my line as clean
while it was silently unusable for the audit — it checks required keys, fixture
vocabulary, outcome vocabulary, and date, and `round_lenses` is in none of those
sets. This is the same shape as #1764 (`rotation_attribution` skipping catches
that lack `round`/`lens`), one level up: there the sub-keys are unchecked, here
the whole key is. Both belong to #1764, which is open and is the tracked owner;
no separate issue is filed.

**Method note from this run**, in the register of the (a)/(b)/(c) list above:
after the clean Phase 7 round, `origin/master` advanced and the branch went
`CONFLICTING`. Merging it moved cited lines in `tests/test_production_scheduler.py`
by +81 — the PR's own `design.md` D4c had recorded that re-anchoring "only works
if that head is final", and this is the first instance where the head moved for a
reason outside the PR entirely. The discipline held only because the re-anchor
was re-run after the merge rather than treated as done at push time.

## Revisit (2026-08-24, issue #1185 / PR #1808)

`loop_log_audit` now reports **120 multi-round merged PRs, later-round catches
core=68 / rotated=285**. PR #1808 adds one multi-round sample and **zero** to
either catch bucket: its only verified finding came from Round 1's pinned
`test-evidence` lens, and the post-fix Round 2 deliberately reused the pinned
subset `{correctness, test-evidence, invariant-state}`. The Round 2 manifest
explicitly bought no rotating free slot because Round 1 had no P0/P1 and no
repeated failure class. Round 2 and Phase 7 were clean.

This is therefore another **rotation-not-applied** sample, not evidence that
rotation did or did not buy recall. Counting the added denominator toward a
keep/cut effect would repeat the instrumentation error already recorded above:
the log records lens names and catches, but not whether rotation was applied as
a treatment. The aggregate direction remains strongly on the keep side, so
**keep rotation** remains the recorded decision; this PR adds no strength to
that conclusion and no policy change follows.

The useful process signal sits on a different axis. Local Phase 2 and CI were
green while the Round 1 `test-evidence` lens found that the job-limit oracle
left its authority row inside the bounded window, so a bounded-reader regression
could still pass. Independent verification confirmed the false green; the
strengthened oracle then made two independently replayed bounded-reader mutants
red, and the pinned Round 2 lens verified closure. That supports keeping a
pinned evidence lens plus independent adjudication. It says nothing about the
marginal value of a rotated free slot, because none ran.

## Revisit (2026-08-24, issue #1564 / PR #1755)

After appending PR #1755's validated accountability line, `loop_log_audit`
returns DECIDABLE at **121 multi-round merged PRs, later-round catches core=71 /
rotated=293**. Relative to the preceding sample, this five-round PR contributes
**3 core** and **8 rotated** later-round catches. The recorded human decision
remains **keep rotation**: the cumulative attribution still concentrates far more
catches in lenses absent from round 1, and this PR adds evidence in that same
direction rather than reversing it.

The three core catches were produced by exact round-1 lens tokens reused later:
`test-evidence` in rounds 2 and 3, and `spec-compliance` in round 4. The eight
rotated catches came from `correctness-state`, `spec-oracle`, and composite
integration/state labels introduced after fixes. They were load-bearing: the
round-2 old-ID routing defect, round-3 writer-authority / committed-reclaim /
safe-root closure, and round-4 canonical-spec contradiction all survived local
Phase 2 and CI.

There is an attribution caveat rather than a reason to cut. The audit compares
lens tokens literally, so a composite token such as
`correctness-state+integration-security` counts as rotated even when it contains
part of a round-1 perspective. Consequently the 8 should not be read as eight
purely novel disciplines. The defensible conclusion is narrower: retaining a
pinned core while allowing the follow-up mix to change scope continued to buy
union recall, especially across fix-created and newly merged contract surfaces.
That is exactly the current policy. No reviewer-set or rotation-policy change
follows from this revisit.

## Revisit (2026-08-24, issue #1816 / PR #1817)

After appending PR #1817's validated accountability line, `loop_log_audit`
returns DECIDABLE at **122 multi-round merged PRs, later-round catches core=71 /
rotated=295**. This three-round PR contributes **0 core** and **2 rotated**.
Recorded human decision: **keep rotation**, unchanged.

This is a clean data point rather than a marginal one, and it is worth recording
why. Round 1 ran two lenses — `deletion-completeness` and
`test-oracle-spec-conformance` — chosen because the change is a module deletion
sharing plumbing with a second repair that had to survive. Both lenses did their
job (one P2 coverage gap, one P3 refuted by measurement). Neither could have
found what round 2 found, because neither was pointed at prose: the round-2 lens
`prose-truth-class-sweep` caught a **false operator-facing claim in the runbook**
that the round-1 fix pass had itself left behind while correcting the identical
sentence in four OpenSpec documents.

That is the same failure class recorded in `.workplans/pr-1793/review/retro-round-3.md`
— a fix scoped to the file the finding named, with the assertion's *terms* never
swept across the whole change set — recurring on a different PR three days later.
Its recurrence here is evidence for two separate things, and they should not be
conflated:

1. **For rotation**: a lens absent from round 1 found a P1 that the round-1 mix
   was structurally blind to. Union recall, exactly as the standing decision
   predicts.
2. **Against reading this as a rotation success story**: the defect did not exist
   at round 1. It was *created* by the round-1 fix pass. Rotation caught damage
   that a properly class-scoped fix would not have produced. Counting it as
   evidence that rotation buys recall is therefore partly circular — the rotated
   lens is being credited for catching the loop's own miss.

The honest reading is that the counter's numerator is inflated by self-inflicted
findings of this shape, and the #1793 retro's `depth` diagnosis (fix prompts too
narrow) remains the unclosed root cause. No rotation-policy change follows. What
does follow, and is recorded here rather than as a new ADR: **a fix brief that
corrects a factual claim must name the claim's terms and require a whole-tree
sweep for them, not name the file.** On this PR the round-2 fix did exactly that
(`grep -rn` across the tree, one surviving copy found) and the final review
confirmed no further copies — so the corrective action is known and it works; it
is applying it at round 1 that keeps failing.

Second, smaller note on a non-catch: round 1's P3 was refuted by measurement
rather than fixed (node-22 has zero stale `repaired-basins-soil-alpha`
directories, so the narrowed cleanup tuple has no orphan to strand). Refutation
by measurement is recorded as `refuted` in the loop-log verdicts and does not
enter `gate_net_catch`. That is the correct treatment — a finding disproved
against production is not a caught defect — but it means the audit's catch
counters systematically undercount the review loop's value on findings that turn
out to be non-existent in production.

## Revisit (2026-08-24, issues #1604/#1605/#1606 / PR #1821)

After appending PR #1821's validated accountability line, `loop_log_audit`
returns DECIDABLE at **123 multi-round merged PRs, later-round catches core=72 /
rotated=295**. Relative to the preceding sample, this PR contributes **1 core**
and **0 rotated**. Recorded decision: **keep rotation**, unchanged.

This line does not test rotation. Round 2 deliberately reused the pinned
correctness/integration/test-evidence core to verify a P1 fix; no rotating free
slot ran. The sole later-round catch was a state-transition defect introduced by
the preceding submission-success repair: a new lock-outside durable reread could
strand a pending retry or submit externally before strict validation. The pinned
core found it, the same-invariant gate forced a depth retro, and the corrective
producer-result refactor removed the reread window. Round 3 and Final Gap Sweep
were clean.

That is evidence that **pinned later-round fix-regression recall** remains
load-bearing. It is not evidence that an applied rotation found nothing, because
rotation was not applied. The attribution caveats recorded above therefore remain
decisive: the raw 72/295 split mixes treatment, no-treatment, targeted rechecks,
and workflow-role lenses. The cumulative direction still rules out an autonomous
cut, while any future reversal still requires the recorded schema/round-role
repairs plus maintainer review. No reviewer-set or rotation-policy change follows.

## Revisit (2026-08-24, issue #1809 / PR #1824)

After appending PR #1824's validated accountability line, `loop_log_audit`
returns DECIDABLE at **124 multi-round merged PRs, later-round catches core=72 /
rotated=295**. Relative to the preceding sample, this PR contributes **0 core**
and **0 rotated** later-round catches. Recorded decision: **keep rotation**,
unchanged.

This line does not test rotation. Round 1's initial six-lens review found the one
net catch: a helper-only selector route omitted five assertion-bearing ultimate
consumers behind support-to-support and function-local imports. Round 2 used the
pinned integration, test-evidence, and invariant-state fix-regression core; it was
clean, as was the independent final gap sweep. With no later-round catch, this PR
adds no attribution evidence for either arm.

The cumulative 72/295 direction still supports keeping rotation, while the
standing attribution caveats remain decisive against treating one no-catch,
no-rotation PR as a cut signal. No reviewer-set or rotation-policy change
follows.

## Revisit (2026-08-24, issues #1832 / PR #1835 and #1825 / PR #1833)

After appending both accountability lines, `loop_log_audit` returns DECIDABLE at
**126 multi-round merged PRs, later-round catches core=74 / rotated=295**.
Relative to the preceding sample these two PRs contribute **2 core** and **0
rotated** later-round catches. Recorded decision: **keep rotation**, unchanged.

Both later-round catches came from the pinned core, and both from
`invariant-state` on PR #1833's round 2. That round is worth naming rather than
counting: round 1 fixed "an unverified artifact must not be left live on the
path the forecast stage reads", and the fix reintroduced exactly that failure —
`quarantine_target` returned `None` on a failed rename, no call site checked it,
and the resulting status was byte-identical to a successful quarantine while the
runbook asserted the artifact had been moved. The second catch on the same round
was an exit-code mask (a preview greener than the state it previewed).

That is a *fix-regression* signal, not a rotation signal: the core lens is
pinned into follow-up rounds precisely to re-check the invariant the previous
round claimed to restore, and here it paid. Two core catches move the cumulative
ratio a little toward the core arm without disturbing the direction, and the
standing attribution caveats (the raw split mixes treatment, no-treatment,
targeted rechecks, and workflow-role lenses) remain decisive against reading two
PRs either way.

PR #1835's rounds contribute nothing to attribution: its round 2 was clean.

One process deviation is recorded on the #1825 line and repeated here because it
bears on how these catches should be weighted: round 2 skipped the independent
verifier gate. Both candidates were mechanically checkable by reading the diff
(an unchecked return value; a status overwritten before a tally), and the tool
was in live production use restoring a basin that was down. Recorded as a
one-off deviation with its reason, not as a precedent — the round-3 review that
followed was scoped explicitly to "did this close the hole or move it", which is
the check the skipped gate would otherwise have supplied.

No reviewer-set or rotation-policy change follows.
