# Design: journal-containment-aware-existence-probe

Fixture level: expanded
Project profile: NHMS

## Change surface

- `services/orchestrator/file_orchestration_journal.py`
  - `_journal_segment_exists` (:6762) — 分段槽位探测（调用方 `_cycle_segment_paths` :6729，
    其自身三个调用方：`_read_cycle_segments` :6796（读）、`_next_sequence_unlocked` :6317
    （sequence floor）、**`_append_journal_bytes_unlocked` :6473（写——rollover 目标判定）**）
  - `_sequence_regular_file_exists` (:6356) — sequence floor 探测（`_next_sequence_unlocked`，
    经 `_write_pipeline_job_unlocked` :6032 亦处于写路径上游）
  - `_sequence_directory_exists` (:6369) — `latest/` 目录探测（`_latest_replay_sequences_unlocked`）
  - choke-frame 转换点（D6）：`_read_cycle_segments` (:6796)、`_next_sequence_unlocked`
    (:6308)、`_append_journal_bytes_unlocked` (:6473)
  - 受 frame 2 影响的直接调用方（D7 行为钉）：`_write_pipeline_job_unlocked` (:6032)、
    `reject_pipeline_job_submit_attempt` (:2382)、`mark_pipeline_job_permanently_failed`
    (:2538)、`permit_pipeline_job_retry` (:2701)、`project_forecast_cohort_tasks` (:2938)、
    `_append_validated_record_unlocked` (:6147)、`_next_event_id_unlocked` (:6382)、
    `_next_accepted_submit_event_id_unlocked` (:6394)、`_materialize_latest_unlocked` (:6541)、
    `_materialize_cycle_latest_unlocked` (:6595)
  - 新共享探测 helper + 载体异常（本文件内私有；不落 safe_fs）
- `tests/test_file_orchestration_journal.py` — symlink 父组件回归用例

## D1: 共享探测建基于 `safe_fs.stat_no_follow`

`packages/common/safe_fs.stat_no_follow(path, containment_root=self.root)`（safe_fs.py:249）
已实现所需语义：逐组件 `O_NOFOLLOW` 打开父链（`_open_parent_dir`，ELOOP →
`SafeFilesystemError`），末端 `dir_fd` lstat，末端 symlink → `SafeFilesystemError`，
missing（末端或任一父组件）→ `FileNotFoundError`。共享 helper 的映射：

| stat_no_follow 结果 | helper 行为 |
|---|---|
| `FileNotFoundError`（末端或父组件缺失） | absent（合法空） |
| `SafeFilesystemError`（symlink 父组件——Linux 走 ELOOP 分支、macOS 走 ENOTDIR/`NotADirectoryError` 分支，殊途同归；末端 symlink / 非目录组件 / io） | raise 探测错误（见下） |
| 其余 `OSError`（权限等） | 同上 raise 探测错误（保持两处探测现有 OSError 分支行为；注意 `_open_parent_dir` 需父链读权限，`0711` 目录会从 absent 翻成 loud——D1 此行已覆盖该结果，PR body 残余风险落字） |
| 成功 | 返回 `st_mode`，调用方做 kind 判定 |

探测错误的载体是一个**不继承 `FileOrchestrationJournalError` 的内部异常**
（`_JournalProbeContainmentError(Exception)`，携带 redacted 相对路径 + error_type）。
不继承是 round-2 review P1-2 的裁决依据：本文件已有 31 处
`except FileOrchestrationJournalError` broad handler，子类会被其中位于探测 lane 上的
handler（:6615 / :6251 / :2762）**在转换前**吞掉，把 containment fault 变回静默空——
恰是本 issue 要消灭的失效模式。载体在 D6 的三个 choke frame 被转换为公共类型；
**载体绝不允许逃出任何公共方法**（D6 覆盖论证）。
测试只钉 reason token 与公共异常类型，绝不钉 message 文本（跨平台分支差异，Note）。

不改 safe_fs 公共 API（issue 边界）；helper 落 `file_orchestration_journal.py`。

## D2: 三处调用方的 kind 语义（各自保持既有判定，只换底座）

- `_journal_segment_exists`: 安全占位（含非常规 non-symlink 占位，如目录/FIFO）→ present
  （hardened reader 仍是非常规占位失败的唯一权威——现有 docstring 契约保留）；absent → False。
- `_sequence_regular_file_exists`: `S_ISREG(mode)`。
- `_sequence_directory_exists`: `S_ISDIR(mode)`。同一 idiom 第三处，失效模式相同
  （symlink 父组件 + 目标缺 dir → 静默 skip → sequence floor 低估 → 潜在 sequence 复用），
  一并切到共享探测。issue 将其列为实现者判断项；本设计判定纳入，理由如上。

## D3: 末端 symlink 行为收敛（有意的行为变化，非静默）

- `_journal_segment_exists` 旧行为：末端 symlink → present → hardened reader raise
  `file_journal_unreadable`。新行为：探测自身即 raise **同一 reason token**。外部可观察
  信号不变（同 token 大声失败），错误产生点前移；测试只钉 token 与 loud 性质，不钉产生点。
- `_sequence_regular_file_exists` 旧行为：末端 symlink → `S_ISREG(lstat)`=False → **静默 skip**
  （sequence floor 漏计，属同类缺陷的另一格）。新行为：raise `file_journal_unreadable`。
  这是本修复的有意扩展：journal 树内合法状态不含任何 symlink（safe_fs 纪律），静默 skip
  只可能掩盖篡改/误配。
- `_sequence_directory_exists` 旧行为：末端 symlink 指向真实目录时 `os.stat(follow_symlinks=False)`
  取 lstat → `S_ISDIR`=False → 静默 skip。新行为：raise。同上理由。

## D4: 必须保持的合法语义

- 真实目录 + cycle 文件不存在 → absent（`_read_cycle_segments` 返回 `[]`）——issue 场景 C。
- 全新 journal（`journal/<source>` 目录整链缺失）→ absent（`FileNotFoundError` 来自父组件缺失，
  与末端缺失同格处理）——冷启动/新 source 首写前的读全部走此格。
- 写路径 fail-closed 语义保持：symlink 父组件下一切公共写方法 fail-loud
  `FileOrchestrationJournalError(file_journal_unreadable)`、零字节写入——与该 lane 今天的
  reader fault（损坏文件）同类型同命运（parity 契约；逐方法结果见 D7 表；今天静默成功的
  `mark_pipeline_job_permanently_failed` 是唯一的成功→失败翻转，有意且钉住）。
- 非常规 non-symlink 占位（目录/FIFO 占 segment 槽）：present → hardened reader 权威 raise，
  与现状一致。
- `_cycle_segment_signatures`(:6781) 的 `_stat_signature` 缓存指纹 idiom 不在本 change 范围
  （issue 明确 stat 用法普查 out of scope）；如实现中发现其与新探测产生一致性问题，报偏离不修。
  **该一致性问题已实测坐实**（PR review round-1 B2）：`_stat_signature` 跟随 symlink 父组件，
  空槽指纹在"真实空目录"与"symlink→空目录"下同为 all-None，长活实例（scheduler 每进程一个
  repository，`scheduler_core.py:110`）的 warm `_cycle_rows` 缓存在 tamper 后继续静默返回
  缓存的 `[]`（fresh 实例返回 blocked 行）。按本条预授权报偏离不修，路由 follow-up issue；
  写路径不受影响（warm 缓存下 frame 2 照常 raise，实测）。

## D6: choke-frame 转换（round-1 P1-1/P2-2 + round-2 P1-1/P1-2 驱动，终版）

**覆盖论证**（转换必须发生在 choke frame，不在叶子）：三个探测只被三个函数消费——
`_cycle_segment_paths`(:6729)、`_next_sequence_unlocked` 内的直接 sequence 探测(:6319)、
`_latest_replay_sequences_unlocked`(:6345, 目录探测)。而 `_cycle_segment_paths` 的全部
调用方是 :6317/:6473/:6796，`_latest_replay_sequences_unlocked` 的**唯一**调用方是
`_next_sequence_unlocked`(:6324)，公共 wrapper `_next_sequence`(:6304) 全仓（含 tests）
**零调用方**。因此载体的转换点恰好是三个 frame，且完备：

三个 frame 的转换目标**统一**为
`FileOrchestrationJournalError("file_journal_unreadable", field=redacted 相对路径,
evidence={error_type})`——读写不分叉（round-3 review 实测钉定；见 D7 契约论证）：

1. **`_read_cycle_segments`(:6796)**：全部读 lane 经此路由（E1）。七条 D7 lane 中六条的
   公共可观察错误由此 frame 定义：这些写方法在触达 frame 2 之前先经
   `_cycle_rows`(:3828→:3880/:3888) 或 `_cycle_rows_by_model_unlocked`(:3910→:3953/:3959)
   做 precondition read。
2. **`_next_sequence_unlocked`(:6308)**：**同为公共契约 frame**（PR review round-1 B1
   实测修正——早先"防御纵深、公共 lane 零触发"的判断是错的）：
   `insert_pipeline_event`(:3597) → `_next_event_id_unlocked`(:6423) →
   `_next_sequence_unlocked` 在任何 precondition read **之前**先算 sequence floor，
   该 lane（含 E2/E4 现场）的公共错误即由本 frame 转换；删除本 frame 会让裸载体从公共方法
   逃逸（实测复现）。其余直接调用方
   （`_write_pipeline_job_unlocked`(:6032)、`reject_pipeline_job_submit_attempt`(:2504)、
   `mark_pipeline_job_permanently_failed`(:2609)、`permit_pipeline_job_retry`(:2826)、
   `project_forecast_cohort_tasks`(:3320)、`_append_validated_record_unlocked`(:6163)、
   `_next_accepted_submit_event_id_unlocked`(:6402)、`_materialize_latest_unlocked`(:6561)、
   `_materialize_cycle_latest_unlocked`(:6598)）在公共 lane 上被 frame 1 前置拦截，对它们
   本 frame 是防御纵深。E2/E4/E7d 的测试钉住本 frame 的可观察行为。
3. **`_append_journal_bytes_unlocked`(:6473)**：防御纵深（append lane 的 rollover 探测；
   公共 lane 实测零触发——触达 append 意味着更早的探测已成功）。

约束与既有行为保持：

- 转换只捕载体 `_JournalProbeContainmentError`，**不捕** `FileOrchestrationJournalError`——
  既有 hardened reader 错误（损坏文件等）在一切 lane 的传播/吞并行为原样保持。
- 零字节写入保证不变：失败发生在任何 `os.write` 之前。
- **broad-handler parity 契约**：转换后的错误与今天 hardened reader 抛出的同类错误在全部
  31 处 broad handler 中**同命运**（如 :6615 的 materialization 部分吞并、:6251 的 None
  回退——那是这些 lane 对一切 journal fault 的既有语义，不是本 change 新开的静默洞；
  目标是 containment fault 与 reader fault **同等 loud**，不是超越）。
  对照测试钉 parity：symlink 现场在该 lane 的可观察结果 == 损坏文件现场的可观察结果。
- 载体不逃逸：三 frame 之外无任何探测消费点（上述完备性 grep 论证 + round-3 全方法
  instrumented 运行零逃逸）；review 时以"全部公共方法在 symlink 现场只抛
  `FileOrchestrationJournalError`"抽查。

## D7: 公共写方法的行为变化——parity 契约（round-3 实测列，诚实钉）

公共契约：**symlink 现场与损坏文件现场同命运**——每个公共方法在 symlink 父组件现场
surfaces 与该 lane 今天的 reader fault 相同的类型（`FileOrchestrationJournalError`），
token 为 `file_journal_unreadable`。实测行为表：

| 方法 | 今天（symlink 现场） | 本 change 后（实测） | 性质 |
|---|---|---|---|
| `upsert_pipeline_job`（E7a 组） | append 内 safe_fs 兜住 → `OrchestratorError(FILE_JOURNAL_WRITE_FAILED)` | `FileOrchestrationJournalError(file_journal_unreadable)` | 失败点前移；类型换到该 lane reader fault 的既有类型 |
| `reject_pipeline_job_submit_attempt` | `OrchestratorError(FILE_JOURNAL_WRITE_FAILED)` | `FileOrchestrationJournalError(file_journal_unreadable)` | 同上 |
| `mark_pipeline_job_permanently_failed` | **静默成功**（`AcceptedSubmitCommitResult(outcome='stale')`） | `FileOrchestrationJournalError(file_journal_unreadable)` | 有意升级：静默接受篡改→大声（本 issue 最有价值的行为变化） |
| `permit_pipeline_job_retry` | loud `file_journal_authority_transition_requires_typed_api`（空读误导出的错因） | loud `file_journal_unreadable`（真实错因） | loud→loud，token 变真实 |
| `insert_pipeline_event` | `OrchestratorError(FILE_JOURNAL_WRITE_FAILED)` | `FileOrchestrationJournalError`（token 预期 `file_journal_unreadable`，round-3 harness 未定，实现时以测试实测钉） | 类型换到 reader fault 既有类型 |
| `update_pipeline_job_status` | loud `file_journal_authority_transition_requires_typed_api` | loud `file_journal_unreadable` | loud→loud，token 变真实 |

类型从 `OrchestratorError` 变 `FileOrchestrationJournalError` 的行（1/2/5）**不是新回归类**：
这些 lane 的调用方今天就已暴露于 reader fault 的 `FileOrchestrationJournalError`（round-3
损坏文件对照实测：同七个方法今天全部 surface `file_journal_malformed_json` 同类型）；只捕
`OrchestratorError` 的 handler（实际站点 `chain_forecast_submission.py:164`——round-1 B4
修正：早先引用的 `chain_array_accounting.py:256` 包的是 sacct 解析，不在 journal lane 上）
的暴露是 pre-existing 类，已在 Non-goals 落字。全表进 PR body 偏离/行为变化节。

## D5: 性能

`stat_no_follow` 每次探测重走父链 open（从文件系统锚点逐组件，成本随 journal root 绝对深度
线性增长）。实测量级（round-1 B3，macOS/APFS，depth-9 root）：单次探测 ~1µs → ~100-140µs
（约两个数量级）；一次 `insert_pipeline_event` 发出 ~76-92 次探测（早先"槽位×surface≈8"的
频次模型低估了一个数量级），端到端写 lane +40-80%（8.7ms → 15.8ms 级）。绝对成本（每写
~毫秒级、每 cycle 数十写）不构成运维可观察的回归；Linux openat 更廉价、生产 root 更浅，
实际数字更小。性能优化 explicit non-goal。

## Seams under test

- 公共读边界：journal 公共读接口（经 `_cycle_journal_records`/`_read_cycle_segments` 路由）——
  symlink 父组件 + 目标缺文件 → `file_journal_unreadable`；真实目录缺文件 → `[]`。
- sequence 边界：sequence floor 探测（`_next_sequence_unlocked` 消费面）在 symlink 父组件下
  fail-loud、绝不静默低估 floor；公共可观察面为其写方法的
  `FileOrchestrationJournalError(file_journal_unreadable)`（parity 契约，与 issue AC 的
  首选 token 一致）。
- 写边界：公共写方法在 symlink 父组件下 fail-loud
  `FileOrchestrationJournalError(file_journal_unreadable)` 且零字节写入（parity 契约；
  钉 token+公共异常类型+零字节，不钉 message/evidence 形状与错误产生 frame）。

## Non-goals

- safe_fs 公共 API 改造；`_stat_signature`/其余 stat 用法普查；#1165 bounded-window /
  orphan-segment 语义；性能优化；探测产生点的错误信息逐字节兼容（只钉 reason token）；
  既有 hardened reader 错误在写路径的类型逃逸普查（pre-existing，D6 明确不新增吞并也不修）。

## 残余风险（PR body 落字项）

- warm `_cycle_rows` 指纹缓存在 tamper 后继续静默返回缓存 `[]`（D4 坐实项；pre-existing
  cache idiom，报偏离不修，路由 follow-up issue；spec 措辞已按此收窄）。
- probe 成本量级见 D5（披露项，非回归）。
- `self.root` 未 resolve（:502）：部署配置的 journal root 自身若经 symlink 到达——round-1
  correctness 实测该项**高估**：root 本身 symlink 时 base 与 head 行为一致（写
  `FILE_JOURNAL_WRITE_FAILED`、读被 walker 的 `file_journal_unsafe_scanned_entry` 挡），
  无静默→loud 翻转。保留缓解事实：写侧 `_ensure_root_unlocked` 一直要求 symlink-free root，
  任何写过 journal 的节点已满足约束。
- `_open_parent_dir` 需父链读权限（`os.stat` 只需 search 权限）：`0711` 父目录会从 absent
  翻成 loud（D1 表 OSError 行的既定结果，非缺陷）。
