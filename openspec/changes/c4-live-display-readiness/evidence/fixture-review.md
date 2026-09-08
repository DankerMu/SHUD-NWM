# C4 split fixture review

## Initial review — 2026-09-07

Reviewer: `ae6a6b53f24237e15`（project reviewer，Sonnet）。只读 fixture review，未执行测试/CI/strict validation；不是实现 comprehensive approval。

Verdict: revise。

- tasks 2.1 应明确同时交付 `tests/test_select_ci_tests.py` 的 `_frontend_filter_block` 和 C4 schema-only 测试，不能依赖含糊的“唯一 hunk”表述。两段代码可以位于同一 diff hunk；修订不声称物理 hunk 数。
- tasks 1.1 应明确子 issue 的 `Verification:` 字段、完整 Evidence Floor 引用、live 验收后置及不关闭 #1895/#1891。

两项均已由 orchestrator 修订 tasks 文案。相同 reviewer 的一次修订复核返回 `Fixture review: pass`，无残余必补项；其余 tier/risk packs/Invariant Matrix/must-preserve/独立性与 spec delta header 初审核对通过。主流程执行 `openspec validate c4-live-display-readiness --strict --no-interactive` 通过，reviewer 未重复执行。该结果仅为 fixture 契约通过，不是实现 approval。

Reviewer 认为先合并未部署能力、完整 readiness 合并后才履行 node-27 live 义务符合原维护窗口顺序。此意见仅解释执行顺序，不构成权限扩展或取消 live receipt 硬门；无部署、无 node-27 访问、无 live PASS，最终生产验收仍由 #1895 完成。
