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
