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
