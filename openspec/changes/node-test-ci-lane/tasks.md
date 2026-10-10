# Tasks

- [x] 1.1 `ci.yml`：`changes` job 新增 output（名字由实现者按既有命名风格定），filter 含 `scripts/**/*.mjs`、`scripts/__tests__/**`、`.github/workflows/ci.yml`；新增 job（以 `openapi-validate` 为模板：`needs: changes`、按新 output 门控、checkout、`actions/setup-node@v4` Node 20、`timeout-minutes: 5`），steps 恰为 checkout、setup-node、单条 run；run 先 `shopt -s failglob` 再执行 `node --test scripts/__tests__/*.test.mjs`（glob 零匹配时 bash 直接非 0 退出、node 不被调用——否则 Node 22+ 会把字面量当 pattern、报 `tests 0` exit 0 的零用例假绿）；run 里不含 `npm` / `pnpm` / `npx` / `install`。job 显示名不以 `Unit Tests` 开头（`scripts/ci/full_regression_watch.py` 按该前缀认全量分片）。
- [x] 1.2 pytest 元守卫，放进 `tests/test_ci_shard_tests.py`（已有同类的 job 结构守卫，且已被 `scripts/select_ci_tests.py` 里 `.github/workflows/ci.yml` 的规则路由；不新建测试文件、不改 selector 及其断言）：断言新 job 存在且受新 output 门控；filter 含三条路径；run 命令以 `node --test` 调用、参数是未加引号的 `scripts/__tests__/*.test.mjs`（不是裸目录、不是加引号的 glob），且之前有 `shopt -s failglob`；steps 恰为三步、run 不含安装命令；显示名不以 `Unit Tests` 开头。
- [x] 1.3 文档：`docs/runbooks/display-mobile-evidence.md` 的“不在 CI 里”改为新车道的名字与触发条件；根指令「CI 门控要点」**不加**新条目（裁定：那一节只列读结果时必须知道的要点，这条车道自解释；且改 `instructions/agents/shared.md` 会拉起后端定向车道）。

## 约定

- Risk pack「Config / project setup」selected：-> 1.2 的元守卫 + PR 上新 job 的实际运行结果。`scripts/select_ci_tests.py` 与 `tests/test_select_ci_tests.py` 不改（守卫落在已被路由的文件里）。
- Risk pack「Documentation」selected：-> 1.3；markdownlint 通过。
- 未选：Public API / entry（不改脚本）、File IO、Schema、Auth、Concurrency、Resource limits、Legacy compatibility、Error handling、Release / dependency（零依赖）。
- Must preserve：既有全部 job 的触发条件与显示名不变；既有读 `ci.yml` 的 pytest 元测试（`tests/test_ci_workflow_locked_install.py`、`tests/test_select_ci_tests.py`、`tests/test_full_regression_watch.py`、`tests/test_ci_shard_tests.py` 等）不改期望值通过；`frontend` filter 不动。
- Non-goals：修改 `scripts/node27_display_v2_browser_evidence.mjs` 或其测试；actionlint；把这套测试并进前端车道；升级 CI 的 Node 版本。
- Evidence floor：
  - `node --test scripts/__tests__/*.test.mjs` 在 Node 20 与本地默认 Node 上各一次，报告用例数与退出码（当前 59）。
  - 判别力：把入口守卫临时改成恒假（或让任一用例失败）时同一命令 exit 非 0；还原后按 sha256 核对。只在本地做，不为此多推 CI。
  - 元守卫先红后绿：对未改的 `ci.yml` 红；变异（去掉门控条件 / 命令改成裸目录 / 命令改成加引号的 glob / 去掉 `shopt -s failglob` / filter 去掉 `ci.yml` 自身 / 加一步 install）逐条变红。
  - 零匹配：在一个空目录里用同一 run 片段（`shopt -s failglob` + 同形式 glob），Node 20 与本地默认 Node 上都非 0 退出。
  - 读 `ci.yml` 的既有 pytest 元测试全绿；`uv run ruff check .`；markdownlint；`openspec validate node-test-ci-lane --strict --no-interactive`。
  - PR 上：新 job 运行并通过，日志里有用例数（Node 20 非 TTY 输出 TAP 的 `# tests N`，更新的版本输出 `ℹ tests N`，两种都算）；其余车道行为不变。
