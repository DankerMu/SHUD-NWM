# Tasks: retention-drill-salvage-window-scope

Issue: #1162

## 1. Implementation

- [x] 1.1 `check_drill_gate`(`scripts/node27_timeseries_retention.py:679-682`)db-export 腿改判定跨度:driver `_completeness_has_db_export_overlap` 命中时,取 `derive_salvage_backed_windows(completeness_receipt, drop_window)` 为目标窗集合;每个目标窗与 drop window 求交(clip;目标窗可能越出 drop window **两侧**,实机 receipt 证实两侧越界均存在),逐窗 `_drill_covers(coverage, "db-export", <clipped>)`,任一不覆盖 → `CODE_DRILL_COVERAGE_DB_EXPORT_MISSING` 并 return(保持单 shortfall 早退结构)。**零长交集(subject 端点恰触 drop 边界,`_overlaps` 闭区间语义)仍作为目标窗判定,不 skip**。clip 逻辑内联或私有小 helper,不改 `_drill_covers`/`derive_salvage_backed_windows`/`_completeness_has_db_export_overlap` 任何字节。`check_drill_gate` docstring 补一句 salvage-scoped 语义并引 #1162(检查顺序表述不动)。
- [x] 1.2 D2 fail-closed 分支:driver 命中但目标窗集合为空 → 显式拒 `CODE_DRILL_COVERAGE_DB_EXPORT_MISSING`(空派生绝不当作已满足;函数级防御,上游 schema `coverage_verdict_contract` 与 completeness gate 已双重拦截该形状,见 tasks 2.4)。
- [x] 1.3 runbook `docs/runbooks/tier-node27-timeseries-storage.md` 三处 + 一条新增说明:
  - `:1461-1463` 整窗主句("AND the UNION of the drill's `source=db-export` tuples must likewise cover the drop window")改为 salvage-scoped(db-export 并集覆盖每个 salvage-backed 窗 ∩ drop window,而非整个 drop window);forcing/runs 的 UNION 措辞不动。
  - `:1473-1479` 通用 "per-source UNION does not cover the drop window" 句给 `DRILL_COVERAGE_DB_EXPORT_MISSING` 加限定,避免被读成三腿同义。
  - `:1608-1610` §8.2 释义改为 salvage-scoped。
  - §7.5 增一句 ops 后果:drill 运行时传入的 `--salvage-manifest` 必须覆盖 drop window 内出现的每一个 db-export subject 窗,否则 gate 正确拒绝。
  - 全部编辑保持 wire-code ALL_CAPS token 原样(`tests/test_node27_timeseries_retention.py:355-361`、`:404-413` 双向消费 runbook §8.2 与 design.md 的 token)。
- [x] 1.4 forcing/runs 腿、检查顺序、wire code 常量、receipt 形状(`salvage_backed_windows[]` 保持**未 clip** 原始 subject 窗;gate 判定用 clip 窗,差异刻意)零改动(diff 不触碰)。

## 2. Tests(requirement-driven,`tests/test_node27_timeseries_retention.py`)

- [x] 2.0 既有断言迁移清单:**空**。已逐一核验 db-export 腿相关既有测试在新语义下行为不变:
  - `:865-891` `test_drill_coverage_db_export_missing_refuses` — drill 用 `db_export_window=None`(零 db-export tuple),`_tuples_cover_window` 空输入即 False(`:713-714`),新旧语义同拒同 code,**零修改保持绿**。
  - `:765-793` `test_db_export_recovery_participates_in_forcing_coverage_union` — drop window 恰等于 salvage subject 窗,clip 后目标窗不变,**零修改保持绿**。
  任何对这两个测试的改动都视为语义弱化,须在 PR 偏离记录中单独举证。
- [x] 2.1 混合窗放行(issue 回归锚点,当前实现必红):completeness 含 db-export subject(verdict=complete)只覆盖 drop window 头部子区间,drill db-export tuple 并集恰覆盖该子区间(∩ drop window),forcing/runs 整窗覆盖 → gate 放行(无 reasons)。
- [x] 2.2 salvage 子区间缺口仍拒:同 2.1 结构但 drill db-export 并集在 salvage 子区间内留缺口 → `DRILL_COVERAGE_DB_EXPORT_MISSING`。
- [x] 2.3 多 salvage 窗独立判定(逐窗 vs 并集 hull 的唯一判别):两个**互不相邻(中间有真实空隙)**的 db-export subject 窗;drill db-export tuple 只覆盖这两个窗、窗间留空。(a) 只覆盖其一 → 拒;(b) 两窗都覆盖(窗间仍空)→ 放行。
- [x] 2.4 verdict 口径显式覆盖(D2,**函数级**):直接调用 `check_drill_gate(drill, completeness_receipt=<手工 dict>, drop_window=..., ...)`,completeness 中唯一 db-export overlap subject 的 verdict 为 `pending-archive`(该形状 schema 非法——`coverage_verdict_contract`,`schemas/archive_completeness_receipt.schema.json:159-183`——故**不得**走 `run_retention`/`_write_json` 端到端路径)→ 断言返回 `[CODE_DRILL_COVERAGE_DB_EXPORT_MISSING]`。测试 docstring 注明:该状态被 schema 与 completeness gate(`:608`/`:616`)双重前置拦截,本用例锁的是 drill 腿自身的 fail-closed,不是可观测生产路径。
- [x] 2.5 无 db-export subject → 不要求 db-export:既有 `test_drill_coverage_db_export_not_required_without_completeness_overlap` 保持绿、零修改。
- [x] 2.6 clip 双侧正确性:(a) subject 窗起点早于 drop.start;(b) subject 窗终点晚于 drop.end;drill db-export 并集**只**覆盖交集部分 → 均放行。(a) 杀"不 clip"变异体,(b) 杀"只 clip 左端"变异体(右越界是实机常态,7 天 forcing_version 窗常越过 drop.end)。
- [x] 2.7 forcing/runs 腿回归:两腿既有测试全部零修改保持绿(含 2.0 清单两条)。
- [x] 2.8 红前证据:2.1/2.6(新语义放行类)在实现前必须能红,失败输出逐字留档 `.workplans/pr-<N>/red-before.log`。

## 3. Verification(合并门)

- [x] 3.1 `uv run pytest -q tests/test_node27_timeseries_retention.py`(全绿)
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate retention-drill-salvage-window-scope --strict --no-interactive`
- [x] 3.4 `openspec validate tier-node27-timeseries-storage --strict --no-interactive`(D3 tier draft 修订后仍 valid)

## 4. Ops oracle(node-27 实机,评审 clean 后合并前)

- [ ] 4.1 node-27 经临时 git worktree(主工作树保持 master)checkout PR 分支,同 cutoff 重跑 retention `--dry-run`,**先读数再分支**:
  - 前置读数:当前 inventory-audit receipt 中与 drop window 相交的 db-export subject 窗的最大 end,与实机 drill receipt db-export 并集右端(现为 `2026-06-21T00:00:00Z`,`first-live-pass-20260725T053420Z.json`)比对。
  - 分支 A:目标窗全部被 drill 覆盖 → receipt 不再 `DRILL_COVERAGE_DB_EXPORT_MISSING`,贴路径进 PR。
  - 分支 B(依现有 receipt 更可能:最后一个 salvage 窗 `[2026-06-14T06Z, 2026-06-21T06Z]` 尾部 6h 无 drill 覆盖):仍拒且 shortfall 落在该窗尾部 → **这是正确拒绝,不是本次修复的缺陷**。处置 = 补跑 archive-rebuild-drill 并把覆盖该 forcing_version 的 `--salvage-manifest` 一并传入,产出新 drill receipt 后复跑;**严禁**通过放宽 clip / 改成"任一窗覆盖即可" / 跳过退化窗让 dry-run 变绿。两个 receipt(拒绝 + 重跑通过)都贴进 PR。
  - 不启用 timer(ADR 0002 gated enforce,超出本 issue)。
