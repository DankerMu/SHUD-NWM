# Authorized extraction review — 2026-09-07

## Scope and baseline

用户授权的行为保持型拆分，限定 18 文件；baseline 是尚在 index 的 pre-split readiness，不是 HEAD。C4 提交 `0ed538ba4023df6950ed654de807133a24936753` 未修改。本审不重开父 readiness 全面审查，也不构成任一子 PR 的 frozen-SHA approval。

## Implementation evidence

Implementer `a09d72a7006a55c2e`：新增五个 production 模块、五个测试分区，修改八个允许文件；所有非豁免文件均低于 1000 行。未修改 guard/exclude，未 stage/commit/远端操作。

原始拆分前后 AST 证据保留在 ignored `artifacts/issue1895-split-preservation-20260907/`，最终对照 `ast-body-preservation-vs-index.final.json`：126 个 test 函数的完整 body/decorators/args 相同，helper 无差异。生产差异仅 adapter 的显式 connect_fn 注入和 facade 接线。EvidenceWriter 的 0600 与 C2 full-scope 断言保留。

提取期间遗漏两个 contextmanager、重复一个 lru_cache，以及 EOF 多余空行，均由主流程/AST 对照发现并经 implementer 修复；这些事实不能被测试数量相同掩盖。最终报告 Ruff 与 unstaged diff check exit 0；cached EOF 尚须由主流程重新暂存工作树版本消除。

## Focused independent review

Reviewer `af47bcce1c55add76`（project reviewer）：无 P0/P1/P2 candidate，无需 finding-verifier 代替空候选集裁决。

- 原模块全部既有 importer 的导出与私有别名保持；route smoke/importlib/context manager 接口仍有效。
- 测试分区无重复/丢失定义，helper 导入方向无环。
- 五个新增 production source 的测试覆盖由 `services/production_closure/**` 规则中的 `READONLY_DB_VALIDATION_TESTS` 保证，不是由 importer 闭包保证。改 core test 才会通过反向 importer 闭包选中其分区；新分区变更自身被选择。
- Adapter facade 在构造时捕获 psycopg2.connect，baseline 是调用期查找。当前已核查调用点都先 patch 后构造或构造后使用，无可达回归；未来增加“先构造后 patch”测试需重新核对该 seam。未为假设性未来需求改代码或添加噪声注释。

## Verification and process limits

没有 pytest、collect、CI 或 node-27 运行证据。AST、Ruff、import smoke 不能证明 collection/conftest/运行结果。完整 readiness 合并前必须获得分区测试与 selector 的 CI 执行结果；node-27 oracle 仍按原维护顺序执行，不以本地静态检查替代。

Reviewer 额外执行了 import smoke 和 select_tests 纯函数模拟，超出 brief 的“不重复 verification matrix”边界。其静态审查结论保留，额外执行不充当 Phase 2 或节点 oracle；流程偏差如实记录，不宣称全程符合角色纪律。

该保存点仅用于保全已授权工作和安全切分 C4，不声称整个 readiness 已通过交付门控。
