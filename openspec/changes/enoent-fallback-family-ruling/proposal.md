# ENOENT 非严格兜底：家族级裁定

## Why

issue #1627 要求对「`strict realpath` → `ENOENT` → 非严格兜底」这一形状做一次家族级裁定，
理由是仓内并存三套都写了理由的相反立场（A 复查 / B 有意容忍 / C 记录在案残留），
新站点作者无法从仓里读出该照哪套写，而该家族链在仓内反复出现。

勘察的结论是：issue 说「从未在家族层裁定过一次」**是对的**，但三个立场**不是**三套教条，
而是同一条**已经成文过两次、每次都只裁本 lane 并把与邻接 lane 的分歧记录走而非解决**的
原则，在不同下游事实上的三个结论：

- 立场 B 的起源 #1332 给出的理由本身就是「下游有护栏」
  （`openspec/changes/archive/2026-08-10-symlink-loop-errno-detection/design.md:216-218`：
  dangling entries fail `is_dir()`/existence checks downstream）。
- 立场 A（#1401）不是推翻这条原则，是同一条原则在**没有下游护栏**的那条腿上的结论
  （`openspec/changes/archive/2026-08-16-runtime-root-safety-symlink-loop/design.md:41-43`）。
- 立场 C（#1402）经实核**同样有下游护栏**：`services/orchestrator/scheduler_state_failure.py:1401`
  的 `path.exists()` 就在 admitted 之后两行，且 `openspec/specs/job-retry-mechanism/spec.md:1585-1589`
  已把它写成硬约束——null-reason absent verdict **SHALL** 只在实际探过存在性之后产生。

所以家族的**行为已经一致**，不一致的只有**说法**，而家族层的裁定确实一次都没作出过
——第一处出处的小标题就是「与 #1402/preflight 家族先例**显式分歧**」，段末把分歧推给了偏离记录。
本 change 的交付物是把那条原则写下来、首次在家族层裁定它、给出可判定的判据，
并留下两条**机械的、不会漂移的**防复发锚：违规者集合守卫，以及权威集合**全部 19 个成员**
的具名处置标记（admit 者写从句号，立场 A 的复查者写 `loop-filtered`——义务是「记录处置」
而非「记录从句」，见 ADR 0009「守卫」）。

## What Changes

- 新增 `docs/adr/0009-path-canonicalization-dereference-doctrine.md`：裁定本身。
- `openspec/specs/safe-filesystem-primitive-contract/spec.md` 新增一条 requirement，
  把裁定的可判定部分固化为规范文本。
- 全仓 tracker 指向从 #1627 改指 ADR 0009（两条 live spec + 一条 requirement + 5 处源码/测试引用，见 tasks A-2）
  （`openspec/specs/slurm-array-runner-integration/spec.md` 的 Scenario
  「parent-segment loop no longer aborts construction on 3.11/3.12」、
  `openspec/specs/runtime-evidence-and-operations/spec.md` 的 Scenario
  「unexpandable tilde in allowed storage roots is tolerated without crashing the preflight」）。**终态必须指向 ADR 而非任何 issue**：
  该 change 合并后 #1627 即关闭，指向它就是一条死指针——PR #1626 复盘已把这条记为
  `closing-issue-while-live-spec-names-it-as-the-tracker`。
- 新增一条守卫测试：三条家族级断言均为「违规者集合为空」形，不断言成员清单（见 ADR 0009「守卫」）。
- `services/orchestrator/scheduler_config/path_modes.py::_resolve_config_path_for_mode` 的注释补 ADR 指针。

## Non-Goals

- **不改任何站点的运行时行为**。裁定结论是零错位站点（ADR 0009「普查」），没有要对齐的对象。
- 不并入 EACCES 跨版本分歧（#1554 / #1623）——issue 明确另一条轴。
- 不处理 `.resolve()` 面（基数与它为什么需要自己的判据见 ADR 0009「已知限制」2；design D5 report-only）。
- 不做 errno 分流与否的裁定：那是正交问题，D2 早有结论（design D4）。
