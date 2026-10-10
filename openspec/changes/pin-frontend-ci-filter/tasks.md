# Tasks

- [x] 1.1 `tests/test_select_ci_tests.py`：参数化三条精确字面量 `'.github/workflows/ci.yml'`、`'apps/frontend/**'`、`'openapi/**'`，断言各自作为条目出现在 `_frontend_filter_block()` 返回的块内（块级切片，不是全文件 grep）。
- [x] 1.2 反向用例（照同文件 `backend` 块 `..._gate_reds_when_the_path_leaves_the_backend_block` 的写法，对构造的 workflow 文本，不改被跟踪的 `ci.yml`）：(a) 从 `frontend` 块删掉条目、(b) 把条目挪到另一个 filter 块下——两种情况下 1.1 用的同一判定都判为“不在 `frontend` 块内”。若 `_frontend_filter_block()` 只能读被跟踪的文件，按 `backend` 块钉的既有做法给它加可选的文本参数或抽一个纯函数，行为不变。

## 约定

- Must preserve：文件内既有用例不改通过；`ci.yml` 无 diff。
- Evidence floor：`uv run pytest -q tests/test_select_ci_tests.py -k "frontend"` 通过并贴出新用例名；手工变异一次（临时在工作树里把 `ci.yml` 的 `frontend` 块那条 `.github/workflows/ci.yml` 删掉 -> 1.1 红；`git checkout -- .github/workflows/ci.yml` 还原并确认无 diff）；`uv run ruff check tests/test_select_ci_tests.py`；`openspec validate pin-frontend-ci-filter --strict --no-interactive`。node-27 不适用（纯文本元测试，无 DB）。
