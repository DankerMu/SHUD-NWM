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
