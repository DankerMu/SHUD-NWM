# Tasks: journal-containment-aware-existence-probe

Fixture level: expanded
Upstream suggested level: absent (issue 早于 0.16.0 契约；`symlink`/`path`/`reader` 强制触发词定 expanded)

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — 全部私有 helper，公共接口签名不变。
- Config / project setup: not selected — 无配置面。
- File IO / path safety / overwrite: **selected** — 本 change 的主题即 containment/symlink 纪律；证据见 E1-E4。
- Schema / columns / units / field names: not selected — 无 schema/字段变化。
- Auth / permissions / secrets: not selected — 无认证面（symlink 防御属 File IO pack）。
- Concurrency / shared state / ordering: not selected — 探测在既有锁语义内换底座，不改锁/顺序。
- Resource limits / large input / discovery: not selected — 探测频次每 cycle 常数次，无新枚举面（D5 落字）。
- Legacy compatibility / examples: **selected** — 合法空读（场景 C）、冷启动整链缺失、非常规占位交 reader 三格必须逐字节保持；证据见 E5-E6。
- Error handling / rollback / partial outputs: **selected** — 静默空 → fail-loud 的信号升级 + 写路径 fail-closed 不变；证据见 E1-E3、E7。
- Release / packaging / dependency compatibility: not selected — 无依赖变化。
- Documentation / migration notes: not selected — 无运维口径变化；reason token 复用既有 `file_journal_unreadable`（runbook 已覆盖该 token 的通用处置）。

Domain packs (NHMS profile): 全部 not selected — 无地理/时序/数值/Slurm/DB 面；
`orchestrator` 触发词已通过 expanded 定级吸收。

## Required evidence

- E1 读侧主缺陷: `journal/<source>` 为 symlink 且目标目录内无该 cycle 文件 → 公共读接口 raise
  `FileOrchestrationJournalError`, reason `file_journal_unreadable`（不再 `[]`）。
- E2 sequence floor 兄弟副本: 同现场下经 `_next_sequence_unlocked` 的公共写方法 fail-loud
  为 `FileOrchestrationJournalError(file_journal_unreadable)`（parity 契约；不再静默把
  floor 低估；与 issue AC 首选 token 一致）。
- E3 末端 symlink 占位: segment 槽位本身为 symlink → 读侧 raise `file_journal_unreadable`
  （token 钉住，产生点不钉——D3）。
- E4 `latest/` 目录探测: `latest/<source>` 父组件为 symlink 且目标缺该 cycle 目录 → sequence
  写方法 fail-loud `file_journal_unreadable`（D2 第三处纳入；唯一消费链在三 frame 内转换）。
- E5 合法空读不回归: 真实目录 + 无 cycle 文件 → 读接口 `[]`；全新 journal root（目录整链缺失）
  → 读 `[]` 且 `_next_sequence` 返回 1。
- E6 非常规占位交 reader: 目录占 segment 槽 → present → hardened reader 大声失败（既有行为钉）。
- E7 写边界（两组入口各至少一腿，round-2 P2-3；期望值为 round-3 实测列）:
  - E7a `_write_pipeline_job_unlocked` 组：symlink 父组件下 `upsert_pipeline_job` →
    `FileOrchestrationJournalError` + `file_journal_unreadable` + 零字节写入（钉 token+
    公共异常类型+零字节，不钉 message/evidence 形状与产生 frame）。
  - E7b 直接 `_next_sequence_unlocked` 组：`reject_pipeline_job_submit_attempt` 同现场 →
    `file_journal_unreadable`（loud 保持；类型换到该 lane reader fault 既有类型，
    pre-existing 暴露类，D7 论证）。
  - E7c 静默→loud 升级钉：`mark_pipeline_job_permanently_failed` 同现场今天静默成功
    （`outcome='stale'`），本 change 后 → `file_journal_unreadable`（D7 有意变化）。
  - E7d reader 错误 passthrough：既有 reader 错误（损坏文件）在写路径的传播类型与今天一致
    （转换只捕载体，不捕 `FileOrchestrationJournalError`）。
- E7e broad-handler parity（round-2 P1-2 / round-3 契约核心）: `_cycle_materialization_model_ids_unlocked`
  类 swallow lane 上，symlink 现场的可观察结果 == 损坏文件现场的可观察结果（containment
  fault 与 reader fault 同命运，无新静默洞、也不因载体继承被提前吞掉）。
- E8 命令: `uv run pytest -q tests/test_file_orchestration_journal.py
  tests/test_file_orchestration_journal_read_cache.py tests/test_gateway_reconcile.py` 全绿；
  `uv run ruff check .` 通过；
  `openspec validate journal-containment-aware-existence-probe --strict --no-interactive` valid。

## Review focus

1. `stat_no_follow` 的 `FileNotFoundError` 双义（末端缺失 vs 父组件缺失）都必须映射 absent——
   冷启动整链缺失若被误判 loud 会把全新 source 首读打死（E5 第二腿是守门测试）。
2. D3 三处末端 symlink 行为收敛是有意变化：确认无任何现存测试/调用方依赖"symlink 槽位静默 skip"。
3. `_journal_segment_exists` 的"非常规占位 → present → reader 权威"契约保持（E6）。
4. 探测异常映射保持既有 evidence 形状（field=redacted 相对路径，evidence.error_type）。
5. 转换只在 D6 三个 choke frame（`_read_cycle_segments` / `_next_sequence_unlocked` /
   `_append_journal_bytes_unlocked`），目标统一 `file_journal_unreadable`：只捕载体不捕
   `FileOrchestrationJournalError`、零字节写入保证不变；frame 之外不触碰任何写逻辑；
   frames 2/3 是防御纵深，**不得**钉为公共契约（公共 lane 实测全部由 frame 1 定义）。
6. 载体 `_JournalProbeContainmentError` **不得**继承 `FileOrchestrationJournalError`
   （否则 31 处 broad handler 会在转换前吞掉它——round-2 P1-2），且不得逃出任何公共方法。
7. D7 表六行行为逐一核对（含 `update_pipeline_job_status`）：类型换到 reader-fault 既有
   类型的行、静默→loud 一行、token 变真实的两行；`insert_pipeline_event` 的 token 以实现
   实测钉定（round-3 harness 未定项）。缺一即偏离。

## Tasks

- [ ] 1.1 共享 containment-aware 探测 helper（D1 映射表）+ 非继承载体
      `_JournalProbeContainmentError(Exception)`，落 `file_orchestration_journal.py`
- [ ] 1.2 `_journal_segment_exists` / `_sequence_regular_file_exists` / `_sequence_directory_exists`
      切换到共享探测（D2 kind 判定各自保持）
- [ ] 1.3 D6 三 choke frame 转换（目标统一 `FileOrchestrationJournalError(file_journal_unreadable)`；
      只捕载体，不捕基类；frames 2/3 为防御纵深）
- [ ] 2.1 回归测试 E1-E7e（symlink 现场用 `tmp_path` 真实 symlink 构造，不 mock safe_fs）
- [ ] 3.1 E8 命令全绿；偏离/行为变化记录（D2 第三处纳入、D3 有意收敛、D6 choke 转换、
      D7 四行为表、残余风险两条）写入 PR body
