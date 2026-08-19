# Proposal: accepted-submit-identity-write-semantics

## Why

Batch J 合并交付三个 accepted-submit 证据完整性缺口（#1180 缺 oracle、#1187/#1188 是 PR #1184 显式记录的两条「已知边界」）。三者同族：都以 `services/orchestrator/accepted_submit_identity.py` 的不变量与
`file_orchestration_journal.py` 的写路径为对象，都属「未裁定/无锁 → 一次重构即可静默翻向」的敞口。

1. **#1187 per-model 行无冻结闸**：`0f617268` 把 `init_state_identities` 加进**全局**
   `_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS`（`file_orchestration_journal.py:190`），但冻结闸的入口条件是
   `accepted_submit_contract_is_current(existing) and accepted_submit_master_identity_is_structural(existing)`
   （`:1682-1684`）——**master-only**。派生的 per-model（candidate）终态行落到 `:1713-1716` 的无条件
   合并循环，于是该字段只得到「可写」这一半、没得到「被冻结」那一半：一次公共行 round-trip 就把
   durable `s3://` URI 洗成字面量 `[object-uri]` 并持久化（写路径 `_write_pipeline_job_unlocked`
   不经 `_append_validated_record_unlocked`，反洗白护栏 `_strip_redaction_placeholders` 不介入），
   显式 `None`/`[]` 直接抹平血统证据。改动前该字段不在可变表内会被 silent-keep，
   **per-model 覆写窗口是该修复新引入的**。
2. **#1188 reclaim 静默丢弃重算映射**：reclaim 的新行由 `existing`（attempt #1 持久行）经
   `apply_accepted_submit_transition(..., begin_attempt())` 派生（`:1905-1911`），而从 `request_row`
   回填的 key 元组（`:1944-1958`）**根本不含** `init_state_identities`——本次 pass 重算的映射被整体
   丢弃。（成因是**回填表遗漏该字段**，不是包住该循环的 `if not versioned_master:` 守卫：评审实测
   单独翻转该守卫变异存活。）keep-first 是该姿态的**副作用而非裁定结果**：spec 只写「值自预约起
   不变」，而 reclaim 恰恰开启新 attempt，「预约」在该边界上二义。
3. **#1180 八个 raise 点零负向用例**：`accepted_submit_identity.py` 的 `identity_blocked_streak` / `identity_mismatch_released` 不变量（构造期 `:229`/`:239`/`:262`/`:264`，归一化 `:566`/`:600`/`:626`/`:630`）是 `identity_mismatch_released` 终态语义的唯一守卫，变异实验显示删掉其中两条全套测试仍存活。

## What Changes

- **裁定并实现 (#1187)**：per-model（candidate）行走**与 master 对称的冻结**——新增一张最小 per-model 不可变证据表（含 `init_state_identities`），在 upsert 的 existing 分支上当 `accepted_submit_row_kind(existing) == "candidate"` 时做同形分歧拒绝。**不**走占位符容忍 merge（见 design D-B rejected）。
- **裁定并落字 (#1188)**：reclaim 边界取 **keep-first**——reclaim 不刷新该映射；spec 措辞从「值自预约起不变」收紧为「自**首次**预约起不变，reclaim 进入新 attempt 不刷新」，reclaim 路径就地注释说明该字段有意不随新 attempt 刷新。
- **补 oracle (#1180)**：八个 raise 点各至少一条负向用例（参数化覆盖 streak 的 负数/非 int/bool/str），断言 typed error **且零写入**；#1187 新增的冻结闸同批补负测（同族新 raise 点不再重蹈无 oracle）。
- 三项各配变异体证死（红-绿对照）。

## Non-Goals

- 不改 #1183 的 verdict 侧三态比对语义；不把记账身份接入 `_job_state_evidence` 可见面（#1185，另需 seam 决策）。
- 不改这些不变量本身的语义或增删条目（#1180 边界原文）。
- 不改 reclaim 的存活性判定（dead-status 谓词、`submission_attempt` 递增、anchor 重打——#1173 已定稿）。
- 不做任何 migration / 历史行回填。
- 冻结表内其余 `*_uri`（`log_uri`）的「sanitized 值复放即分歧」同源类别不在必修范围（见 design 的 master PUBLIC 复放裁定）。`slurm_job_id` **不**被占位符化，不属该类别（fixture review Note 更正）。
- SQL 版 `chain_repository.py` 的 reclaim 语句不涉及该列（accepted-submit 证据面目前 file-journal 独有），不动。

## Impact

- Affected specs: `pipeline-job-persistence`（MODIFIED init-state identity forward-only 需求：per-model 对称冻结 + reclaim keep-first；MODIFIED identity-mismatch 收敛需求：不变量 test-anchored）
- Affected code: `services/orchestrator/accepted_submit_identity.py`（per-model 冻结表）、`services/orchestrator/file_orchestration_journal.py`（upsert 冻结闸 candidate 臂、reclaim 就地注释）
- Affected tests: `tests/test_file_orchestration_journal.py`、`tests/test_gateway_reconcile.py`、`tests/test_warm_start_chaining.py`
- Closes #1180, closes #1187, closes #1188。
