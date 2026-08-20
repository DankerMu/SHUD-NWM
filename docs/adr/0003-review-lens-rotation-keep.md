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
