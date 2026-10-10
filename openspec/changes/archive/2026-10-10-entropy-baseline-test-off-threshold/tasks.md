# Tasks

行号指 `scripts/governance/write_entropy_baseline.py` 与 `tests/test_entropy_audit_baseline_writer_summary.py` 在本分支起点的位置，动手前复核。

- [x] 1.1 `test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo` 的 `cleanup_priorities` 断言，改为“三条期望 dict + 整表相等”：
  - route-token 条目（`Align current display runbooks…`）整条钉字面量：它的 `impact` 按 check_id 硬编码为 `high`、`effort` 为 `low`、`axis` 为 `context`，都不由条数派生。
  - orchestrator 条目整条钉字面量（来自常量 `STRUCTURAL_CLEANUP_PRIORITIES`）。
  - Playwright 条目（`broad-e2e-api-mock`）钉 `target`、`axis`、`effort == "medium"`；只有 `impact` 是活的：期望值取自被测模块对**同一份报告里该组 findings** 的计算结果（`_cleanup_impact` 的三个活输入——条数、组内最高 priority、组内最高 severity——都来自该组 findings；最简单是取 `_cleanup_priority_targets(该组 findings)[0]["impact"]`），不在测试里重抄阈值，也不只按条数派生（一个 live 标签的宽 mock 是 P1 / high，会在任意条数下把 `impact` 翻成 `high`）。另断言 `impact in {"medium", "high"}`。
  - 顺序：`assert cleanup_priorities == sorted(expected, key=<与 writer 相同的排序键：impact 降序、effort 升序、target>)`——整表相等同时保住“恰三条、全字段”。排序键以 `_baseline_cleanup_priorities` 的实现为准。
  - 一条不依赖被测函数的独立断言：`cleanup_priorities[0]` 等于 route-token 条目的字面量（high + low 在任何条数下都是最小键）。
- [x] 1.2 新增一条不读当前仓库的用例，入口 `write_entropy_baseline._baseline_cleanup_priorities(findings)`（它负责排序并追加 orchestrator 条目；`_cleanup_priority_targets` 不排序、不含 orchestrator，断不了顺序）。合成 finding 的最小形状 `{"check_id": "broad-e2e-api-mock", "priority": "P2", "severity": "medium"}`。两条**完整字面量列表**断言：9 条时结果为 `[orchestrator 条目, Playwright 条目(impact "medium")]`，10 条时为 `[Playwright 条目(impact "high"), orchestrator 条目]`。1.1 用被测函数算 `impact` 的期望，对 `impact` 本身没有判别力——阈值与排序键由这条用例的字面量独立钉住。
- [x] 1.3 同一条 current-repo 用例里其余断言已核对（fixture review 的只读观察），**都不动**，结论写进 PR：

  | 断言 | 当前值 | 阈值 | 余量 |
  |---|---|---|---|
  | stale-display-route-token `spread_risk == "high"` | 708 条 / 19 模块 | 条数或模块数 ≥ 10 | 698 / 9 |
  | placeholder-path-token `spread_risk == "high"` | 109 条 / 4 模块 | 条数 ≥ 10 | 99 |
  | `v1_summary_source_files > 700` | 4391 | 700 | 充足 |
  | `modules_with_high_entropy >= 2` | 2 | 常量 overlay `STRUCTURAL_HOTSPOT_MODULES` 给出的下界，不是条数阈值 | 不适用 |

  实现者复核表中数字（一次 `build_report` 观察即可）；与表不符则报告，不自行扩大改动。另记一条既有行为：“恰三条”隐含 `agent-artifact-ownership-policy` 为 0 条，出现第一条会多出第四个条目而变红——是否算合理信号由 owner 判断，本次不动。

## 约定

- Risk pack「Legacy compatibility」selected：稳定部分的判别力不得丢 -> 变异证明。
- Must preserve：同文件其他用例与 `tests/test_entropy_audit_report_contract.py` 不改通过；`scripts/**` 零 diff。改动只在 `tests/test_entropy_audit_baseline_writer_summary.py` 一个文件（取某类 findings 用 `tests/entropy_audit_helpers.py` 现成的 helper 或 `report["findings"]`，不新增 helper）。
- Non-goals：`_cleanup_impact` 的阈值与分类逻辑；刷新 `.entropy-baseline/latest.json`（不是修法）；减少前端宽 mock；其他 `test_entropy_audit_*.py` 的同类排查；Full Regression Watch 是否自动立单、`backend` filter 是否覆盖读前端树的 Python 用例（需 owner 裁定）。
- Evidence floor：
  - 先红后绿：改前 `uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 该用例红（贴断言 diff）；改后全绿。
  - 变异（只改测试 / 测试内数据，不改被测代码；逐条还原）：(a) 期望里某个 `target` 改名 -> 红；(b) 改 Playwright 条目期望的 `axis` -> 红；(c) 删掉期望里的 orchestrator 条目 -> 红；(d) 在用例里临时把 `report["findings"]` 中的 broad-mock 截到 9 条（至少留 1 条——滤光后条目消失、“恰三条”会红）再调 `build_baseline_snapshot` -> current-repo 用例仍绿（`_repo_report` 返回深拷贝，安全；不要 patch 扫描器或 `build_report`，memo 热了之后不生效还会污染缓存）；(e) 1.2 里把 9 / 10 两条列表的期望互换 -> 红。
  - `uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py tests/test_entropy_audit_report_contract.py`；`uv run ruff check tests/test_entropy_audit_baseline_writer_summary.py`；`uv run python scripts/select_ci_tests.py --changed-file <只含该测试文件的清单>` 选中它；`openspec validate entropy-baseline-test-off-threshold --strict --no-interactive`。
  - 合并后（编排者）：master 的 `Unit Tests (full)` 四片与对应的 `Full Regression Watch` run 为 success，链接贴进 issue。node-27 不适用（纯仓库文本测试，无 DB）。
