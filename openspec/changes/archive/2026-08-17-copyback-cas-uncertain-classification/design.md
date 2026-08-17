# Design: copyback-cas-uncertain-classification

坐标系：master `4ced0c75`。

## D1 判据：phase 自述 + 双载体，precommit 之外一律 uncertain

```python
phase = getattr(error, "phase", None)
if phase is None:
    evidence = getattr(error, "evidence", None)
    if isinstance(evidence, Mapping):
        phase = evidence.get("phase")
if phase is not None and phase != "precommit":
    raise RunTreeCopybackError("OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN", ...)
raise RunTreeCopybackError("OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED", ...)
```

- 载体一：裸 `ProviderAtomicError.phase`（lock-release 路径，#1363 现状）。
- 载体二：`StateManagerError.evidence["phase"]`——
  `_write_state_index_bytes`（state_manager.py:2710-2724）重包时挂
  `evidence={"phase": error.phase}`，reason 原样保留（
  `provider_replace_uncertain`/`provider_postread_failed`/
  `provider_restored_previous` 均不在 :2712-2718 的 remap 集合内）；
  `_state_index_error`（:3137-3143）保证 `.reason`/`.evidence` 属性在场。
- **方向选择**：判「!= precommit → uncertain」而非「∈ uncertain 集合」，
  与 replay 的证明哲学同构（`scheduler_state_index_copyback_replay.py:191-197`
  MERGE_PRE_COMMIT_REFUSAL_REASONS：只有经审计的 pre-commit raise point 才
  证明 index 未改，其余一律 commit-uncertain）——未来新增 phase 自动落
  安全侧（fail-safe 方向与 replay 完全一致）。
- 无 phase（两载体都取不到，如 `_state_index_error` 的纯 index 校验
  reason：unreadable/malformed/entries_invalid 等，raise point 全在 CAS 之
  前）→ `..._FAILED`，保持既有 fail-closed 语义。
- phase 全域核查（provider_atomic.py 全部 raise 点）：`precommit`（锁/前
  置/preimage/replace 未执行）、`replace_uncertain`（:384-385 replace 后
  fsync/identity 失败；:394,:397,:413,:419 post-CAS 回读失败）、
  `release_uncertain`（:296 锁释放）、`postcommit`（:420 回滚已校验成功）。
  除 precommit 外三族的 raise point 全部位于 `os.replace` 之后——正是
  replay 注释「raise points are past os.replace, so the shared index may
  already hold the new bytes」点名排除的同一集合。

## D2 `provider_restored_previous`（phase="postcommit"）归属：uncertain

路由 `..._COMMIT_UNCERTAIN`，理由：

1. **与 replay 口径一致**（in-scope 目标"两个 operator surface 同一判读"）：
   replay 把它排除在 pre-commit allowlist 外（replay:96-98 注释点名）。
2. 回滚虽经字节校验成功（restored.sha256 == before.sha256），但新 entry
   曾在 `os.replace` 后短暂对并发读者可见；"什么都没发生"不成立。
3. 操作员的安全下一步与其余 uncertain 族相同：核 shared index entry_count
   再处置——归入 uncertain 桶不会引错方向；归入 FAILED 桶会。

消息措辞如实覆盖该形（D4），测试显式钉 `error_reason ==
"provider_restored_previous"` + destination 字节已回滚为 previous（既断
分类也断事实）。

## D3 details 形状

- 两分支 details 统一为 `{"object_key", "error", "error_reason"}`；
  `error_reason = getattr(error, "reason", None)`（两载体都有 `.reason`；
  取不到时 None → 事件 JSON 中为 null，形状稳定）。
- `..._FAILED` 分支补 `error_reason` 是 issue 点名的缺口（现只有
  `error`），runbook grep 指引因此对两支同构成立。
- 既有 lock-release 分支的 details 形状不变（原就含三键）。

## D4 uncertain 消息按 phase 泛化

现消息硬编码 lock-release 语境（"provider lock release failed after the
compare-and-swap"）。泛化为点名 phase 的单一消息，例如：
`f"State-index copyback merge may have committed; the failure arose at or
past the destination compare-and-swap (phase={phase})."`
——lock-release 用例只断 code/details（tests:657-661），消息可安全泛化；
postcommit 形被"may have committed"如实覆盖（曾提交后回滚）。注释同步改写
（现注释 :109-116 只讲 release 语境），点名双载体与 replay 口径同构。

## D5 runbook 收窄（current-production-ops.md §8.8）

- :1732-1734 代码枚举句："另含尚未分流的 `replace_uncertain` 族"删除；
  `..._FAILED` = 纯 pre-commit fail-closed。
- :1752-1757 `..._COMMIT_UNCERTAIN` bullet：`error_reason` 枚举从"一般是
  provider_lock_release_failed"扩为三族（`provider_lock_release_failed` /
  `provider_replace_uncertain` / `provider_postread_failed` /
  `provider_restored_previous`），处置流程（核 entry_count → 幂等重跑或
  replay）不变；restored_previous 补一句"destination 已回滚，entry_count
  预期不含本批"。
- :1759-1764 "尚未分流"caveat bullet 整体删除（其存在理由消失）。
- :1736-1737 「`..._COMMIT_UNCERTAIN` **另有** `details.details.error_reason`」
  改为「两个 code 的 `details.details` 均含 `error_reason`（`error` 为异常
  文本）」——D3 落地后旧句为假（fixture review P2）。
- :1853 附近 replay 侧描述不动（本来就对）；`grep replace_uncertain docs/`
  其余命中（:461-669）属 provider-refresh receipt status 语境，不动。

## D6 规格：ADDED requirement（#1193 requirement 原文不动）

`file-state-snapshot-index` 新增 requirement：自然 copyback 路径对 merge
抛出的、自述 phase 处于 destination CAS 之后（release_uncertain /
replace_uncertain / postcommit）的失败，无论载体是裸 provider 错误还是被
state-manager 重包的错误，SHALL 分类为 commit-uncertain 专用 code；只有
无 phase 或 phase=precommit 的失败保留 fail-closed code；details SHALL 携带
error_reason。场景：replace_uncertain 重包载体、postread_failed、
restored_previous 归属、pre-commit 回归、details 形状。

## Invariant Matrix（pin 的行为）

| # | 形 | 载体 | 判决 | 锚 |
|---|---|---|---|---|
| I1 | `provider_replace_uncertain`（replace 后 fsync 失败，dest 已含新 entry） | 重包 StateManagerError | `..._COMMIT_UNCERTAIN` + error_reason；**断言 dest 字节含新 entry** | tasks 2.1 |
| I2 | `provider_postread_failed`（推荐 bootstrap 形：destination 缺席、回滚分支不进入） | 重包 StateManagerError | `..._COMMIT_UNCERTAIN` + error_reason；bootstrap 形**断言 dest 字节含新 entry** | tasks 2.2 |
| I3 | `provider_restored_previous`（回滚校验成功） | 重包 StateManagerError | `..._COMMIT_UNCERTAIN` + error_reason；**断言 dest 字节已回滚** | tasks 2.3 |
| I4 | `provider_lock_release_failed`（#1363 既有） | 裸 ProviderAtomicError | `..._COMMIT_UNCERTAIN`（回归，既有用例） | tasks 2.4 |
| I5 | pre-commit 族（preimage/校验/collision/无 phase 的 index reason） | 两载体 | `..._FAILED` + error_reason 新键 | tasks 2.4/2.5 |
| I6 | runbook §8.8 | — | `..._FAILED` 判读=纯 pre-commit；uncertain 三族枚举 | tasks 1.3/3.3 |
