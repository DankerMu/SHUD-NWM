# journal-concurrent-read-integrity

## Why

共享单例 + 多线程 cohort 扇出（`scheduler.py` `DEFAULT_CONCURRENT_SUBMIT_BOUND = 4`，生产默认）下，
`FileOrchestrationJournalRepository` 的读路径有两个**叠加**的 pre-existing 缺陷，
都在 `origin/master` 上成立，都只在并发下可达，因此现有单线程测试对二者零覆盖：

- **#1595 — 写窗判据认锁不认 owner**：`file_orchestration_journal.py:4103`
  `in_write_window = self._write_lock.locked()`。`threading.Lock.locked()` 是 ownership-blind 的。
  线程 B 只因为线程 A 正持锁，就把 fingerprint 置 `None`（`:4104-4108`）、
  无条件吃下 `_cycle_rows_cache` 命中（`:4111`）。被污染的读点是
  `has_active_orchestration:569` / `has_active_pipeline:577` / `has_completed_pipeline:597` /
  `active_slurm_jobs` / `candidate_state` 这类 **submit-once 与 resume 判据**——
  错值的代价是重投/漏投，不是慢一点。
- **#1600 — inode 身份比对认不出 `os.replace`**：`packages/common/safe_fs.py:284-285`
  比 `(st_dev, st_ino)`，而 journal 的唯一耐久写出口走 `os.replace`（`safe_fs.py:118` `atomic_write_bytes_no_follow`）。
  正常并发写因此被判成 containment 故障，抛
  `SafeFilesystemError: Target file changed while being opened`。实测饱和微基准
  **37 224 次成功读中 142 次触发（约 0.38%）**。后果被 32 个 `except FileOrchestrationJournalError`
  分流成三种互不一致的结局：静默跳投（`:571`/`:579` 的 `return True`，读点在 `:569`/`:577`）、
  伪造 `"pipeline_status": "running"`（`candidate_state` → `_file_journal_blocked_candidate_state`）、
  整趟 `submission_failed`（`load_model_context` → `OrchestratorError`）。

**两条必须同 PR。** #1595 的修复让非 owner 线程在他人写窗内恢复计算 fingerprint
（`_cycle_rows_source_fingerprint`，逐文件 stat），**增加**了落进 #1600 那个
`stat`↔`open` 窗口的机会。只修 #1595 会把一个已实测存在的竞态的命中率推高，
交付一个比现状更容易炸的结果——与 F-a（#1592/#1589）同形的耦合。

## What Changes

- 给 cycle 写窗判据补 **owner 语义**，使「本线程在 cycle 写窗内」与「他人持有 `_write_lock`」可区分；
  非 owner 线程走正常 fingerprint 校验路径，与单线程语义等价。
- 让 `SafeFilesystemError` 的「inode 身份变了」成为**结构化可判别**的一种（`kind`），
  并在 journal 读路径的唯一 chokepoint 上做**有界重试**；重试耗尽仍 fail-closed。
- 不弱化 `safe_fs.py:284` 的身份比对，不删任何 containment 检查，不在共享原语内部加重试。

## Non-Goals

- **不改 `_cache_lock`**：#1380 / PR #1598 已落地，只做互斥、不改读值语义；本 change 与其正交。
- **不改 `_stat_signature` 指纹强度**（#1567，symlink 父组件跟穿）：那是「指纹算得不对」，
  本 change 的 #1595 面是「指纹根本没算」。二者同段代码、不同缺陷。
- **不让读者取 cycle flock**：能彻底消除竞态，但把无锁读路径改成加锁读路径，
  跨进程读者全部要改，代价远大于收益（#1600 issue 已列为「不推荐，列出以免被重新发明」）。
- **不修 journal 以外的 safe_fs 调用点**：本 change 只做调用点普查并留痕；
  普查中确证存在「并发写方 + 原子 replace」的其他调用点，**报告立 issue，不在本 change 修**。
- 不动 PG repository（`chain_repository.py`）：共享单例路径只发 file journal。

## Impact

- `services/orchestrator/file_orchestration_journal.py`：写窗判据、cycle 写上下文、读 chokepoint。
- `packages/common/safe_fs.py`：`SafeFilesystemError` 增加一个 `kind` 取值 + `:285` 那一处 raise 带上它。
  **这是共享原语的新增判别位，不改任何既有 kind 的语义，不改任何拒绝行为。**
- 规格：`pipeline-job-persistence`（写窗判据 + 读路径韧性）、
  `safe-filesystem-primitive-contract`（结构化判别位与其安全边界）。
