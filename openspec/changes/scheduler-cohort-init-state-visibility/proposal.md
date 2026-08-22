## Why

Issue #1185 暴露了 #1183 写侧与两条读侧判定之间的断链：accepted-submit cohort 已在预约 master 和 per-model reconciled terminal row 中持久化 `init_state_identities`，但 completion verdict 仍只看 `hydro_run`，§8.7 predecessor gate 的 accessor 也只读 completed hydro row。生产 cohort 因此永久落入 `absent`，写下的冲突证据无人消费。

## What Changes

- 在 file-journal repository 新增窄的完整 identity accessor：completed hydro identity优先；缺失时从内部未截断 job rows 选择最新权威 accepted-submit per-model terminal row 的单条 identity。
- 既有 `completed_pipeline_init_state_id` 委托完整 accessor，仅提取其历史认可的 `init_state_id` / `initial_state_id` aliases；discovery §8.7 与 candidate quarantine 无需复制新选择逻辑。
- completion verdict 通过可选 repository accessor消费完整 cohort identity（id/checksum/uri/valid_time）；无 accessor或无返回值时仍回退原 terminal `hydro_run` evidence，保留非-journal与历史路径。
- 不扩 `_job_state_evidence` keep-list，不把 `init_state_identities` 加入 `_CYCLE_SCOPE_JOB_PROJECTION_KEYS`，不改变 writer、absence容忍规则、digest或persisted schema。
- 补齐 cohort conflict/match/absent、§8.7两侧一致性、latest-row authority与legacy/direct compatibility的判别测试。

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `cross-cycle-warm-start-chaining`: completion verdict可消费cohort terminal row已持久化的完整init-state identity，并继续严格区分match/absent/conflict。
- `file-state-snapshot-index`: completed-pipeline journal identity accessor从hydro-only扩为cohort-aware单一来源，供discovery与candidate §8.7两侧一致消费。

## Impact

- Runtime: `services/orchestrator/file_orchestration_journal.py`、`scheduler_discovery.py`；既有 `scheduler_candidates.py` 通过旧 accessor委托自动继承，无调用方签名修改。
- Tests: file-journal真实读路径、production scheduler verdict、warm-start/§8.7/gateway回归。
- Specs: 两份完整 MODIFIED requirement；无migration、schema、writer、Slurm/sbatch或资源配置变化。
- Oracle: 纯db-free journal/state-machine逻辑由本地/CI pytest裁决；不要求node-22 live receipt。
