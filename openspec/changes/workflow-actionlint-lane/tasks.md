# Tasks

- [x] 1.1 `ci.yml`：`changes` job 的 `outputs` 加 `workflows`，paths-filter 加
  ```yaml
  workflows:
    - '.github/workflows/**'
  ```
  新增 job `workflow-lint`（name `Workflow Lint`，`needs: changes`，`if: needs.changes.outputs.workflows == 'true'`，`runs-on: ubuntu-latest`，`timeout-minutes: 5`，steps：`actions/checkout@v4` + `uses: docker://rhysd/actionlint:1.7.7`，`with: args: -color`）。位置与写法仿 `script-node-tests`。不改任何既有 job 的 `needs` / `if`。
- [x] 1.2 元测试，放在 `tests/test_ci_shard_tests.py`（#2856 的先例在该文件 `:427-455`，用 `yaml.safe_load` 解析 `ci.yml`），YAML 级断言：
  - `jobs["workflow-lint"]["if"] == "needs.changes.outputs.workflows == 'true'"`（精确相等）、`needs` 为 `changes`、`jobs["changes"]["outputs"]["workflows"]` 存在；
  - 解析后的 paths-filter `filters` 里 `workflows == ['.github/workflows/**']`；
  - 没有任何 job 的 `needs` 含 `workflow-lint`；
  - 反向（对构造的文本，不改被跟踪文件）：删掉 filter 条目 / 把条目挪到更靠前的另一个 filter 块（如 `docs`）/ 去掉 output 声明 / 给另一个 job 加 `needs: workflow-lint`，上面的断言各自失败。断言逻辑抽成一个接收 workflow 文本的 helper，正反共用。
  - 不在 `tests/test_select_ci_tests.py` 里加块级文本钉（那里的 `_frontend_filter_block` 写死了 key，`_NEXT_FILTER_KEY_LINE` 对最后一个 filter 块会切过界）。
- [x] 1.3（fixture review 核对后预期为空操作）既有的 workflow 元测试（`tests/test_ci_workflow_locked_install.py`、`tests/test_governance_workflow_hard_gate.py`、`tests/test_select_ci_tests.py` 里枚举 job / filter 的用例、`scripts/ci/` 下读取 `ci.yml` 的脚本及其测试）若对 job 或 filter 的全集做了断言，按新增的一项更新；不放宽任何既有断言。
- [x] 1.4 指令文件：读 `CLAUDE.md` 的“CI 门控要点”与其源 `instructions/agents/`；该节是“读结果必须知道的五条”而不是车道全集，默认不改。只有当某一条因本改动变得不实时才改源并重新生成。

## 约定

- Risk pack「CI gating」selected：新车道不成为任何 job 的 `needs`；filter 条目丢失或挪位时元测试红 -> 1.2。
- Must preserve：其余 filter 与 job 逐字不变；`governance.yml` 不动；不引入需要 token 或写权限的 step。
- Non-goals：修 actionlint 之外的 workflow 风格问题；给其它 workflow 文件加触发条件；把 actionlint 接进本地 pre-commit。
- Evidence floor：
  - 本地跑 actionlint 1.7.7（`docker run --rm -v "$PWD":/repo -w /repo rhysd/actionlint:1.7.7 -color`，没有 docker 时用官方 release 二进制放 scratchpad，不装到系统）：改后 5 个 workflow 0 发现；反例：`git worktree` 不用——在 scratchpad 里放一份带 `.git` 的最小副本（`git init` 后拷入 `.github/workflows/`），或对副本显式传文件路径；把某个 `needs:` 指向不存在的 job，断言 actionlint 的输出里出现针对该 job 名的发现文本（只看非零退出不够——找不到项目根也会非零）。
  - `uv run pytest -q tests/test_ci_shard_tests.py tests/test_select_ci_tests.py tests/test_ci_workflow_locked_install.py tests/test_governance_workflow_hard_gate.py tests/test_floating_resolve_report.py`；`uv run ruff check tests/test_ci_shard_tests.py`；确认 `scripts/select_ci_tests.py` 对只改 `ci.yml` 的 PR 会选中 `tests/test_ci_shard_tests.py`（不选中则报告，不改 selector）。
  - 本 PR 改了 `ci.yml`：PR 的 CI 上 `Workflow Lint` 实际执行并通过（编排者核对）；同时 `Frontend Build`、`Frontend E2E (mocked)`、`Script Node Tests`、定向 `Unit Tests` 因各自 filter 含 `ci.yml` 也会跑，须全绿。
  - `openspec validate workflow-actionlint-lane --strict --no-interactive`。
