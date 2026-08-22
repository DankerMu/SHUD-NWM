## 1. Journal identity authority

- [x] 1.1 新增 `completed_pipeline_init_state_identity` 完整mapping accessor：completed hydro identity优先；否则在内部未截断rows中按canonical truth-order选latest current-contract accepted-submit candidate row，latest必须terminal-success且含唯一normalized identity。
- [x] 1.2 选择器拒绝cohort master、marker-free/ordinary row、other-model、malformed identity、latest failed/empty row；必须latest-first再qualify，禁止旧success遮蔽新failure。
- [x] 1.3 既有 `completed_pipeline_init_state_id` 委托完整accessor且只提取 `init_state_id` / `initial_state_id`，保持bare `state_id`、unreadable/no-record返回None；accessor read-only且不经public redaction/job_limit。
- [x] 1.4 `_job_state_evidence` 与 `_CYCLE_SCOPE_JOB_PROJECTION_KEYS` 保持不含 `init_state_identities`；#1183 writer、digest、frozen fields与schema逐字不变。

## 2. Verdict and §8.7 wiring

- [x] 2.1 completion verdict在strict terminal-success分支通过optional full accessor读取observed identity；得到mapping时override hydro evidence，method缺失/返回None时回退原 `terminal_evidence["hydro_run"]`。
- [x] 2.2 cohort stale identity（id或任一双方在场optional field冲突）即使successor ready也判`conflict -> gap`；matching identity即使无successor也`match -> complete`。
- [x] 2.3 pre-change/no-identity cohort继续`absent`：successor ready→complete，successor absent/unready→gap；cold strict no-state与candidate strict wrapper不变。
- [x] 2.4 discovery §8.7与candidate quarantine通过委托后的旧string accessor读取同一cohort token；positive mismatch生效，match/no-accessor/no-record/suffix-less/different-base仍no judgement。

## 3. Requirement-driven tests and red proof

- [x] 3.1 在生产改动前提交完整新行为test batch并运行RED；删除full accessor cohort fallback或verdict接线时至少cohort conflict用例转红；修后同命令GREEN且无`red-proof` stash。
- [x] 3.2 真实file-journal write→reopen→read覆盖完整identity、旧string id、read-only bytes、hydro优先、latest-first与所有拒绝形状。
- [x] 3.3 production scheduler truth table覆盖cohort conflict/match/absent及successor三态；real accessor mapping而非仅fake hydro evidence。
- [x] 3.4 §8.7 discovery stale与candidate quarantine在同一real repository fixture上同向；breaker已有行为不回退。
- [x] 3.5 non-cohort direct/hydro、repos without accessor、run-manifest-only、bare alias、state_save_qc placeholder、projection/keep-list体积不变量保持。

## 4. Spec and Evidence Floor

- [x] 4.1 完整MODIFIED `cross-cycle-warm-start-chaining` requirement，保留全部旧scenario并新增cohort identity可见性真值表。
- [x] 4.2 完整MODIFIED `file-state-snapshot-index` §8.7 requirement，记录hydro-first/latest-candidate fallback与三条gate同源语义。
- [x] 4.3 `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py tests/test_warm_start_chaining.py tests/test_gateway_reconcile.py tests/test_scheduler_backfill.py` 通过；mixed-model oracle 证明 #1735 lineage filter 先于 #1185 full-identity accessor，排除模型不被读取且入 scope 模型仍按 match/conflict 裁决。
- [x] 4.4 `uv run ruff check .`、`openspec validate scheduler-cohort-init-state-visibility --strict --no-interactive`、`git diff --check` 通过。
- [x] 4.5 Invariant Matrix逐面审计；无writer/schema/digest/projection/Protocol/Slurm行为变化，node-22 receipt不作为merge门。

## 5. Risk-pack evidence mapping

- [x] 5.1 Shared state/order + Slurm lifecycle：latest-first authority、real reservation/reconcile roundtrip与旧success遮蔽mutation。
- [x] 5.2 Legacy/error/idempotency：hydro-only、pre-change absent、no accessor、malformed/latest failure、different-base/suffix-less均锁定。
- [x] 5.3 File/resource + provenance：internal untruncated read、on-disk bytes不变、no run-manifest/public-redaction evidence、bounded public payload不扩。
- [x] 5.4 Config/documentation：default与`forecast_state_save_qc`口径、两份spec、accessor docstrings一致；其余not-selected packs无新增surface。
- [x] 5.5 实现报告列出所有检查的producer/accessor/consumer sibling与偏离；无偏离亦须明确。
