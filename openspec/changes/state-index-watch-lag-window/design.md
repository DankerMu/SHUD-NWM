# Design: state-index-watch-lag-window (#3118)

## Governing invariant

prune 窗口必须严格覆盖调度器最深的 warm-start 回看：`retention_days*24 > MAX_LOOKBACK_HOURS + cycle_lag_hours + STATE_INDEX_RETENTION_STEP_BACK_HOURS`。

watch 和 repair 必须用**同一个**函数计算 floor。watch 只能把窗口推宽到满足这个不变量，绝不能绕过它。

## 1. floor helper（state_manager）

```python
def state_index_retention_floor_hours(cycle_lag_hours: int) -> int:
    return MAX_LOOKBACK_HOURS + cycle_lag_hours + STATE_INDEX_RETENTION_STEP_BACK_HOURS

def minimum_state_index_retention_days(cycle_lag_hours: int) -> int:
    return state_index_retention_floor_hours(cycle_lag_hours) // 24 + 1   # 严格大于
```

- `_state_index_retention_window` 先保留原有的 lag/days 类型校验（错误码不变），再改用 `state_index_retention_floor_hours` 算 `floor_hours`。
- 拒绝条件仍然是 `retention_days*24 <= floor_hours`。
- 两个 helper 只接受已经校验过的非负整数 lag。调用方负责校验，helper 对非法输入不定义行为，也不会被非法输入调用。

## 2. watch 推导（scripts/node22_state_index_capacity_watch.py）

- 读 `os.getenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "").strip()`：
  - 能解析为 `int` 且 `>= 0`：`retention_days = max(DEFAULT, minimum_state_index_retention_days(lag))`。等于 DEFAULT 且 `minimum <= DEFAULT` 时，`retention_days_source = "default"`，否则为 `"derived_from_cycle_lag"`。
  - 其他情况（未设置、空串、非整数、负数）：`retention_days = DEFAULT`，`retention_days_source = "default"`。不在 watch 里报错，仍把 `cycle_lag_hours=None` 交给 repair，由 repair 读同一个 env，原样报出现有错误。所以：
    - 未设置：exit 2，`error={reason: repair_cycle_lag_unset, field: cycle_lag_hours}`；
    - 非整数：exit 2，`repair_cycle_lag_unset`，field 为 `NHMS_SCHEDULER_CYCLE_LAG_HOURS`（repair 的 `_env_cycle_lag_hours`）；
    - 负数（例如 `-1`）：exit 2，`repair_cycle_lag_unset`，`field: cycle_lag_hours`（`_state_index_retention_window`）。
    - 不会新增 exit 4 路径。
- 传给 repair 的 `cycle_lag_hours` 仍然是 `None`（保持 `tests/test_node22_state_index_capacity_watch.py:472` 的断言）。repair 会读到同一个 env 值，与 watch 的推导一致。
- `retention_days_source` 放在 receipt **顶层**，每份 receipt 都带这个 key。
  - 窗口推导发生在 import 之后、receipt 根校验之前，用字面量 21 兜底不可能出错；之后的所有 evaluated / refused receipt 都写推导值。
  - import 失败（`:59-60`，此时 `DEFAULT_STATE_INDEX_RETENTION_DAYS` 不可用）的 refused receipt 写 `null`。
  - receipt 根不安全（`:66-71`）时，推导已经完成，写推导值。各 lane 的 `retention_days` 不变（取自 repair 的 summary，等于推导值）。`schema_version` 保持 `nhms.scheduler.state_index_capacity_watch_receipt.v1`（只是兼容性新增字段）。

边界示例：

| lag | floor_hours | 结果 | source |
|---|---|---|---|
| 16 | 400 | 21 | default |
| 119 | 503 | 21 | default |
| 120 | 504 | 22 | derived_from_cycle_lag |
| 294 | 678 | 29 | derived_from_cycle_lag |

## 3. WARNING 指纹

- `_warn_state_index_capacity` 是私有函数。签名改为 `(capacity, *, index_uri)`，接收调用点原样拿到的 uri（`str | Path`）。
  - 只有在 `capacity["warning"]` 为真的分支里才计算标签。
  - 标签是 `f"{_state_index_uri_evidence(index_uri)} index_fp={_state_index_fingerprint(index_uri)}"`。
- `_state_index_fingerprint(uri) = hashlib.sha256(str(uri).encode("utf-8")).hexdigest()[:8]`：纯字符串哈希。
  - 不调用 `resolve()`，不碰文件系统，也不可能抛异常。
  - 原因：`:2201` 的告警发生在 `_write_state_index_bytes` 已经提交之后，标签计算一旦抛错，会把已经成功的 publish 报成异常；在 reader 上则会早于硬上限失败。
- 公开的只读 helper：`state_index_log_label(uri) -> str`，返回同一个标签。runbook 和测试都调用它，不另写一份。
- 三个调用点（`:1988`、`:2041`、`:2201`）改为传 `index_uri=self.index_uri` 或 `index_uri=destination_uri`。
- evidence 里的 `"index"` 值仍然用 `_state_index_uri_evidence`，保持不变。
- 指纹针对的是**调用点收到的 uri 字符串**。实现时追踪到的实际情况（2026-11-11，与本 fixture 最初的假设不同，以此为准）：
  - scheduler pass 读的 `NHMS_SCHEDULER_STATE_INDEX` 指向 **shared/destination** index；copyback 在 `:2202` 传入的是 `Path(NHMS_OBJECT_STORE_COPYBACK_ROOT).resolve() / STATE_INDEX_OBJECT_KEY`，字符串与前者相同。
  - private（repair 的 reference）index 由 `NHMS_SLURM_SCHEDULER_STATE_INDEX` 指定，只在 Slurm `state_save_qc` 作业里被读写和评估（`chain_runtime_utils.py:1067`）。
  - runbook 按调用点逐行列出 uri 原文与指纹。
- runbook 命令（node-22，精确解释器，禁止 `uv run`）：

  ```bash
  cd /scratch/frd_muziyao/NWM && /scratch/frd_muziyao/NWM/.venv/bin/python -c 'import sys; from packages.common.state_manager import state_index_log_label as f; print(f(sys.argv[1]))' "<uri>"
  ```

- 计数不变：每次 publish、每次未缓存的 load 只打一条 WARNING，缓存读取不打（`tests/test_state_index_retention.py:1067-1069`）。

### Sibling callers（都经过上述三个调用点，自动获得指纹，无需改动）

- `services/orchestrator/scheduler_core.py:76`、`:781`（scheduler private index 读）
- `scripts/scheduler_refresh/runner.py:259`、`:555`（provider refresh 读与发布）
- `scripts/node22_clone_direct_grid_cutover_states.py:452`、`:458`、`:695`
- `packages/common/state_manager.py:1940`、`:2518`（copyback 发布到 destination）、`:3018`（repair enforce 发布）

## 4. private lane 覆盖调查（只调查、只写文档）

确认 scheduler pass 读取的 `config.scheduler_state_index` 是哪份 index，以及 copyback 时 destination 与 private 各自是否经过 `_warn_state_index_capacity`。

- 如果 private lane 在 pass 中会被评估，解释为什么日志从未出现 0.84（比如两边的 json_nodes 统计口径不同）；
- 如果不会被评估，就在 runbook 8.12 写明 private lane 只靠每日 watch 覆盖。

结论写进 PR 的"调查"小节。

## Must preserve

- dry-run 语义：字面量 `enforce=False`，不加锁，不写 index、archive、repair receipt。
- `_state_index_retention_window` 的拒绝条件、错误码和 evidence 字段。
- repair CLI 的 `--retention-days` 默认值 21。
- watch 的退出码表 0/1/2/3/4、已有 alert 名、`schema_version` v1，以及传给 repair 的 `cycle_lag_hours=None`。
- 现有测试断言，包括 `tests/test_node22_state_index_capacity_watch.py:281-282`、`:472` 和 `tests/test_state_index_retention.py:1067-1069`。
- evidence、publish 结果里的 `index` 值和 `capacity` 结构。
- WARNING 保留 `run the prune-retention repair (known-issues-state-index 8.12)` 这段文字。

## Required evidence（tests）

- watch：
  - lag 16 / 119 → 21，`default`；
  - lag 120 → 22，`derived_from_cycle_lag`；
  - lag 294 → 29，`derived_from_cycle_lag`，且是 evaluated 而不是 refused；
  - 未设置 / 非整数 / `-1` → exit 2，reason 和 field 与上面列出的一致；
  - refused receipt 也带 `retention_days_source`：import 失败时为 `null`；receipt 根不安全时为推导值（lag 294 → `derived_from_cycle_lag`）。
- helper：在 lag ∈ {0, 16, 119, 120, 294} 上，`_state_index_retention_window(retention_days=minimum(lag), …)` 通过，`retention_days=minimum(lag)-1` 被拒绝，证明两者同源、边界严格。
- WARNING：
  - 两份不同 uri 的 index 产生不同的 `index_fp`；行内不含路径片段；仍含 8.12 指引；条数不变。
  - `state_index_log_label(uri)` 的输出与日志行中的标签逐字相同（runbook 命令与日志对得上）。
  - `warning=false` 时不计算标签：mock `_state_index_fingerprint`，断言它没被调用。
  - 标签不碰文件系统：直接调用 `_warn_state_index_capacity(capacity_with_warning, index_uri=...)` 和 `state_index_log_label(...)` 时 patch `Path.resolve` 让它抛 `RuntimeError`（只在这两个调用周围 patch，不做全局 patch，因为 `_write_state_index_bytes` 本身会调用 `resolve()`），断言 WARNING 仍然打出、没有抛出异常。
- 每个新行为的测试在改动前的源码上先红。
