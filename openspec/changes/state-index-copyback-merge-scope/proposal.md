# Proposal: state-index-copyback-merge-scope

## Why

Closes #1189。生产链自 2026-07-25T18:40:48Z 起结构性停摆:把新 entry 写进 NFS canonical state index 的**唯一写者**(`state_save_qc` 终态后的 copyback merge)在写入前对 destination(/ghdc)index **全量历史 entry** 做对象存在性校验;node-27 product-archive mover 按 14 天策略归档了 574 个旧 state 对象(cutoff 2026-07-06,receipt 在案)→ 校验永久 fail-closed(`OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`,node-22 journal 留痕)→ 072000 产出的 36 条 f012 后继 checkpoint 永进不了调度器读取的 index → #1183 verdict 修复(已合并,load-bearing)无法收敛,072012+ 永不规划。provider-refresh 用 /scratch 根续期、天天"成功",故障静默 19 天。

## What Changes

1. **merge 校验与拷贝范围收窄到本次 merge 的 source entries**(三处,全在 `merge_state_snapshot_index_copyback`):
   - destination 读侧校验不再做对象存在性校验(结构/schema 校验保留);
   - checkpoint 拷贝循环从"全部 merged entry"收窄到"本次 source entries"——**禁止**把已归档的历史对象从 /scratch 复活回 NFS(与 mover 契约对齐);
   - publish 侧关闭全量对象校验(新增 entry 的对象完整性由逐 entry 拷贝时的 checksum 读回校验保证,不变)。
2. **积压恢复:copyback replay 工具**(新 script):按 cycle/run-ids 幂等重放失败的 state-index copyback(复用修复后的 merge 函数 + `authoritative_run_ids`),带 receipt;用于把 072000 的 36 条 entry 补进 NFS index。无自动触发,operator 显式调用。
3. runbook:copyback fail-closed 判读 + replay 处置步骤。

## Impact

- Affected specs: `file-state-snapshot-index`
- Affected code: `packages/common/state_manager.py`(merge/publish 调用点)、`scripts/`(新 replay 工具)、`docs/runbooks/`
- 不动:mover(node-27)、refresh 续期语义、run-tree copyback 顺序、调度器 index 读取拓扑、merge 冲突语义、锁/CAS
