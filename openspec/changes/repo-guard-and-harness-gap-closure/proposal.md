# 仓库自有守卫与测试夹具的缺口收口（第 9 批）

## Why

七个 issue 的共同对象不是产品行为，而是**仓库用来判断自己对不对的那层东西**：CI 定向选择规则、
e2e 门、测试夹具的确定性、hook 测试 harness 的编码、诊断发射器的鉴权、以及跨 PR 的 review-gate 记忆。
它们各自独立、都是 S 规模、都在同一条「守卫存在但不咬人」的形状上：

- **#2105** `test:e2e:mocked-regression`（3 spec / 16 test）**没有任何自动 CI 门**——`.github/workflows/ci.yml:416-439`
  的 `frontend-build` 是唯一消费 `apps/frontend` 的 job，其 step 里无 Playwright。前端 PR 可以改坏 e2e 断言后全绿合并。
- **#2211** `apps/api/openapi_patching.py` 的 path-exact 规则（`scripts/select_ci_tests.py:2639`）漏掉了能观察它的
  suite。issue 认定漏的是 `tests/test_precip_overlay.py`，**实测推翻**：该 suite 读的是静态
  `openapi/nhms.v1.yaml`（`tests/test_precip_overlay.py:1814`），观察不到运行时 patch，且 precip 已被
  `tests/test_openapi_drift.py:138` 直接钉住。真正的缺口是 `tests/test_hydro_display_mvt_scaling.py`
  ——它断言 `main.create_app().openapi()`（`:1865`、`:4239`），且改前未被选中。
- **#1827** `tests/lineage_state_index_fixtures.py` 的规则（`scripts/select_ci_tests.py:1059`）命名四个消费者，
  其中两个是函数体内 import；通用守卫 `test_routed_support_module_selects_its_importer_suites_and_the_meta_guard`
  （`tests/test_select_ci_tests.py:13170`）从规则自身派生 `required`，删掉一个 target 它照样绿。
- **#2107** `tests/test_gateway_reconcile_comment_sacct_bounds.py:324-326` 无条件读 fake shell 写的 PID 文件；
  20 次隔离运行实测 17 passed / 3 failed，随机红灯掩盖真回归。
- **#1829** `.claude/hooks/large-file-guard/test-large-file-guard.sh:19`、`:46`（另有 `:168`、`:354` 两处同形手写） 对 `command` 裸插值构造 JSON，
  含引号/控制字符的用例在 hook 的 `json.loads` 阶段就崩溃，测不到守卫逻辑。无生产缺陷。
- **#1897** `scripts/m24_gateway_proof.py:163-192`、`:295-307` 匿名 POST/DELETE `/api/v1/slurm/jobs`，#1888 之后稳定 401。
- **#2261** `.review-gate-issues.json` 的两个裸顶层 key 与 `issues` map 互相矛盾（`1736` 的 outcome 失真、
  `1660` 的 `gateEntries` 1 vs 0 无 oracle），且含词表外的 `"closed"`。

## What Changes

七件事一个 PR，写集互不重叠（重叠的两组已串行化，见 design.md）。**#2261 被降范围**：
其六条验收里有五条要求修改 `.claude/skills/subagent-workflow/scripts/{review_gate,evidence_check}.py` 与
`references/*.md`——这些文件**不在版本控制内**（`git ls-files | grep -ci subagent` = 0），由 `002ba4b59`
（2026-09-14）有意 untrack。要改它们就得先把它们重新纳入跟踪，那是推翻一条四天前的治理决定，
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
