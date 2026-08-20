# Tasks: copyback-cas-uncertain-classification

## 1. 实现

- [x] 1.1 `run_tree_copyback.py` except 判据改为 phase 自述 + 双载体
      （design D1 代码形）：phase 在场且 != "precommit" →
      `..._COMMIT_UNCERTAIN`；否则 `..._FAILED`。注释重写：双载体、
      与 replay pre-commit-proof 哲学同构、postcommit 归属（D2）。
- [x] 1.2 details 两分支统一含 `error_reason`（D3）；uncertain 消息按
      phase 泛化（D4）；`tests/test_orchestration_chain.py:2946-2948` stub
      中硬编码的旧 lock-release 消息文案顺手同步（该测试只断
      code/error_reason，泛化不破测试，但不留脱节字面量）。
- [x] 1.3 runbook §8.8 收窄（D5）：`..._FAILED` = 纯 pre-commit；
      "尚未分流" caveat 删除；uncertain error_reason 三族枚举 +
      restored_previous 的 entry_count 预期说明；:1736-1737 「`..._COMMIT_
      UNCERTAIN` 另有 error_reason」改为「两个 code 的 `details.details`
      均含 `error_reason`」（fixture review P2——否则违反本 change 自己的
      requirement "key on the reason under either code"）。

## 2. 测试（tests/test_run_tree_copyback.py；镜像 B3 形态，注入式，先红后绿）

- [x] 2.1 I1 主锚（红-绿）：注入 destination CAS 在 `os.replace` 之后失败
      （真实写入后抛 `SafeFilesystemError(kind="indeterminate")` →
      `provider_replace_uncertain` 重包形）→ code ==
      `..._COMMIT_UNCERTAIN` 且 != `..._FAILED`，
      `details["error_reason"] == "provider_replace_uncertain"`，
      **断言 destination index 字节确实已含新 entry**（断"已提交"事实）。
- [x] 2.2 I2：`provider_postread_failed` 一例。**推荐 bootstrap 形**
      （fixture review P1-1）：destination index 不存在（
      `previous_content is None`，provider_atomic.py:397 路径）+ post-CAS
      `capture_provider_preimage` 抛 OSError。seam 必须**按路径过滤**（只
      对 destination index 生效，否则打到 source 侧 read_provider_snapshot）
      且**只在 CAS 写入之后武装**：包一层 `atomic_write_bytes_no_follow`，
      其正常返回后置标志位，patched capture 仅在标志位已置且 path 为
      destination index 时抛（destination path 上的 capture 依次为
      :131 → :361(before) → :387(after)，要打的是第 3 次；打在 :361 会让
      OSError 逃出 run_tree_copyback.py:106 的 except 元组）→ 回滚分支不
      进入，destination 必然持有新字节——code + error_reason +
      destination 含新 entry 三断言确定成立。
      若 bootstrap 形在 merge 路径不可达，退用「回滚也失败」形，但 seam
      必须让回滚那次 `atomic_write_bytes_no_follow` 在 `os.replace` 前抛
      非-indeterminate `SafeFilesystemError`（replaced=False）以保证
      destination 仍是新字节，并在测试注释里写明该约束。
- [x] 2.3 I3：`provider_restored_previous`（回读失败、回滚校验成功）→
      `..._COMMIT_UNCERTAIN` + error_reason；**断言 destination 字节已
      回滚为 previous**（分类与事实都钉）。
- [x] 2.4 回归：#1363 lock-release 用例（error_reason ==
      "provider_lock_release_failed"）与既有 pre-commit `..._FAILED` 用例
      全绿；pre-commit 用例如断言 details 形状需补 `error_reason` 键的，
      按新形状更新且不放宽 code 断言。
- [x] 2.5 I5 补锚：无 phase 的 StateManagerError（index 校验类 reason，
      raise point 在 CAS 前）仍落 `..._FAILED` 且 details 含
      error_reason（值为该 reason）。
- [x] 2.6 I5 第二载体（fixture review P1-2，`!= "precommit"` 半边的唯一
      锚）：注入 **phase = "precommit" 的重包 StateManagerError**（让
      destination CAS 的 `atomic_write_bytes_no_follow` 抛非-indeterminate
      `SafeFilesystemError` → `provider_replace_failed`/phase=precommit）→
      必须仍落 `..._FAILED`、`details["error_reason"] ==
      "provider_replace_failed"`（该 reason **不在** state_manager.py:
      2711-2717 remap 集合内，原样保留；若想额外覆盖 remap 形另注入
      `provider_destination_unreadable` → error_reason ==
      "state_snapshot_index_write_failed"）、destination 字节不变。修正
      期望值而非放宽断言（不得改成 `in details` 之类）。防止
      判据被写成 `if phase is not None:` 后全套测试照绿（那会把 replay 判
      refusal 的 reason 反向翻成 uncertain，重造两 surface 相反判读）。

## 3. 验证（Evidence Floor）

- [x] 3.1 `uv run pytest -q tests/test_run_tree_copyback.py` 通过。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `uv run npx markdownlint-cli2 docs/runbooks/current-production-ops.md`
      通过；runbook 判读口径与实现逐句对读。
- [x] 3.4 `openspec validate copyback-cas-uncertain-classification --strict
      --no-interactive` 通过。
