## 1. Clarify authoritative classification

- [x] 1.1 修改 `独立真实 C4 入口` 的完整 requirement：明确 path/URL 缺失的 BLOCKED 分类优先；pin FAIL 场景要求 path 与两个 URL 已提供。
- [x] 1.2 确认只改规格，无 runtime/test/schema 行为；目标 OpenSpec strict validation 与 `git diff --check` 通过。

Suggested fixture level: none
Minimal mergeable slice: atomic — 一处组合状态优先级的权威文本。
Risk packs: Schema / field semantics selected；documentation selected；其它 core/domain packs not selected，因为不改实现、权限、文件 I/O、并发、资源或生产数据。
Evidence: `openspec validate c4-input-classification-precedence --strict --no-interactive` PASS；diff 只含 fixture 与归档后的权威 requirement delta。
Non-goals: 不改 C4 分类实现/测试；不在本项运行 live；不关闭 #1895/#1891。
