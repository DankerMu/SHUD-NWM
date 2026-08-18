## 1. Implementation

- [ ] 1.1 `reconcile.py:382-383`：`page_start.astimezone().strftime(...)` /
      `page_end.astimezone().strftime(...)` + 一行注释（sacct 本地解释口径，
      对齐 real_backend.py:466 措辞）；`_pages()` 与 page_key/session 冻结零改动

## 2. Tests

- [ ] 2.1 TZ 三档字面串钉测（`tests/test_gateway_reconcile.py`，形态照抄
      `tests/test_real_slurm_gateway.py:3455-3516`；`_pinned_local_timezone` 若为
      test_real_slurm_gateway 模块私有则在本文件复制同款 contextmanager 并注明出处，
      不跨文件 import 私名）：pinned now，断言最新页 `--endtime=` 与最老页
      `--starttime=` 完整字面串；期望值写死
- [ ] 2.2 `:10302` 期望串迁移 + 该用例 pin TZ；`:10290` scope_pages/commands 计数
      断言保持（session 冻结 + page_key 去重不回归）
- [ ] 2.3 既有 `tests/test_gateway_reconcile.py` 其余用例零改动全绿

## 3. Verification

- [ ] 3.1 红证：非 UTC 档改动前红（渲染 UTC 墙钟而非本地墙钟）；UTC 档改动前后
      逐字同（等价锁）
- [ ] 3.2 uv run pytest -q tests/test_gateway_reconcile.py
- [ ] 3.3 uv run ruff check .（untracked 投影 E501 既知例外照会话惯例记录）
- [ ] 3.4 openspec validate reconcile-sacct-window-local-wallclock --strict --no-interactive
- [ ] 3.5 merge 后 node-27 oracle receipt：3.2 套件，记入 #1559；并在 #1116 留
      前提解除注记（node-22 实机 sacct 对照仍为 #1559 的 p1 升级判据，记 issue 不阻 PR）
