# Proposal: journal-containment-aware-existence-probe

## Why

Issue #1167（PR #1166 Phase-7 defer 的 P3 follow-up）：分段 cycle 日志的存在性探测
`_journal_segment_exists` 与其兄弟副本 `_sequence_regular_file_exists` 用裸
`os.stat(path, follow_symlinks=False)`，不跟随末端 symlink 但**会跟随被 symlink 的父组件**。
当 `journal/<source>`（或任一父组件）被替换为指向空目录的 symlink 时，探测判"槽位不存在"，
`_read_cycle_segments` 静默返回 `[]` —— 把一次篡改/误配降级为"这个 cycle 没有记录"；
master（未分段）读路径对同一现场经 `safe_fs` containment 是 fail-loud（`file_journal_unreadable`）。
读侧安全信号从"大声报错"被分段化降级成了"静默空"。

## What Changes

为两处探测提供一个共享的 containment-aware 存在性探测（建基于既有
`packages/common/safe_fs.stat_no_follow(path, containment_root=self.root)`，
不改 safe_fs 本身），使 symlink 父组件（以及末端 symlink 占位）在**一切公共面**fail-loud 为
`FileOrchestrationJournalError(file_journal_unreadable)`——parity 契约：与该 lane 今天的
reader fault（损坏文件）同类型同命运，写面零字节写入。载体异常不继承公共异常（杜绝 31 处
broad handler 提前吞并），在三个 choke frame 统一转换。"真实目录 + 文件不存在 → 合法空读"
保持。公共写方法行为变化全部实测钉住（design D7 六行表）：一处从静默 no-op 成功变 loud、
两处 token 从误导错因变真实、三处类型换到该 lane reader fault 的既有类型（pre-existing
暴露类，非新回归）。补 symlink 父组件回归测试（读侧 + sequence floor 侧 + 写边界两组入口 +
parity 对照）。

## Impact

- `services/orchestrator/file_orchestration_journal.py`：`_journal_segment_exists`(:6762)、
  `_sequence_regular_file_exists`(:6356)、`_sequence_directory_exists`(:6369, 同 idiom 第三处)；
  choke-frame 转换：`_read_cycle_segments`(:6796)、`_next_sequence_unlocked`(:6308)、
  `_append_journal_bytes_unlocked`(:6473)（受影响写方法全名单见 design 变更面）。
- `tests/test_file_orchestration_journal.py`：新增 symlink 父组件用例。
- 不改 `packages/common/safe_fs.py` 公共 API。

Fixture level: expanded（强制触发词：`symlink`/`path`/`reader`；规模 S 但不因此降级）。
Upstream suggested level: absent（issue 早于 0.16.0 契约；按触发词定 expanded）。
