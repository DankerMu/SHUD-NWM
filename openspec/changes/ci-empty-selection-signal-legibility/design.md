# Design: ci-empty-selection-signal-legibility

## 风险三角与 fixture level

- **Fixture level: compact**（issue 建议规模 S；无运行时代码、无 DB/display 面、无调度面；改动面是 CI workflow 文本 + 一个选择器脚本的告警路径 + 测试）。
- 风险轴：CI 门信号的**语义诚实性**（本 change 的目的即修复它）；最大回归风险是把正常分支（count!=0）的行为或 check 名改坏，以及告警文本污染 `$GITHUB_OUTPUT` 解析。
- Suggested fixture level 来源：issue 元信息（S/C 路线）；无偏离。

## 决策

### D1 — else 分支的响亮化形态（ci.yml）

`count == 0` 时：

```yaml
else
  echo "::warning title=Unit Tests executed 0 assertions::select_ci_tests.py mapped no test files for this diff; collect-only smoke verifies imports/syntax only (issue #1182)"
  {
    echo "## Unit Tests: collect-only smoke (0 assertions executed)"
    echo ""
    echo "select_ci_tests.py selected no backend test files for this PR diff."
    echo "The suite was only collected (import/syntax check); no test assertions ran."
  } >> "$GITHUB_STEP_SUMMARY"
  if pytest tests/ -q --collect-only > collect-only.log 2>&1; then
    tail -n 5 collect-only.log
  else
    cat collect-only.log
    exit 1
  fi
fi
```

- `>/dev/null` 移除但**不裸打全量**：`-q --collect-only` 实测输出 12,380 行（每 test 一行 node id）；重定向到文件，成功打 tail（含 `N tests collected` 统计行），失败打全文并显式 `exit 1`。**禁用 `pytest | tail` 管道形态**——`run:` 默认 shell 是 `bash -e {0}` 无 `pipefail`，管道会把 collect 失败吞成绿，削弱该分支仅有的门控价值。
- 不拆分 check 名（issue C 给的两个形态之一）：拆 job 会动 `changes`→`unit-test-targeted` 的依赖拓扑与 branch-protection 心智；可辨识性由 run 页 checks-tab 的 warning 注解 + step summary 达成（`::warning` 无 `file=`/`line=` 不会出现在 Files-changed 页，AC-1 的"可辨识"只锚定实际渲染面），KISS。
- 退出语义不变：collect 失败（import/语法坏）仍红——这是该分支现有的全部门控价值，保留。

### D2 — 选择器 stale-target 告警（select_ci_tests.py）

`select_tests()` 末段现为一行静默过滤。改为：

```python
selected_paths = sorted(selected)
missing = [p for p in selected_paths if not _test_target_exists(p, repo_root=repo_root)]
for path in missing:
    message = f"selected test target does not exist and was dropped: {path}"
    print(f"select_ci_tests: WARNING: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Stale CI test-rule target::{message}")
return [p for p in selected_paths if p not in set(missing)]
```

- stderr 恒发（本地/CI 都有人读面）；`::warning` 注解走 stdout 且**仅当 `GITHUB_ACTIONS=true`**。
- **stdout 污染核查（修正）**：`main()` **会**向 stdout 打印选中的 test 列表（`:500-501` `for test in tests: print(test)`），且仓库有文档化的命令替换用法 `pytest -q $(python scripts/select_ci_tests.py --base-ref master)`（本地/远端手工跑）。`GITHUB_ACTIONS=true` 门保证：命令替换场景（本地，env 不设）stdout 永不混入 warning 行；CI 场景 ci.yml `:244` 用 `--github-output` 传数据、不捕获 stdout，warning 行只被 runner 解析为注解。模块需补 `import os`。
- 返回值语义、函数签名不变（issue Verification 脚本直接调 `select_tests`，必须保持兼容）。
- 不 fail：目标不存在时"至少告警"是 AC-4 的原文；升级为硬错属 B 路线轴。
- **顺带清理已实存的 stale 目标**：`scripts/select_ci_tests.py:216` `workers/forcing_producer/**` 规则含 `tests/test_worker_chain_smoke.py`（树中不存在，全规则集唯一死目标，当前被静默丢弃）。删除该行=零行为变更纯卫生；不删则本 change 落地后每个 forcing-producer PR 都带永久无主注解。同时把既有 meta-guard `test_every_pinned_node_id_resolves_to_an_existing_test_function`（`tests/test_select_ci_tests.py:545`，现只覆盖 `::` 限定 node id）扩展到文件级目标——AC-4 从运行时告警升级为规则集门（运行时告警仍保留：test 文件在普通 PR 中被重命名时，selector 测试未必被选中运行）。

### D3 — 测试策略

`tests/test_select_ci_tests.py` 新增：

- **B1-B6 空选择 pin**（6 类，issue 表 + AC-2 列表）：`schemas/x.schema.json`、`infra/nginx/site.conf`、`openspec/tools/x.py`、`apps/frontend/scripts/gen.py`（两条非五前缀 `.py`）、`packages/common/sql/x.sql`、`tests/fixtures/sample.json`、`scripts/run_x.sh` → `select_tests(...) == []`。断言语义：**路线 C 下空选择是已知且被响亮标注的契约**，测试名/注释点明这是 pin 不是 endorse；`.sh` 类的翻转主是 **#1138**（独立于 A/B 轴），其余类由 A/B 决策翻转。
- **B7 stale-target 告警**：构造 tmp repo_root，使一条规则命中但目标文件不存在 → 返回值不含该目标 + capsys 捕获 stderr 含 `WARNING` 与目标路径；`GITHUB_ACTIONS=true` 时 stdout 含 `::warning`（monkeypatch env）。
- **B8 无丢弃时零告警**：选一条全目标实存的规则输入（如 `db/README.md` → `tests/test_migrations.py`）断言 stderr 无 WARNING；**不得**用 `workers/forcing_producer/**` 类输入（清理前含死目标；清理后可换用，但显式选择实存目标输入使该测试不依赖 1.2b 的清理顺序）。
- **B9 meta-guard 扩展**：`test_every_pinned_node_id_resolves_to_an_existing_test_function` 覆盖文件级规则目标——规则集中任何不存在的 test 目标即测试失败。
- 现有 32 用例中，除 meta-guard 扩展（B9 改既有测试）外零改动全绿（must-preserve；22 是 issue 撰写时的旧计数，PR #1238 后为 32）。

### D4 — 指令源与投影

**AC-6 的条件判定**：路线 C 下 `instructions/agents/shared.md:166` 现句（"降级为 collect-only 冒烟——只验 import/语法，不执行任何断言"）仍字面为真，**无冲突需解决**——AC-6 按条件款记 N/A。仍做一处一句话增量（living doc 准确性，成本一行）：句尾补"该分支带 warning 注解 + step summary 显式标注 0 断言"。**投影为手工程序**（无生成脚本；`CLAUDE.md:1-5` 头注归属 `project-instruction-bootstrap` skill）：对 `shared.md` 与两个投影产物（`CLAUDE.md`、`AGENTS.md`）应用逐字相同的增量，然后 `grep -n` 三文件核验该句逐字一致。

## Must-preserve

- `count != 0` 正常分支：命令、行为、输出零改动。
- check 名 "Unit Tests" 不变；collect-only 分支绿/红语义不变。
- `select_tests()` 签名与返回值语义不变；现有 32 用例除 B9 meta-guard 扩展外零改动通过。
- 本地命令替换用法 `pytest -q $(python scripts/select_ci_tests.py --base-ref master)` 不被 warning 行污染（`GITHUB_ACTIONS` 门保证）。
- `$GITHUB_OUTPUT` 写入路径不经 stdout，告警不得混入 output 解析。
- CI 成本：新增 0 runner 分钟。

## Seams under test

- `select_tests(changed, repo_root=…)` 纯函数 seam（既有）。
- 告警面 seam：capsys/monkeypatch(`GITHUB_ACTIONS`)。
- ci.yml 无可执行测试 seam——以 YAML 结构核查（`yq`/python yaml 解析 else 分支包含 `::warning` 与 `GITHUB_STEP_SUMMARY`、不含 `>/dev/null`）+ merge 前 AC-5 实机 run 补证。

## Evidence mapping

| AC | 证据 |
|---|---|
| AC-1（路线 C：collect-only 可辨识 + PR 声明路线与理由） | ci.yml diff + proposal 决策段 + PR body 路线声明 |
| AC-2（6 类空选择用例） | B1-B6 pytest 绿 |
| AC-3（A 路线 floor 耗时实测） | N/A——未选 A，PR 记录 |
| AC-4（stale-target 告警 + 规则集门） | B7/B8/B9 pytest 绿 + D2 diff（含 `:216` 死目标清理） |
| AC-5（实机 Actions run） | **merge 前**：从 feature 分支再切 probe 分支加一行 `schemas/**` 改动，开试探 PR（`pull_request` 跑的是 PR merge ref 上的新 workflow 定义，else 新分支可在 merge 前实机验证），采 Actions run 链接显示 warning 注解 + step summary，关闭不合并，证据入 PR body 并贴 #1182 |
| AC-6（指令源/投影同步） | 条件款 N/A（路线 C 无措辞冲突）+ 自愿一句话增量的三文件一致性核验（D4） |
| AC-7（ruff） | `uv run ruff check .` |

## Risk packs

- 选用：`ci-config`（workflow 语法/语义回归——YAML 解析核查 + 实机 run）、`test-oracle-integrity`（新用例是 pin 不是削弱）。
- 未选：`db-migration`/`display-boundary`/`concurrency`（零触面）；`perf`（0 runner 分钟增量）。

## Non-goals（重申）

A/B 路线、#1138 映射、PR 全量 unit-test、draft-ready 门控。
