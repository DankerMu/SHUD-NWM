# Proposal: ci-selector-refresh-env-owner-suite (#2195)

## Why

`infra/env/compute.scheduler-provider-refresh.env.example` 有一个 content-asserting 读者，却选不到它。
`tests/test_scheduler_file_provider_refresh.py:3595` 在
`test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent` 里 `read_text` 该模板，并断言
`:3613` 的 `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`，以及 `:3616-3617` 的否定清单
（`DATABASE_URL=` / `PIPELINE_DATABASE_URL=` / `PGHOST=` / `PGPORT=` 四者都不得出现）。

实测（695aff35）该模板只选中 `tests/test_two_node_docker_runtime.py`——由 `scripts/select_ci_tests.py:2301-2304`
的 `infra/env/**` 规则买到，而那个 suite `grep` 该文件名零命中，根本不读它。所以被选中的集合里**零个**读者。

比 #2188 更沉默：`count == 1` 不为零，`.github/workflows/ci.yml` 的 `#1182` 零断言告警不触发，PR 页面上没有任何提示。

成因：模板生于 `a75aaa06`（#1076）；`67017eaf`（#1684）给同目录两个兄弟模板补了 path-exact 行
（`:2314-2321` 的 `infra/env/compute.example` 与 `compute.scheduler-dbfree.env.example`），**独漏这第三个**。

## What Changes

1. `scripts/select_ci_tests.py` `PATH_TEST_RULES` 新增一条 path-exact 行（无 `stop_on_match`、无 `only_when_any_changed`），
   带 `#2195` 注释写明读者用例名与被断言的两组内容：

   ```text
   infra/env/compute.scheduler-provider-refresh.env.example -> ("tests/test_scheduler_file_provider_refresh.py",)
   ```

   **放置在 `infra/env/README.md` 行（`:2322-2325`）之后**——即 `:2305-2325` 的 #1684 rollout-producer 组结束处、
   `scripts/validate_two_node_docker_runtime.py` 行之前：仍在 env 邻域，落在 #1684 注释（`:2305-2309`）的管辖行之外，
   不触动任何既有块。两个被否的位置各有理由：
   - **不并入 #1684 组**：那组的 target 是 `SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST`，而
     `tests/test_slurm_gateway_deployment_contract.py` 的模板清单（`:243-244`）只列 `compute.example` 与
     `compute.scheduler-dbfree.env.example`，全树 grep 确认它**不读**本模板；挂进去会把语义写错，
     且会为该模板买一个不读它的 suite。
   - **不放进 #2188 的 refresh 家族块**（改前 `:2423` 起，本行插入后为 `:2445` 起）：那个块的注释自称
     「these two rows are systemd units」并写明「the wrapper run resumes just below」，插一条 env 模板行进去会把该注释证伪，
     代价是要改写一段已合入的注释，而收益仅是就近。
2. `tests/test_select_ci_tests.py` 新增一条**精确 2 元集**钉：

   ```python
   assert set(select_tests(["infra/env/compute.scheduler-provider-refresh.env.example"], repo_root=Path("."))) == {
       "tests/test_scheduler_file_provider_refresh.py",
       "tests/test_two_node_docker_runtime.py",
   }
   ```

   写成 `== {owner}` 会红——`:2302` 的 `infra/env/**` 行仍在，两条规则累加。既有的
   `test_rollout_owner_producers_select_the_static_deployment_contract`（`tests/test_select_ci_tests.py`）
   用的是宽松的 `in selected`，与本钉不冲突也不重复。

3. 同文件另加四条钉，合计五条：

   - **red leg**：monkeypatch 去掉新规则后，该模板必须选不到 owner suite。
   - **flags 钉**：断言新规则的 `stop_on_match` 与 `only_when_any_changed` 均为假。二者对本路径**行为惰性**
     （`infra/env/**` 在前已累加、其后无规则匹配本路径），精确相等钉抓不到，故 spec delta 里「neither flag」
     那条 SHALL 需要独立的钉。手法照 #2122 的 `test_precip_tree_rule_carries_no_selection_flags`。
   - **兄弟模板精确集钉**（round 1 复审补）：把 Scenario 2 点名的 `compute.example`、
     `compute.scheduler-dbfree.env.example`、`display.example` 三者的选择结果钉成精确列表。没有它，改写既有
     `compute.example` 行的 targets 会把该模板选择集由 2 元变 3 元而全表守卫皆绿；把 `infra/env/**` 收窄
     还会让 `display.example` 塌成**空**选择（即 #1182 的零断言降级，正是本 change 要防的失效）。
   - **reader 链接守卫**（round 1 复审补）：用既有 `_literal_path_consumer_index(targets=...)` 推导读者，
     断言 owner suite 确在其中。**成员向而非子集向**——reader 被删后派生集为空，子集向真空为绿，
     只有成员向变红。

## Non-Goals

- 不改 `.github/workflows/ci.yml`（`#1182` 的零断言告警行为正确，本 change 不是「改告警」）。
- 不动 env 模板本身、不动任何被选中的 suite、不动 `:2302` 的 `infra/env/**` 行。
- **不收 write_roles 那条腿**（见下方「已知残留」）：不把 `:2247` 的 `infra/env/node27-*.example` glob 放宽成 `*.example`，
  也不给本行加 `tests/test_node27_write_roles.py` 目标。issue #2195 把「4 个非 node27 模板拿不到 write_roles superuser 守卫」
  显式列为 out of scope，其验收标准第一项也把本模板的选择结果钉成**恰两行**；本 change 遵守该范围。
- 不碰 `infra/systemd/**`（#2188 已合）、`services/precip/**`（#2122 已合 / #2191）、river_segment 写面（#2185）。
- 不触碰 #2041 / #2146 的 timer 现网停摆——那是运行态，本 change 只管 CI 路由。

## 已知残留（本 change 有意不收，落地后仍然成立）

`tests/test_node27_write_roles.py:486` 的 `_env_templates()` 用 `_ENV_DIR.glob("*.example")` 扫全部 14 个模板（含本文件），
喂给两条守卫：`:521-531` 的 `test_env_templates_name_no_superuser_credential_outside_the_allow_list`
（`#1774`，断言无 superuser 凭据）与 `:567-576` 的
`test_no_template_still_carries_the_unprovisioned_writer_placeholder`（断言无 `REPLACE_ME_WRITER` 占位）。
两者都 `read_text` 本模板并断言其内容，且同样不被选中——
`:2247` 的规则 glob 是 `infra/env/node27-*.example`，本文件不匹配。

后果说清楚：issue 实测的三条红变异里，`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID` 翻转与混入 `PGHOST=` 两条命中
`tests/test_scheduler_file_provider_refresh.py`，本 change 之后定向车道能抓；第三条**混入 `PGUSER=nhms`** 只红 write_roles
（`PGUSER=` 不在 refresh 读者的否定清单里），本 change 之后**仍**只能靠 merge 后 master 的全量 run 事后抓。
这是 issue 划定范围的已知代价，不是遗漏。该腿的受害面实测为 4 个非 `node27-*` 模板（本文件 +
`compute.example` + `compute.scheduler-dbfree.env.example` + `display.example`）；本 change **不动** `:2247` 的 glob，
所以这 4 个改前改后一个不少——**含本文件在内，4 个全部仍在受害面内**。更广的「content-asserting 读者未被选中」
家族见 issue #2195 正文第 7 条。

## Risk triage

- Fixture level: compact（一条 path-exact 规则 + 五条元测试，全部为选择器结构断言；无运行时行为变化。不取 `none`：
  `scripts/select_ci_tests.py` 是 CI 定向门的共享选择入口，与 #2173 / #2180 / #2122 / #2188 同级）。
  Upstream suggested level: absent（issue 由 #2188 立项期间的 issue-scribe 立单，无 `Suggested fixture level` 字段；`预估规模 S`）。
- Repair intensity: low。
- Risk packs 与 evidence 见 `tasks.md`。`design.md` 按 compact 级豁免。

## Must preserve

- `:2302` 的 `infra/env/**` 行与 `:2305-2325` 的 #1684 rollout-producer 组零改动；`:2247` 的 write_roles glob 零改动。
- `tests/test_select_ci_tests.py` 的 #1684 两条用例（`test_rollout_owner_producers_select_the_static_deployment_contract` 与其 red leg `test_rollout_owner_producer_rules_red_when_removed`）、`NODE27_UNIT_OWNER_SUITES`（#2180 起）
  与 `NODE22_UNIT_OWNER_SUITES`（#2188）两张 owner 表及其元测试、duplicate-pattern / stop-on-match /
  规则目标存在性守卫，全部照旧绿。
- 其余 13 个 `infra/env/*.example` 的选择结果逐条不变（改前基线已逐个实测留档）。

## Seams under test

- `select_tests(["infra/env/compute.scheduler-provider-refresh.env.example"], repo_root=Path("."))` 的输出恰为两元集。
- 新规则的 `stop_on_match` / `only_when_any_changed` 两个 flag 均为假。
- Scenario 2 三个兄弟模板（`compute.example`、`compute.scheduler-dbfree.env.example`、`display.example`）各自的选择结果。
- `_literal_path_consumer_index(targets=[本模板])` 推导出的字面读者集合含 owner suite。

## Evidence mapping

见 `tasks.md` Required evidence；本地即可闭环（issue `Verification` 明示无需 node-22 / node-27 oracle）。
