# Tasks: ci-selector-refresh-env-owner-suite (#2195)

Fixture level: compact
Change surface:
- `scripts/select_ci_tests.py`：一条 path-exact `PathTestRule`，放在 `infra/env/README.md` 行（`:2325`）之后、#1684 组管辖行之外，带 `#2195` 注释
- `tests/test_select_ci_tests.py`：一条精确 2 元集钉 + 其 red leg（monkeypatch 掉新规则）+ 一条 flags 钉（`stop_on_match` / `only_when_any_changed` 均为假）+ Scenario 2 三个兄弟模板的精确集钉 + 一条 reader 链接守卫
Must preserve:
- `:2247` write_roles glob、`:2302` `infra/env/**` 行、`:2305-2325` #1684 rollout-producer 组零改动；#2188 的 refresh 家族块及其注释零改动（改前 `:2423-2448`，因本行插入在其上方而整体后移 22 行，改后 `:2445-2470`）
- `tests/test_select_ci_tests.py` 的 #1684 两条用例（`test_rollout_owner_producers_select_the_static_deployment_contract` 与其 red leg `test_rollout_owner_producer_rules_red_when_removed`）、`NODE27_UNIT_OWNER_SUITES`（#2180 起）与 `NODE22_UNIT_OWNER_SUITES`（#2188）两张 owner 表与元测试、duplicate-pattern / stop-on-match / 目标存在性守卫照旧绿
- 其余 13 个 `infra/env/*.example` 选择结果不变；`.github/workflows/ci.yml`、`infra/env/**`、`infra/systemd/**` 零改动
Must add/change:
- 一条规则行 + `#2195` 注释；一条精确相等元测试及其 red leg；一条 flags 钉；Scenario 2 兄弟模板精确集钉；reader 链接守卫
Seams under test:
- `select_tests(["infra/env/compute.scheduler-provider-refresh.env.example"], repo_root=Path("."))` 恰为 `{tests/test_scheduler_file_provider_refresh.py, tests/test_two_node_docker_runtime.py}`
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门的选择入口；精确相等钉 + red leg 钉住该路径输出，既有规则用例全绿
- File IO / path safety / overwrite: not selected - 只读规则表，无写入
- Schema / columns / units / field names: not selected - 无数据格式
- Legacy compatibility / examples: not selected - env 模板本身不动
- Other packs: not selected - 无 auth/并发/发布/迁移面
Required evidence:
- 新钉命名为 `test_refresh_env_template_selects_exactly_its_owner_and_runtime_suites`，使 `uv run pytest -q tests/test_select_ci_tests.py -k "refresh_env"` 恰好选中它；加规则前红（实际集少了 owner suite），加规则后绿，红→绿输出贴 PR body
- red leg：用 `monkeypatch.setattr(select_ci_tests, "PATH_TEST_RULES", mutant)` 去掉新规则后该钉必须红（手法照 #1684 的 red leg `test_rollout_owner_producer_rules_red_when_removed`）
- 清单实测贴 PR body：`printf 'infra/env/compute.scheduler-provider-refresh.env.example\n' | uv run python scripts/select_ci_tests.py` 恰两行；并对**全部 14 个** `infra/env/*.example` 逐个跑选择器，与改前基线逐行 diff——本模板多出 owner suite，其余 13 个字节相同（改前基线：`for f in infra/env/*.example; do printf '%s => ' "$f"; printf '%s\n' "$f" | uv run python scripts/select_ci_tests.py | tr '\n' ' '; echo; done`）
- `uv run pytest -q tests/test_select_ci_tests.py tests/test_scheduler_file_provider_refresh.py` 全绿
- `uv run ruff check .`；`openspec validate ci-selector-refresh-env-owner-suite --strict --no-interactive`；`git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/**`
Non-goals:
- 不改 ci.yml、不动 env 模板与任何 suite、不放宽 `:2247` 的 write_roles glob、不给新行加 write_roles 目标、不给其余 13 个模板加行、不碰 #2185 / #2191

## 1. 选择规则

- [x] 1.1 `scripts/select_ci_tests.py` 在 `infra/env/README.md` 行（`:2322-2325`）之后、`scripts/validate_two_node_docker_runtime.py` 行之前新增 `infra/env/compute.scheduler-provider-refresh.env.example` → `("tests/test_scheduler_file_provider_refresh.py",)`。该位置在 env 邻域内、且落在 `:2305-2309` 的 #1684 注释管辖行之外，因此不触动任何既有块；**不得**插进 #2188 的 refresh 家族块（改前 `:2423` 起），那会证伪该块自称「these two rows are systemd units」与「the wrapper run resumes just below」的注释。注释写明读者用例名、被断言的两组内容（`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` 与四项 DB 凭据否定清单），并说明为何不挂进 #1684 组（那组 target 是 deployment contract suite，而该 suite 不读本模板）。

## 2. 元测试

- [x] 2.1 新增精确 2 元集钉 `test_refresh_env_template_selects_exactly_its_owner_and_runtime_suites`，断言该模板的选择恰为 owner suite 与 `tests/test_two_node_docker_runtime.py`（后者来自仍在的 `infra/env/**` 行），注释说明为何不是 1 元集。
- [x] 2.2 新增 red leg：monkeypatch 掉新规则后该钉必须红。
- [x] 2.5 新增 Scenario 2 三个兄弟模板（`compute.example`、`compute.scheduler-dbfree.env.example`、`display.example`）的**精确集**钉，手法照 `test_node27_autopipe_timer_row_selects_both_of_its_readers`。round 1 两名 verifier 均判定这是**测试覆盖缺口**而非措辞问题：改写既有 `compute.example` 行的 targets 会让其选择集从 2 元变 3 元而现有护栏全绿；把 `infra/env/**` 收窄会让 `display.example` 选择集变**空**（即 #1182 的零断言降级，正是 #2195 要防的失效），同样无人捕获。`display.example` 在本元测试套里改前零命中。
- [x] 2.6 新增 reader 链接守卫：用既有 `_literal_path_consumer_index(targets=...)`（同文件）断言 owner suite 确实按字面路径读本模板。**成员向而非子集向**——实测删掉 reader 的 `read_text` 行后子集向真空为绿、只有成员向变红。不覆盖 suite 文件被删/改名（`test_every_pinned_node_id_resolves_to_an_existing_test_function` 的 stale-target 腿已红）。
- [x] 2.4 新增 flags 钉，断言新规则的 `stop_on_match` 与 `only_when_any_changed` 均为假（照 #2122 的 `test_precip_tree_rule_carries_no_selection_flags` 手法）。两个 flag 对本路径**行为惰性**——`infra/env/**` 行在前已累加、其后无规则匹配本路径，故精确相等钉抓不到它们，spec delta 里「neither `stop_on_match` nor `only_when_any_changed`」这条 SHALL 没有钉就是空文。命名**不得含 `refresh_env` 子串**，以保住 `-k "refresh_env"` 单选精确相等钉。
- [x] 2.3 红→绿与 red-leg 证据按 Required evidence 产出。

## 3. 验证

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py tests/test_scheduler_file_provider_refresh.py` 全绿；`uv run ruff check .` 清洁；四条清单实测；`openspec validate --strict` 通过。
