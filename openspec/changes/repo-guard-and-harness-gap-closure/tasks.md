# 任务

## Wave 1

### A. #1827 → #2211（串行，同一实现者）

- [x] A-1 加 `test_lineage_state_index_fixtures_rule_selects_its_exact_suites`，断言
      `select_tests(["tests/lineage_state_index_fixtures.py"])` 全等于四个消费者 ∪ `SELECTOR_META_GUARD_TEST`
- [x] A-2 red leg：临时从 `scripts/select_ci_tests.py` 里 `tests/lineage_state_index_fixtures.py` 那条规则删掉
      `tests/test_scheduler_generation.py`，
      记录新测试红、`test_routed_support_module_selects_its_importer_suites_and_the_meta_guard` 仍绿；随后还原
- [x] A-3 量化 `apps/api/openapi_patching.py` 的 13 个 `_patch_*_openapi`：每个 → 它改的路由/component →
      候选 oracle suite → 是否已在规则 targets 内 → **no-op mutant 下是否真的变红**。结果落 PR body。
      最后一列是判据本身：本模块唯一可观察输出是运行时 OpenAPI document，读静态 yaml 或不读 schema 的
      suite 无论路由多公开都不是它的 oracle
- [x] A-4 按量化结果逐条补显式字面量 target（不放宽规则），并**更新既有** exact-set 锚
      `tests/test_select_ci_tests.py::test_select_tests_maps_openapi_patch_owner_to_drift_plus_api_consumers`——
      不新增第二条同路径锚；期望集必须保留 `WRITE_SURFACE_SCAN_PATH` 与 `FAMILY_GUARD_PATH`
- [x] A-5 no-op mutant 实测（不止 precip 一组）：记录每组变红的 suite 集合。
      `_patch_precip_openapi` 组是 issue 的立论依据，必须单独留档——实测它**不支持** issue 的结论
- [x] A-6 改前/改后对**四条**路径各跑一次选择器并 diff：`apps/api/openapi_patching.py`、`apps/api/main.py`、
      `apps/api/route_registry.py`、`apps/api/routes/precip.py`。前者应只增加新 target，后三者 diff 必须为空——
      这才是「其余路径选择结果零变化」的实测，单跑一条路径证不了

### B. #2105

- [x] B-1 `frontend-build` 追加 `corepack enable && corepack prepare pnpm@10.11.0 --activate`
- [x] B-2 追加带 `actions/cache` 的 `playwright install --with-deps chromium`（key 绑 `pnpm-lock.yaml`）
- [x] B-3 追加 `pnpm run test:e2e:mocked-regression`；不设 `retries`
- [x] B-4 核对 `frontend-build` 的 `timeout-minutes: 10`：现状 ≈1m35s，加 chromium 下载 30-60s、`--with-deps`
      apt 30-60s、lane 本地 16.8s（CI 按 2-3× 估 35-50s），冷缓存最坏 ≈4m25s，稳态 ≈3m。保持 10 不上调
- [x] B-5 `docs/VALIDATION.md` Frontend E2E 小节注明：mocked-regression 已是自动 CI 门，live-display 仍只在 node-27

### C. #2107

- [x] C-1 用记录型 delegating monkeypatch 包 `reconcile_module.subprocess.Popen`，捕获真实 `Popen` 对象
- [x] C-2 回收断言改用捕获句柄的已完成 returncode；删除对 `FAKE_SACCT_PID_PATH` / `wall_time.pid` 的依赖
- [x] C-3 保留 `query_unavailable`、`durable_write_count == 0`、pipeline job 未变、无 candidate projection 断言，
      **以及** `len(repr(outcomes[0])) < 1_000` 的体量边界与 `terminated_path` marker 断言
      （#2107 验收第 3 条，按符号点名以免被「简化」掉；行号在本 PR 自己的 diff 下会漂，故不写行号）
- [x] C-4 不改 `services/orchestrator/reconcile.py`；不 skip/xfail/延长 timeout/加重试

### E. #1829

- [x] E-1 加 `json_doc()`，一次性序列化整个 tool-call 文档（command + 可选 cwd）
- [x] E-2 三处裸插值槽位（`run_hook` 的 `command` 与 `cwd`、`run_hook_split`、`run_hook_from`）统一改用 `json_doc()`；
      第 6 组 legacy 用例的无 `%s` 字面量文档一并改用同一 helper 但不计入裸插值。先机械枚举确认没有第四处
- [x] E-3 新增用例：command 含双引号/反斜杠/换行/CR/tab，断言 hook exit 为 0 或 2，而非 1

### F. #1897

- [x] F-1 `HttpClient` / `_HttpxClient` 的 `post`/`delete` 增加可选 `headers` 形参；`get` 不变
- [x] F-2 mutation 经 `packages.common.request_auth.read_configured_service_token` 附 `Authorization: Bearer`
- [x] F-3 缺可用 token 或 401 `AUTH_REQUIRED` → `status=BLOCKED` + 非空 `dependency_blocker`，
      `live_proof_accepted is false`，绝不编造 PASS
- [x] F-4 token 不进 argv / `--help` / `receipt["command"]` / `notes` / 日志 / 证据 JSON
- [x] F-5 测试覆盖：mutation 带 bearer、health/GET 不带、缺 token BLOCKED、receipt 序列化后不含假 token 字面值

## Wave 2

### G. #2261（降范围切片，见 design.md D1）

- [x] G-1 `.review-gate-issues.json` 只剩 `issues` 一个顶层 key；**全部 13 条**词表外的 `outcome: "closed"`
      改为 `merged`（issue 1183/1237/1269/1272/1338/1341/1369/1370/1414/1697/1734/1736/2013，
      对应 PR 1184/1385/1271/1275/1377/1443/1379/1381/1687/1706/1788/1751/2117，已逐条 `gh pr view` 核为 MERGED）；
      `issues.1660.gateEntries == 1`（取保守上界，无 oracle，理由记 PR body）
- [x] G-2 新增 `tests/test_review_gate_issue_memory.py`：stdlib 读文件，断言 (a) 顶层 key 恰为 `{"issues"}`，
      (b) 每条 `closed[].outcome` ∈ `{merged, superseded-by-split, abandoned, descoped}`，
      (c) 每个 issue 条目结构完整（`ceilingPrs` / `gateEntries` / `closed` 俱在，类型正确）
- [x] G-3 守卫写成对 dict 的纯函数；测试对真实文件的变异体（加裸顶层 key / `outcome: "closed"` / 缺 `ceilingPrs`）取红
- [x] G-3b 词表违规的失败信息必须指出成因与修法：`close` 未带 `--outcome` 时未跟踪工具的兜底值不在词表内，
      修法分两段：当场 `close` 时补 `--outcome merged`；已写入的记录 `close` 改不了，须按 `gh pr view` 手改并提交。
      这条通道本 PR 修不了（design.md D1），守卫只能抓红并告知修法
- [x] G-4 `.github/workflows/ci.yml` 的 `backend` filter 加 `.review-gate-issues.json` 字面量，并按仓内既有先例
      （`tests/test_select_ci_tests.py` 的 `test_calibration_declaration_backend_filter_entry_is_block_scoped`
      与 `test_calibration_declaration_backend_filter_entry_reds_when_removed_or_moved` 等 #1571/#1860/#1688 pin）
      配一条字面量 pin 测试，带「删掉该 filter 条目即红」的变异腿
- [x] G-5 `scripts/select_ci_tests.py` 加规则把 `.review-gate-issues.json` 路由到 G-2 的测试 + meta-guard，
      并在 `tests/test_select_ci_tests.py` 加 exact-set 锚
- [x] G-6 在 #2261 上留言，逐条列出六条验收的处置：**4 条**（1/3/4/5）因文件未被跟踪而不可在本仓落地、
      **1 条**（2，JSON 一次性修复）已交付、**1 条**（6，测试通过 + ruff 干净）以新增的仓库侧守卫
      而非未跟踪 CLI 的单测满足；PR **不写** `Closes #2261`

## Evidence Floor

每项 = 一条可跑的命令 + 一个出现在其输出里的字面 token。

- [x] EF-1 `uv run pytest -q tests/test_select_ci_tests.py` → `passed`，无 `failed`
- [x] EF-2 #1827 红腿：`sed -i.bak '/"tests\/test_scheduler_generation.py",/d' scripts/select_ci_tests.py`，
      跑 `uv run pytest -q tests/test_select_ci_tests.py -k "lineage_state_index or routed_support_module"`
      → 输出含 `FAILED` 且失败的是新锚、`test_routed_support_module_selects_its_importer_suites_and_the_meta_guard` 仍 `passed`；
      `mv scripts/select_ci_tests.py.bak scripts/select_ci_tests.py` 还原后同命令无 `failed`
- [x] EF-3 `printf '%s\n' 'apps/api/openapi_patching.py' | uv run python scripts/select_ci_tests.py --repo-root .`
      改后输出含 `tests/test_hydro_display_mvt_scaling.py`（**实测定的 target**；issue 猜的
      `tests/test_precip_overlay.py` 读静态 yaml、观察不到本模块，故不加）
- [x] EF-4 A-6 的四条路径改前/改后各跑一次并 `diff`：`apps/api/main.py`、`apps/api/route_registry.py`、
      `apps/api/routes/precip.py` 三者的 diff 输出为**空**（零字节）
- [x] EF-5 #2211 生产红腿：把 `_patch_mvt_tile_openapi` 体置为 `return`，
      `uv run pytest -q $(printf '%s\n' 'apps/api/openapi_patching.py' | uv run python scripts/select_ci_tests.py --repo-root . | tr '\n' ' ')`
      → 含 `tests/test_hydro_display_mvt_scaling.py` 的两条 `FAILED`（改前拿不到的红）；还原后无 `failed`
- [x] EF-5b #2211 selector 红腿：从规则删掉新 target，`uv run pytest -q tests/test_select_ci_tests.py -k openapi_patch_owner`
      → 含 `FAILED`；还原后 `passed`
- [x] EF-5c #2211 反证留档：`_patch_precip_openapi` 置 no-op 后 `tests/test_precip_overlay.py` 全绿——
      这是「不加该 target」的依据，必须进 PR body，否则读者会以为验收被绕过
- [x] EF-6 wall-time node 连续 100 次、每次独立临时目录：
      `mkdir -p /private/tmp/nwmtmp; for i in $(seq 100); do TMPDIR=$(mktemp -d /private/tmp/nwmtmp/r.XXXXXX) uv run pytest -q -p no:cacheprovider 'tests/test_gateway_reconcile_comment_sacct_bounds.py::test_real_sacct_process_bounds_reap_and_leave_inflight_cohort_unchanged[wall_time]' >/dev/null 2>&1 || { echo "FAILED at $i"; break; }; done; echo "completed=$i"`
      → 输出 `completed=100`，且无 `FAILED at`
- [x] EF-7 `uv run pytest -q tests/test_gateway_reconcile_comment_sacct_bounds.py` → `passed`，无 `failed`
- [x] EF-8 `bash .claude/hooks/large-file-guard/test-large-file-guard.sh` → 输出含 `summary: ALL CHECKS PASSED`，
      且 `summary: N PASS assertions executed` 的 N 不小于改前实测值（改前值由实现者先跑一次记录）
- [x] EF-9 `uv run pytest -q tests/test_m24_gateway_proof.py` → `passed`，无 `failed`
- [x] EF-10 `uv run pytest -q tests/test_review_gate_issue_memory.py` → `passed`
- [x] EF-11 G-3 三个变异体各取红。变异在内存 dict 上做（守卫是纯函数），由三个 pytest 用例承载；
      运行 `uv run pytest -q tests/test_review_gate_issue_memory.py -k mutant` → `3 passed`
- [x] EF-12 `uv run python -c "import json;print(list(json.load(open('.review-gate-issues.json'))))"` → `['issues']`
- [x] EF-13 `uv run python -c "import json;d=json.load(open('.review-gate-issues.json'))['issues'];print(sorted({c['outcome'] for v in d.values() for c in v['closed']}))"`
      → `['descoped', 'merged', 'superseded-by-split']`（无 `closed`）
- [x] EF-14 G-4 红腿：从 `ci.yml` 的 `backend` filter 删掉 `.review-gate-issues.json` 一行，
      `uv run pytest -q tests/test_select_ci_tests.py -k review_gate` → 含 `FAILED`；还原后 `passed`
- [x] EF-15 `uv run ruff check .` → `All checks passed!`
- [x] EF-16 `openspec validate repo-guard-and-harness-gap-closure --strict --no-interactive` → `is valid`
- [x] EF-17 `uv run python scripts/cite_check.py openspec/changes/repo-guard-and-harness-gap-closure/*.md`
      → `hard failures: 0`
- [x] EF-18 #2105 **绿**证：临时分支（自本分支终态切出）commit 1 只在 `apps/frontend/e2e/m11-routes.mocked.spec.ts`
      加一行注释——必须触碰 `apps/frontend/**`，否则 `frontend` filter 不命中、job 直接 skip。
      `gh run view <id> --log` 含 `33 passed`——**实测值**：`playwright test --config playwright.config.ts --list`
      给出 `Total: 33 tests in 3 files`，本地实跑 `33 passed (16.4s)`。issue #2105 正文里的「16」是 PR #2101
      时点的旧数，用它 grep 会把绿 run 误判为红
- [x] EF-19 #2105 **红**证：等 EF-18 的 run 跑完（PR 上 `cancel-in-progress: true`）再推 commit 2，
      把 `m11-routes.mocked.spec.ts` 一条断言写反 → 同 job `conclusion: failure`。两个 run URL 进 PR body；
      临时 PR 关闭不合并
- [x] EF-20 #366 结论未被回滚：`grep -c workflow_dispatch .github/workflows/m15-visual-evidence.yml` ≥ 1，
      且 `grep -rn '&& false' .github/workflows/` 退出码非 0（无命中）
- [x] EF-21 本 PR 的 `Unit Tests` 与 `SQL Migration Dry Run` 绿；`Frontend Build` 在本 PR 上 `skipping`
      ——`ci.yml` 不在 `frontend` filter 内，属结构性预期，非缺口（#2105 验收第 4 条由既有 `if:` 结构性满足）
