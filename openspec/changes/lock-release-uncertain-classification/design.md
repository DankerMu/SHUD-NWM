# Design: lock-release-uncertain-classification

## 风险三角与 fixture level

- **Fixture level: compact**（issue 规模 S/M；无 DB/display 面；风险集中在异常语义与锁 fd 生命周期这一窄面）。
- 风险轴：(1) finally 段改写的异常屏蔽——释放错覆盖 body 在途异常是最危险回归；(2) 兄弟锁使用点的口径漂移——release 失败从裸 OSError 变 typed 后落入各调用方现有 except 的语义必须逐点判定；(3) oracle 完整性——"已提交"必须断言 destination 字节，不能只断言异常类型。
- Suggested level 来源：issue 元信息 S/M + implementation-ready；无偏离。

## 现状基线（issue 文本与 HEAD 的差异，fixture 撰写时核实）

issue 立案于 fecdbd35（#1189 r2）；此后 r3/r4 已交付：replay `:389` `except Exception` 广捕 + `merge_uncertain` 通道（`merge_commit_uncertain`，rc 3，摘要 + receipt，`merge_error_reason` 合成 `merge_unexpected_exception:OSError`）、`_POST_MERGE_FAILURE_PRECEDENCE`、runbook 退出码表全覆盖。故 issue「解决思路」中"replay 侧新增 reason `merge_commit_status_unknown`、`_record_committed_merge` 容许 merge=None"两项**已被现有机制等价满足**（reason 名为 `merge_commit_uncertain`；receipt merge 字段置 null 已实现）——本 change 对 replay 零代码变更，AC-2 以注入测试钉现有行为。这是对 issue 文本的记录性偏离，非范围收缩。

## 决策

### D1 — 释放段归类形态（provider_atomic.py）

`_provider_destination_file_lock` 的 `try: yield / finally: …` 重构为显式双路径，保证 body 异常优先：

```python
try:
    yield
except BaseException:
    _close_release_fds_quietly(lock_fd, acquired, parent_fd)   # 吞 OSError，不覆盖在途异常
    raise
else:
    release_error: OSError | None = None
    # fd 关闭是无条件义务：任何一步失败都先把 lock_fd/parent_fd 关完
    # 再抛归类错——泄漏 lock_fd 会让 LOCK_EX 随 open file description
    # 存活至进程退出，长驻 orchestrator 上同路径后续加锁将永久
    # provider_already_running / 挂死（P1 回归风险，B1b 钉）。
    try:
        if lock_fd is not None and acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError as error:
                release_error = error
            finally:
                try:
                    os.close(lock_fd)
                except OSError as error:
                    release_error = release_error or error
        elif lock_fd is not None:
            os.close(lock_fd)   # 未获取分支维持现状（close 失败走同一归类）
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError as error:
                release_error = release_error or error
    if release_error is not None:
        raise ProviderAtomicError("provider_lock_release_failed", phase="release_uncertain") from release_error
```

- **phase=`release_uncertain`**：语义为"锁范围内的写已完成/已提交，锁释放本身失败"——与既有 `replace_uncertain`（`provider_atomic.py:318-320`）命名对齐。reason 恒为 `provider_lock_release_failed`，flock 失败与 close 失败不分立 reason（KISS：operator 处置相同，`__cause__` 保留首个原始 errno）。
- **fd 不泄漏是硬不变式**：上述形态保证 unlock 失败仍关 lock_fd、parent_fd 恒关（首个错误保留为 cause，后续 close 错误不覆盖）。**B1b**：注入释放失败后，同进程对同一路径再次 `provider_destination_lock` 必须成功（钉无泄漏/无自死锁）。
- **body 异常优先**：body 已抛（含 pre-commit `ProviderAtomicError`、`KeyboardInterrupt`）时释放错误被静默吞掉、只保证 fd 尽力关闭——release 归类**只在 body 干净退出时**出现。屏蔽方向绝不反向（B5 钉）。
- **双故障语义变化（有意，记录在案）**：body pre-commit 异常 + 释放同时失败,现状是 finally 裸 OSError **覆盖** body 异常 → replay 归 `merge_commit_uncertain`(rc 3);D1 后 body 异常存活 → allowlist 命中 → rc 2 `refused`。方向正确（audited pre-commit raise 确实未提交）但这是 uncertain→refused 的重分类，是 #1189 契约最敏感的方向——**B5b** 在 replay 层钉 rc 2/`status=refused`/`reason=merge_failed`。Must-preserve 的"rc 语义零改动"对此双故障路径例外，其余路径不变。
- `_process_destination_lock`（线程层）释放不抛 OSError，不改。
- 影响面自动扩展：`provider_destination_lock` 的全部使用点（copyback merge、refresh 三处、raw manifest、`atomic_replace_provider_bytes` 自持锁分支）release 失败统一 typed 化——这正是 issue 推荐方案的收益点。

### D2 — 自然路径独立 code（run_tree_copyback + chain_forecast_execution）

`run_tree_copyback.py:106` 现有 except 内按 phase 分流：

```python
except (ProviderAtomicError, StateManagerError) as error:
    if isinstance(error, ProviderAtomicError) and error.phase == "release_uncertain":
        raise RunTreeCopybackError(
            "OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN",
            "State-index copyback merge may have committed; provider lock release failed after the compare-and-swap.",
            {"object_key": object_key, "error": str(error), "error_reason": error.reason},
        ) from error
    raise RunTreeCopybackError("OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED", …) from error
```

- 判据用 **phase 而非 reason**：`release_uncertain` 是"锁内写已完成"的语义位，未来同 phase 新 reason 自动归对桶；`replace_uncertain`/`provider_postread_failed` 类 merge 内部不确定态已被 state_manager 包装为 `StateManagerError`（有各自 reason），不受此分流影响——本分流只治锁释放期。
- `chain_forecast_execution.py:762` **零改动**：现有 `except RunTreeCopybackError` 事件写入器把 `error.code` 写进 details（`error_code` 字段），新 code 自动落 `object_store_copyback` event；`status_to="failed"` 保持（stage 确实失败，二分由 code 承担；不发明新 status 词汇）。
- known-residual（登记不修）：merge 抛出的**非锁释放期**裸异常（如 state_manager 内部未包装 OSError）在自然路径仍穿透零 event——pre-existing，issue 范围外；replay 侧已有广捕，自然路径广捕留给独立决策。

### D3 — replay 侧：零代码变更 + 注释同步

`provider_lock_release_failed` ∉ `MERGE_PRE_COMMIT_REFUSAL_REASONS` ⇒ typed-off-allowlist 自动走 `merge_uncertain` → rc 3、`status=merge_committed_incomplete`、`reason=merge_commit_uncertain`（stderr details 键 `error_reason`）、stdout/receipt `merge_error_reason=provider_lock_release_failed`（真实 reason 替代合成标识）、receipt `merge_commit_state=uncertain`。**allowlist 不加入该 reason**（它不是 pre-commit refusal，加入即语义错误）。注释同步范围：`scripts/scheduler_state_index_copyback_replay.py:380-388`（裸 OSError 举例改"其他未分类异常"，锁 teardown 例注明现已 typed）**及** `tests/test_scheduler_state_index_copyback_replay.py:505-507` 的同源 prose 注释（该用例 stub merge、断言不受影响，注释-only 同步不算破坏 2.7 的"既有用例零改动"——2.7 指断言与行为，注释同步显式豁免）。

### D4 — runbook 两处更新

1. `merge_commit_uncertain` bullet：off-allowlist 具名例增加 `provider_lock_release_failed`（CAS 之后 provider 锁释放失败；receipt/stdout 的 `merge_error_reason` 记该 reason），原"未分类异常 → `merge_unexpected_exception:<类型>`"例保留（语义仍真，适用于其余裸异常）。处置指引不变。退出码表结构不动。
2. **§8.8 判读入口补新 code（复审 P1）**：`:1603-1611` 的 journal 检索 grep 从单一 `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED` 扩为两码并列；判读口径 bullets 增加一条：`…_COMMIT_UNCERTAIN` = "shared index 可能已提交"——先核对 shared index `entry_count` 与 lost 方向，再幂等重跑或走 replay；不得按"未提交、直接重跑"处置。否则新 code 自身就是一个无判读入口的失败形态（重蹈本 issue 的病根）。

### D5 — 测试策略（注入式，禁真实 NFS 故障）

注入 seam：monkeypatch `packages.common.provider_atomic.fcntl.flock` 使**仅 `LOCK_UN`** 抛 `OSError(EIO)`（LOCK_EX 正常）；close 注入用 monkeypatch `os.close` 按 fd 判别或 patch 模块内引用。

- **B1（AC-1，state_manager 层）**：真实 tmp 双根 + 真实 merge，注入 LOCK_UN 失败 → `merge_state_snapshot_index_copyback` 抛 `ProviderAtomicError`，`reason=provider_lock_release_failed`、`phase=release_uncertain`，**且 destination index 字节为 merge 后新内容**（读回断言 entry 集合，钉"已提交"事实本身）。
- **B1b（fd 不泄漏钉，复审 P1）**：注入释放失败并捕获归类错后，同进程同路径再次 `provider_destination_lock`（non-blocking）必须成功获取——钉"释放失败不泄漏 lock_fd/不自死锁"。
- **B2（AC-2，replay 层）**：同注入下 `main([... --enforce])` → rc **3**；stderr JSON `status=merge_committed_incomplete`、`reason=merge_commit_uncertain`、**details `error_reason=provider_lock_release_failed`**（stderr 键是 `error_reason`——既有 pinned 形态 `tests/test_scheduler_state_index_copyback_replay.py:349`）；stdout 摘要与 receipt 的 **`merge_error_reason`**=`provider_lock_release_failed`、`merge_commit_state=uncertain`、merge 字段 null；receipt 目录落盘 receipt。反断言：绝非 rc 1 / 空 stdout / `status=refused`。
- **B3（AC-3a）**：`copyback_run_trees` 同注入（真实 merge）→ `RunTreeCopybackError.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN"` ≠ `…_FAILED`，destination 字节已提交。
- **B4（AC-3b）**：`_copyback_stage_run_trees` 的事件断言用既有 harness 模式（`tests/test_orchestration_chain.py:2903-2921`，该 harness **stub 掉 `copyback_run_trees`**，fcntl 注入到不了真实 merge）——B4 以 stub 抛 `RunTreeCopybackError(新 code)` 断言 event 存在、details `error_code` 为新 code、`status_to="failed"`；真实注入链由 B3 承担。
- **B5（屏蔽方向钉，provider 层）**：body 抛 pre-commit `ProviderAtomicError`（如 preimage_changed）且释放同时失败 → 传播的是 body 异常（reason 不变），非 release 归类。
- **B5b（双故障 replay 层，复审 P2）**：同双故障注入下 replay → rc **2**、`status=refused`、`reason=merge_failed`（uncertain→refused 的有意重分类被钉住）。
- **B6（close 注入）**：flock 释放成功、`os.close` 失败 → 同 `provider_lock_release_failed`/`release_uncertain`。**seam 纪律**：fake `os.close` 必须判别目标 fd（如 fstat dev/ino 对锁路径）且**真正关闭 fd 后再抛**——测试自身不得泄漏被测 fd，也不得让进程内无关 close 失败。
- **B7（无注入回归）**：无注入全绿由既有套件承担（四文件 + provider_atomic 相关用例零改动通过）。

### D6 — 兄弟锁点审计（AC-5，判定表）

release 失败 typed 化后各点落桶（实现后逐点验证并记录于 PR body）：

| 使用点 | 现有 except | 新归类落点（复审后具体化：outcome/reason/phase/副作用） |
|---|---|---|
| `scheduler_file_provider_refresh.py:639`（refresh 主锁） | `except ProviderAtomicError`（`:1026`；`provider_lock_release_failed` ∉ refresh `REASONS` 集） | `outcome="failed"`、`reason="provider_invalid"`、receipt `phase="release_uncertain"`（**新 phase token**，schema `:93` 自由字符串合法）+ `rollback_receipt_if_needed()` **回滚已发布 provider**。与现状 `except Exception`（`:1104`）路径等价（同 rollback、同 reason），仅 phase token 新——**非回归但有两个新事实**：novel phase token 进 receipt;release-uncertain **有意不**映射 refresh 的 `replace_uncertain` outcome 家族（那是另一语义轴,超本 issue 范围） |
| `:1320`（postcommit unlink 分支） | **实现期核实修正**：`_restore_provider_path` 自身 `:1334` except 元组已含 `OSError` 与 `ProviderAtomicError` | **零变化行**——typed 前后同落 `RefreshError("provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit")`，无 novel token |
| `:1512`（receipt publication 锁） | **实现期核实修正**：调用方 `:1126` except 元组已含两类 | **零变化行**（emergency-slot finalize 路径不变）；pre-existing 残余：第二调用方 `reconstruct_primary_receipt:1189` 无 handler，release 失败 typed 前后都以 rc 1 逃逸（超范围，登记） |
| `source_cycle_raw_manifest.py:432→:495` | `except ProviderAtomicError` → `NfsRawManifestStagingError("raw_stage_lock_failed:<reason>")`（`:431` try 确实包住 `:432` with） | 变为 `raw_stage_lock_failed:provider_lock_release_failed`（原裸 OSError 穿透至上层）——staging 已完成后的释放失败被结构化标注 |
| `atomic_replace_provider_bytes` 自持锁分支（`provider_atomic.py:292`） | 调用方各自 except `ProviderAtomicError` | replace 已成功后的释放失败 → `release_uncertain`，与 `replace_uncertain` 家族同向 |

未核查残余（记录）：node-22 operator 脚本若有对 refresh receipt `phase=="precommit"` 的 grep 假设，novel token 可能影响其筛选——实现阶段 grep `infra/`+`scripts/` 一次确认，结果记 PR body。

验收口径：四个既有测试文件全绿零改动（`test_scheduler_file_provider_refresh.py`、`test_source_cycle_raw_manifest.py`、`test_chain_repository_nfs_raw_manifest.py`、`test_state_manager.py` 既有用例），审计结论写入 PR body`偏离记录`旁的独立小节。

### 备选方案否决记录

merge 侧把释放失败降级为返回值 warning（issue 备选）：调用方证据最全，但"释放失败仍返回成功"掩盖真实 fd/NFS 故障、需要 state_manager 显式标记 CAS 成功点、其他锁使用点零受益——三点均劣于锁层归类，否决。

## Must-preserve

- 锁获取段语义逐字节不变（#1192 的面）；`blocking`/不可重入/进程内注册表行为不变。
- **fd 不泄漏**：释放失败的任何形态下 lock_fd/parent_fd 均被关闭（B1b 钉）。
- body 异常（含 pre-commit refusal、KeyboardInterrupt/SystemExit）传播优先级不变——释放错绝不覆盖。
- replay：rc 0/2/3 语义、allowlist 集合、`_POST_MERGE_FAILURE_PRECEDENCE`、receipt schema 均零改动——**唯一例外**：双故障（body pre-commit + 释放失败）从"裸 OSError 覆盖 → rc 3 uncertain"变为"body 异常存活 → rc 2 refused"，有意重分类，B5b 钉（见 D1）。
- `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED` 在非 release_uncertain 路径的语义与文本不变。
- `chain_forecast_execution` 事件写入器零改动。
- 兄弟锁点：无注入路径行为零变化。

## Seams under test

- `provider_atomic.fcntl.flock` / `os.close` monkeypatch（模块内引用，进程内注入，无真实 NFS）。
- 真实 tmp 双根 merge（state_manager 既有测试基建）。
- replay `main()` capsys/rc、receipt tmp 根（`test_scheduler_state_index_copyback_replay.py` 既有模式）。
- 事件断言 harness（`tests/test_orchestration_chain.py:2917` 模式）。

## Evidence mapping

| AC | 证据 |
|---|---|
| AC-1（merge 层可判定 + 已提交字节断言） | B1 |
| AC-2（replay rc 3 / 非 refused / 摘要 / receipt） | B2（注入钉现有机制，零代码变更为记录性偏离） |
| AC-3（自然路径独立 code + event） | B3 + B4 |
| AC-4（runbook 同步） | D4 diff + markdownlint |
| AC-5（兄弟点不回归） | D6 判定表入 PR + 四测试文件全绿 |
| AC-6（ruff + 定向 pytest） | 命令输出（文件名按仓库实际：`tests/test_state_manager.py`、`tests/test_scheduler_state_index_copyback_replay.py`、`tests/test_run_tree_copyback.py`、`tests/test_scheduler_file_provider_refresh.py` + `tests/test_orchestration_chain.py` 定向用例、`tests/test_source_cycle_raw_manifest.py`） |

## Risk packs

- 选用：`exception-semantics`（屏蔽方向、finally 改写）、`operator-evidence-contract`（rc/status/receipt 二分）、`cross-module-blast-radius`（兄弟锁点审计）。
- 未选：`db-migration`/`display-boundary`（零触面）、`concurrency`（锁获取侧不动，#1192）、`perf`（无热路径变化）。
