# Proposal: reconcile-sacct-window-local-wallclock (#1559)

## Why

Issue #1559（PR #1558/#1282 task-0.1 探针路由，**BLOCKS #1116**）：
`services/orchestrator/reconcile.py` `_pages()`（`:341-350`）在 tz-aware UTC 轴上算
7d/12h 分页边界，`:382-383` 两端裸 strftime 渲染进 `--starttime=/--endtime=`
（`:391-392`）——sacct 按宿主本地时区解释，整窗被平移宿主 offset。UTC+8（node-22）
上**向过去平移 8h**：reconcile 要找回的恰是「几分钟前 sbatch、durable bind 未落」的
作业，年龄 < 8h 的 job 不落任何一页，其「sacct 查不到」是空洞缺席。**被证伪的具体
不变量**：`coverage_start`/`coverage_end`（`:416-417`）取自 UTC pages 元组，修复前
它们声称覆盖了一个 sacct 从未扫过的区间——`coverage_complete`（`:419-420`，消费于
`:1818-1825`）正是授权 `reservation_lost` 的门，对分钟龄作业恒为假阳性→过 grace 判
`reservation_lost` → 重新 reserve+sbatch → 同 cohort 双重提交。全程静默（渲染串与
TZ 无关，仅解释侧变；`tests/test_gateway_reconcile.py:10302` 现有期望串钉死了 UTC
口径且不 pin TZ）。本仓该缺陷类最后一处（#1117 parse 侧、#1282/PR #1558 list_jobs
侧已修）。

## What Changes

- issue 推荐修法（与 PR #1558 同构）：`:382-383` strftime 前插 `.astimezone()`
  （两行 + 注释）；`_pages()` 内 `.astimezone(UTC)` 边界算术与 `now` 注入缝（`:321`）
  保留。
- **缓存身份论证显式**（issue 实现细节条款，fixture review 强化）：渲染串兼任
  `page_key = (*owner_scope, start_time, end_time)`（`:384`）。无害性论证分两半：
  **单射性**——相邻页边界相距 12h，最大 DST 跳变 1h，任何 TZ 下渲染串不可能碰撞
  （7d/12h 恰 14 整页，`max(end-width, floor)` 不产生 start==end 退化页）；
  **稳定性**——`pages` 每 querier session 冻结一次（`:355`），且 page_key/
  `indexed_matches_by_page` 均为 closure-local（`:334`），不落盘、不出日志、无跨
  session 消费者。PR body 必须写明，不留给读者推。
- TZ-pinned 回归测试（复用 PR #1558 形态：`_pinned_local_timezone` + pinned now +
  完整字面串 + skipif tzset）：三档（Asia/Shanghai / America/New_York / UTC）至少
  断言最新页 `--endtime=` 与最老页 `--starttime=`；期望值写死字面量。
- 同步更新 `tests/test_gateway_reconcile.py:10302` 期望串并为该用例 pin TZ；
  `:10290` 的 `scope_pages`/`commands` 计数断言（session 冻结与 page_key 去重语义）
  不回归。

## Non-Goals

- #1116 的 comment 能力探测 / fail-closed 语义本体（本单只是其前提修复）。
- `COMMENT_SACCT_LOOKBACK_DAYS`/`COMMENT_SACCT_PAGE_HOURS` 取值、分页策略、
  `_SacctScanBudget` 预算语义。
- node-22 历史 reconcile 判定回填（若确认误判另开，参照 #1284 对 #1117 先例）。
- issue 验收最后一条（node-22 30s 实机 sacct 对照，p1 升级判据）：不阻本 PR，但
  **具名为 #1559 的 post-merge 义务**（tasks 3.5）——它是「sacct 按宿主本地解释」
  前提的唯一实证确认；若前提有误，本修会把 node-22 窗口反向前移 8h（premise 依据：
  #1117 parse 侧已确认行为 + Slurm 文档 + PR #1558 已按同前提上线）。

## Risk triage

- Fixture level: compact（PR #1558 同构修法 + 既有测试形态复用；issue 已把实现
  细节钉到行）。
- Repair intensity: low-medium（reconcile 判定窗口面，但机械两行；缓存身份已论证）。
- Risk packs: test-evidence selected（三档字面串 + 修复前红 + :10302 口径迁移）；
  env-divergence selected（TZ 轴）；state-semantics selected（reservation_lost
  误判路径是本修的存在理由，session 冻结/page_key 语义不得回归）；其余 not selected。

## Must preserve

- `_pages()` 分页算术（UTC 轴、页数、页宽、floor 对齐）逐字不变；渲染串仅本地化。
- session 冻结语义（`:355`）与 `page_key` 去重行为不回归（`:10290` 计数断言绿）。
- 宿主 TZ=UTC 时渲染串与修复前逐字同。
- `now` 注入缝（`:321`）保留。

## Evidence mapping

- 验收 1 → 1.1+2.1；验收 2 → 2.1（三档、最新页 endtime + 最老页 starttime）；
  验收 3 → 3.1 红证（非 UTC 档修复前红）；验收 4 → 2.2（:10302 迁移 + pin TZ +
  计数断言不回归）；验收 5 → 3.2/3.3；验收 6（node-22 实机对照）→ Non-Goal 记 issue。
- Verification：`uv run pytest -q tests/test_gateway_reconcile.py` + ruff（本地）；
  merge 后 node-27 receipt。
