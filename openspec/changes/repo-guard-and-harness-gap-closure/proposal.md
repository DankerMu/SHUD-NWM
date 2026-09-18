# 仓库自有守卫与测试夹具的缺口收口（第 9 批）

## Why

七个 issue 的共同对象不是产品行为，而是**仓库用来判断自己对不对的那层东西**：CI 定向选择规则、
e2e 门、测试夹具的确定性、hook 测试 harness 的编码、诊断发射器的鉴权、以及跨 PR 的 review-gate 记忆。
它们各自独立、都是 S 规模、都在同一条「守卫存在但不咬人」的形状上：

- **#2105** `test:e2e:mocked-regression`（实测 3 spec / 33 test；issue 正文的 16 是 PR #2101 时点的旧数）**没有任何自动 CI 门**——`.github/workflows/ci.yml` 的 `frontend-build` job
  的 `frontend-build` 是唯一消费 `apps/frontend` 的 job，其 step 里无 Playwright。前端 PR 可以改坏 e2e 断言后全绿合并。
- **#2211** `apps/api/openapi_patching.py` 的 path-exact 规则（`scripts/select_ci_tests.py` 的 `PATH_TEST_RULES` 中该路径那条）漏掉了能观察它的
  suite。issue 认定漏的是 `tests/test_precip_overlay.py`，**实测推翻**：该 suite 读的是静态
  `openapi/nhms.v1.yaml`（其 `_static_openapi` fixture 读的就是这份），观察不到运行时 patch，且 precip 已被
  `tests/test_openapi_drift.py::test_dropping_the_precip_openapi_patch_drifts_runtime_schema_from_static_yaml`
  直接钉住。真正的缺口是 `tests/test_hydro_display_mvt_scaling.py` ——它断言 `main.create_app().openapi()`，且改前未被选中。
- **#1827** `tests/lineage_state_index_fixtures.py` 的规则（`scripts/select_ci_tests.py` 的 `PATH_TEST_RULES` 中该路径那条）命名四个消费者，
  其中两个是函数体内 import；通用守卫 `tests/test_select_ci_tests.py::test_routed_support_module_selects_its_importer_suites_and_the_meta_guard`
  从规则自身派生 `required`，删掉一个 target 它照样绿。
- **#2107** `tests/test_gateway_reconcile_comment_sacct_bounds.py::test_real_sacct_process_bounds_reap_and_leave_inflight_cohort_unchanged`
  的 `wall_time` 腿改前无条件读 fake shell 写的 PID 文件；
  20 次隔离运行实测 17 passed / 3 failed，随机红灯掩盖真回归。
- **#1829** `.claude/hooks/large-file-guard/test-large-file-guard.sh` 的 `run_hook` / `run_hook_split` / `run_hook_from`
  三处对 `command` 裸插值构造 JSON（另有一处无 `%s` 的手写字面量文档），
  含引号/控制字符的用例在 hook 的 `json.loads` 阶段就崩溃，测不到守卫逻辑。无生产缺陷。
- **#1897** `scripts/m24_gateway_proof.py` 的 `_submit_smoke` 与 `_run_submit_cancel_stage` 匿名 POST/DELETE
  `/api/v1/slurm/jobs`，#1888 之后稳定 401。
- **#2261** `.review-gate-issues.json` 的两个裸顶层 key 与 `issues` map 互相矛盾（`1736` 的 outcome 失真、
  `1660` 的 `gateEntries` 1 vs 0 无 oracle），且含词表外的 `"closed"`。

## What Changes

七件事一个 PR，写集互不重叠（重叠的两组已串行化，见 design.md）。**#2261 被降范围**：
其六条验收里有**四条**（第 1/3/4/5 条）要求修改 `.claude/skills/subagent-workflow/scripts/{review_gate,evidence_check}.py`
与 `references/*.md`——这些文件**不在版本控制内**（`git ls-files | grep -ci subagent` = 0；`.claude/` 一直被
`.gitignore:90` 忽略，这些路径从未被跟踪）。要改它们就得先**首次** force-add 纳入跟踪，而同一套脚本的
`.agents/skills/` 副本刚由 `002ba4b59`（2026-09-14）因装包覆盖问题有意 untrack——首次纳入与该判断正相反，
#2261 没提出、用户的预授权也不覆盖（见 design.md D1）。本 PR 只交付其**可落地切片**
（committed JSON 的修复 + 一条 tracked 的结构守卫 + 让该守卫真能被 CI 选中），
差额回报给 #2261，**不 `Closes #2261`**。

## Impact

- Affected specs: `frontend-testing`、`ci-contract-baseline`、`worktree-local-large-file-guard`、
  `opt-in-live-proof-lane`、`delivery-traceability-hygiene`
- Affected code: `.github/workflows/ci.yml`、`docs/VALIDATION.md`、`scripts/select_ci_tests.py`、
  `tests/test_select_ci_tests.py`、`tests/test_gateway_reconcile_comment_sacct_bounds.py`、
  `.claude/hooks/large-file-guard/test-large-file-guard.sh`、`scripts/m24_gateway_proof.py`、
  `tests/test_m24_gateway_proof.py`、`.review-gate-issues.json`、新增 `tests/test_review_gate_issue_memory.py`
- 不改：`services/orchestrator/reconcile.py` 的 timeout/reap 契约、`apps/api/openapi_patching.py` 的功能、
  任何 e2e spec 内容、`m15-visual-evidence.yml` 的触发方式、生产 `HttpSlurmGatewayClient`。
