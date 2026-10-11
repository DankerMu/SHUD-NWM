# Tasks

- [x] 1.1 `scripts/node27_purge_superseded_weights.py`：`--receipt-root` 设为 `required=True`，help 去掉默认值说明；删除 `DEFAULT_RECEIPT_ROOT_KEY` 及 settings 构造里的回落分支；模块 docstring 里如有“默认”字样同步。
- [x] 1.2 `tests/test_node27_purge_superseded_weights.py`：新增用例——不传 `--receipt-root` 时以 argparse 的方式拒绝（`SystemExit` code 2），且未连接 DB、未创建任何目录；依赖默认值的既有用例（如有）改为显式传参，断言不变。integration 文件同理（只改传参）。
- [x] 1.3 `docs/runbooks/production-ops/operating-scope.md` 7.7：参数说明改为“`--receipt-root`（必填）”；示例命令已显式传参，不动。

## 约定

- Must preserve：其余参数、回执目录结构（`<receipt-root>/purge-<utc stamp>/`）、锁文件位置、拒绝文案；`scripts/node27_retire_basin.py` 与 `packages/common/succession_receipt.py` 的同名常量不动。
- Non-goals：#2773 其余三项；给 retire 工具改默认值。
- Evidence floor：
  - `uv run pytest -q tests/test_node27_purge_superseded_weights.py`（新用例先在未改代码上红）；`uv run ruff check scripts/node27_purge_superseded_weights.py tests/test_node27_purge_superseded_weights.py`。
  - `git grep -n "scheduler/weight-purge" -- . ':!openspec/changes/archive'` 只剩本 change 的说明文字。
  - `uv run python scripts/select_ci_tests.py` 相关路由若有针对该脚本的规则，确认新测试被选中（只核对，不改 selector）。
  - `openspec validate purge-receipt-root-required --strict --no-interactive`。
  - node-27 不需要 receipt：不改库表与运行行为；下一次真实使用（最早 2026-11-07 的重跑）本来就显式传参。
