## 1. Implementation

- [x] 1.1 `reconcile.py:382-383`：`page_start.astimezone().strftime(...)` /
      `page_end.astimezone().strftime(...)` + 一行注释（sacct 本地解释口径，
      对齐 services/slurm_gateway/real_backend.py:466-468（PR #1558 已合并版）措辞）；`_pages()` 与 page_key/session 冻结零改动

## 2. Tests

- [x] 2.1 TZ 三档字面串钉测（`tests/test_gateway_reconcile.py`，形态照抄
      `tests/test_real_slurm_gateway.py:3451-3516`；`_pinned_local_timezone` 在
      `:3382`，**直接跨文件 import**——仓内测试跨文件引私名是既有惯例
      （test_production_slurm_validation.py:13 等四处先例）；若 import 引入不可
      接受的模块加载/CI selector 耦合则复制并注明理由）：pinned now，断言最新页 `--endtime=` 与最老页
      `--starttime=` 完整字面串；期望值写死
- [x] 2.2 `:10302` 期望串迁移 + 该用例 pin TZ；`:10290` scope_pages/commands 计数
      断言保持（session 冻结 + page_key 去重不回归）
- [x] 2.3 既有 `tests/test_gateway_reconcile.py` 其余用例零改动全绿

## 3. Verification

- [x] 3.1 红证：非 UTC 档改动前红（渲染 UTC 墙钟而非本地墙钟）；UTC 档改动前后
      逐字同（等价锁）
- [x] 3.2 uv run pytest -q tests/test_gateway_reconcile.py
- [x] 3.3 uv run ruff check .（untracked 投影 E501 既知例外照会话惯例记录）
- [x] 3.4 openspec validate reconcile-sacct-window-local-wallclock --strict --no-interactive
- [x] 3.5 merge 后双义务：(a) node-27 oracle receipt（3.2 套件）记入 #1559；
      (b) **具名 post-merge 义务**——node-22 30s 实机 sacct 对照（同裸串下
      `sacct --starttime=<裸本地串>` vs `TZ=UTC sacct ...` 行数/区间应不同），
      作为前提唯一实证记入 #1559；并在 #1116 留前提解除注记
