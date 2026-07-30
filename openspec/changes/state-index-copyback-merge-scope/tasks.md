# Tasks: state-index-copyback-merge-scope

## 1. 实现

- [x] 1.1 **merge destination 侧三处收窄**(`packages/common/state_manager.py`):destination 读侧 `verify_objects=False`(结构/schema/checksum/唯一性校验保留);checkpoint 循环遍历**胜出并进入 merged 的 source entry**(`merged.get(key) == entry`,F2——严禁裸 `source_entries.values()`,严禁把发布集收窄到 source 集);merge 内 publish 调用点 `verify_objects=False`(**不改**函数默认值与其他调用点,must-preserve #8)。source 侧全量校验 `:1923-1932`、冲突语义、checksum/containment/no-follow/读回、锁+CAS 逐字节不变;publish 的 entry 列表仍为 merged 全集(must-preserve #9)。
- [x] 1.2 **replay 工具** `scripts/scheduler_state_index_copyback_replay.py`:双根旗标(默认 env `OBJECT_STORE_ROOT`/`NHMS_OBJECT_STORE_COPYBACK_ROOT`),index 路径固定派生 `<root>/scheduler/state-index/index-last.json`,复刻 root 相等/重叠守卫;`--run-ids` 或可重复 `--cycle`(输入按生产规则小写归一;解析用**平铺** `entry["cycle_id"]`(可选,None 跳过)收集 `entry["run_id"]`,**无** `lineage` 子对象);**空解析非零退出 + 零写**;默认 dry-run(**不调用 merge**,只读集合预览,advisory);`--enforce` 调用修复后的 merge(同一代码路径);receipt 落 env `NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT`(0700,含 schema_version/mode/run_ids/前后计数/copied-reused-replaced);幂等。
- [x] 1.3 **runbook**:copyback fail-closed 判读(journal `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`)+ replay 处置步骤(**以 provider 属主 `frd_muziyao` 执行**,F11)+ 本案 072000 处置记录(引用 #1189);**同步修订** `docs/runbooks/current-production-ops.md:369-376` 的禁令句(F8):精确收窄为"private root 校验与 registry package 解析不变;shared root 的历史 state entry 对象存在性不再要求",避免下任 operator 依旧文"恢复 object verification"原地重装雷。

## 2. 验证(本地)

- [x] 2.1 `uv run pytest -q tests/test_state_manager.py tests/test_run_tree_copyback.py tests/test_production_scheduler.py`（实测同时纳入新增 `tests/test_scheduler_state_index_copyback_replay.py`；1164 passed）(净化 `__pycache__` + `PYTHONDONTWRITEBYTECODE=1`;**必须含 `test_run_tree_copyback.py`**——merge 的既有回归锁全在该文件,两处 `checkpoint_*_count` 断言需随胜出集语义同步更新,F5)。
- [x] 2.2 `uv run ruff check .`;`openspec validate state-index-copyback-merge-scope --strict --no-interactive`。
- [x] 2.3 红前证据:destination 含对象缺失历史 entry 的 merge 场景,在未改源上抛 `state_snapshot_index_object_missing`(`tests/test_state_manager.py::test_state_index_copyback_merge_publishes_new_entry_beside_archived_destination_object` 未改源红在 `state_manager.py:2517` `_verify_state_index_object`;同批 `..._does_not_copy_losing_source_entry_object` 红在 `state_manager.py:2060` `state_snapshot_index_object_checksum_mismatch`)。
- [x] 2.4 负测锁:已归档对象不复活(merge 后 destination 目标路径仍不存在);**败北 source entry 对象不拷**(F2);entry_count 守恒 `published == destination ∪ 胜出source`(must-preserve #9);幂等重放全 reused;must-preserve #8(其他 publish 调用点行为不变);replay 空解析非零退出零写、dry-run 零 index/对象变更。

## 3. 评审

- [x] 3.1 fixture review(只读)→ 修复 → validate。(第 1 轮:15 findings(5 P1/6 P2/4 P3)已全部修入 fixture,validate 通过)
- [ ] 3.2 实现后 risk-adaptive cross-review(≥2 lane)+ verifier 批次;round ledger 记账。

## 4. Evidence Floor(实机 oracle,merge 后)

- [ ] 4.1 node-22 部署后,以 `frd_muziyao` 执行:replay dry-run 核对 → `--enforce --cycle gfs_2026072000 --cycle ifs_2026072000`(**小写**,F4)→ NFS index entry_count 以 receipt 前后计数为准(**预期 +36**,receipt 在 `NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT` 下在案);随后自然 pass:072000 verdict complete(不再 oldest gap)、072012 候选生成并**提交**。若观测偏离,先读数再分支,严禁放宽判定。
- [ ] 4.2 **验收(用户裁定口径,继承自 #1183)**:连续两个完整 warm-start pass 跑通——072012 Slurm 成功 + 其 +12h 状态经**自然 copyback**(修复后首个 `state_save_qc`,journal 无新 `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`)进入 NFS index + 下一 cycle(072100)以之 warm start 提交并成功。receipt(pass 文件名 + index entry_count 轨迹 + squeue/sacct)回贴 issue #1189 与 #1183。若观测偏离,先读数再分支,严禁放宽判定。
