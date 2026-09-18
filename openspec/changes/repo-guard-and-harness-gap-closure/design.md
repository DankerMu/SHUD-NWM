# 设计决策

## 风险分级

- Fixture level: **expanded**（七个独立 issue，写集跨 CI workflow / 定向选择器 / 测试夹具 / 诊断脚本 / 跨 PR 状态文件；
  单个 issue 都是 S，聚合面是宽而不深）。上游 issue 未带 `Suggested fixture level` 字段，无背离可记。
- Round 1 seats（3）：`test-evidence`、`spec-compliance`、`invariant-state`。
- 选中的 risk pack：CI 门完整性、测试 oracle 独立性、secret 不外泄。未选：DB/迁移、并发、性能——七单均不触达。

## 必须保持的行为

| 不变量 | 锚 |
|---|---|
| `services/orchestrator/reconcile.py` 的 timeout/terminate-and-reap 契约 | `_bounded_sacct_stdout`（`services/orchestrator/reconcile.py:1099`）与 `_terminate_and_reap`（`services/orchestrator/reconcile.py:1159`）。issue #2107 正文引的 `:853-925` 已随文件增长失效，以这两个符号为准 |
| `apps/api/openapi_patching.py` 的 patch 语义 | 本 PR 不改该文件 |
| `m15-visual-evidence.yml` 仅 `workflow_dispatch`（#366 结论） | `.github/workflows/m15-visual-evidence.yml` |
| gateway health/GET 匿名可达（#1888） | `scripts/m24_gateway_proof.py` 的 `get` 协议不加 headers |
| 生产 `HttpSlurmGatewayClient` 行为 | `services/orchestrator/chain_slurm_client.py:158` |
| `large-file-guard.sh` 本身无缺陷 | 本 PR 只改 `test-large-file-guard.sh` |

## 跨切决策

### D1 —— #2261 强制降范围，不是选择

`git ls-files | grep -ci subagent` = **0**。`002ba4b59`（2026-09-14，"chore(agents): untrack installed skill
copies under .agents/skills*"）有意把这些副本移出版本控制，理由是每次装包都会产生永久工作树漂移。

**机制上并非不可能**：`.gitignore:90` 忽略整个 `.claude/`，而 `git ls-files '.claude/*'` 返回两个文件
——正是本 PR E 组要改的 `.claude/hooks/large-file-guard/{large-file-guard,test-large-file-guard}.sh`。
force-add 是本仓现成手段。降范围的理由不是做不到，而是**没有授权**：把
`.claude/skills/subagent-workflow/scripts/*` 重新纳入跟踪就是推翻 `002ba4b59`，
那是一条四天前的治理决定，#2261 没有提出，用户对本批的预授权是 merge 而不是治理翻案。
按仓规，范围外发现**报告，不修**。

交付的切片**不等价**于原方案，且差额要说清：
- 缺陷 1 的**手工解冲突**通道：被覆盖，且覆盖位置比原方案好——守卫从「未跟踪、装包即覆盖」的 CLI 内部，
  搬到「被跟踪、CI 会跑」的仓库测试里。
- 缺陷 1 的**写路径**通道：**未覆盖**。未跟踪的 `cmd_close` 在不带 `--outcome` 时仍会写出词表外的
  `"closed"`（实测该通道近期仍在触发：issue 2013 / PR 2117）。本 PR 只能让守卫把它**抓红**并在失败信息里
  指出修法（`--outcome merged` 一个 flag），不能让它不再发生。
- 缺陷 2（verdict table 落盘 + `record-round` 挂钩 + 文档口径，占 6 条验收中的第 4、5 条）：**零覆盖、无替代**。

这两项差额由 G-6 逐条写进 #2261 的留言，不 close。

### D2 —— 新守卫读 JSON，不 import `review_gate.py`

`tests/test_loop_log_audit_attribution.py:26` 把 `SCRIPTS_DIR` 指向 `.agents/skills/subagent-workflow/scripts/`，
该目录现在只剩 `__pycache__`，于是该测试自 `002ba4b59` 起在每个 checkout 和每次 CI 里都是 module-level skip。
新守卫若照抄这个 import 方式，就复制了同一个缺陷。它改为用 stdlib 读 `.review-gate-issues.json`，
把 outcome 词表写成四个字面量并在注释里注明来源是 `review_gate.py` 的 `OUTCOMES`、以及为什么不能 import。
（`tests/test_loop_log_audit_attribution.py` 的恒 skip 本身超出本批范围，Phase 8 立单。）

### D3 —— `.review-gate-issues.json` 必须进 `backend` paths-filter

现实的复发路径是 post-merge 记账 PR（#2463 那个形状）：只动 `.review-gate-issues.json` +
`docs/review-loop-log.jsonl` + `openspec/changes/archive/**`，`backend` filter 全不命中 → Unit Tests skip →
守卫永不执行。引入裸顶层 `1660` 的 `e78cf98a` 正是这种 commit。所以 `ci.yml` 的 `backend` filter 必须
加这个字面量，并配一条 selector 规则把它路由到新测试——否则守卫是装饰。

### D4 —— 修 JSON 用与工具同一个序列化器

未跟踪的 `review_gate.py` 在其 history 写入处用 `json.dumps(history, indent=2, ensure_ascii=False) + "\n"`
（该文件不在 `git ls-files` 内，cite-check 回执按设计不解析它，故此处不写行号锚）。实测该格式对当前文件
**round-trip 逐字节相同**，所以修复 diff 只含 **15 处**真实改动、不含重排噪声：删 2 个裸顶层 key，
改 13 条词表外的 `outcome: "closed"`。13 条已逐条用 `gh pr view <pr> --json state` 核对，
对应 PR 1184/1271/1275/1377/1379/1381/1385/1443/1687/1706/1751/1788/2117 **全部 MERGED**，
故一律取 `merged`。`load_history()` 只读 `history["issues"]`，修好的文件对跑本 PR 自己 gate 的那份未跟踪工具是安全的。

### D5 —— review gate 的 `--issue` 只能记一个，选 #2261

`review_gate.py` 的 `--issue` 是单个 int，`issue_record()` 用 `str(issue)` 作 key，没有多 issue 形式。
既有多 issue 批次的记法也是只记一个（`.review-gate-issues.json` 里 #2059/#2104/#1955/#1739/#1844 各有一条
`closed` 而同批的 #2055/#1740/#1543 为 null）。本批记 **#2261**：它是七单里唯一预期会有后继 PR 的
（5/6 条验收被 D1 推回上游），而该字段的用途正是给后继 PR 的 ceiling 记忆。已实测七个 issue 当前
都不在任何 `ceilingPrs` 里，故 depth/noise retro 无需 `--user-approved`。

## 逐单决策

- **#2105**：选「在 `frontend-build` 里追加 step」而非独立 job——复用同一次 checkout/install，
  路径 scope 天然继承，验收第 4 条（后端/docs PR 仍 skip）由既有 `if: needs.changes.outputs.frontend == 'true'`
  结构性满足，不必再买一次 run 去证明。新 step 前必须 `corepack enable && corepack prepare pnpm@10.11.0 --activate`，
  因为 `playwright.config.ts:33` 的 `webServer.command` 硬编码 `corepack pnpm dev`。不加 `retries`。**知情接受的成本**：`frontend` filter 含 `openapi/**`，
  所以纯 `openapi/**` 的 PR 也会付一次浏览器成本；#2105 正文点名了这条，本 change 接受它，
  不另开更窄 filter（再开一条 filter 就是第二处要维护的路径真值）。
- **#2211**：逐条补显式字面量 target，不改宽规则——该文件每条规则都是显式 tuple，
  放宽会正面抵消验收要求的 exact-set 锚。13 个 patch 函数的量化表以实测为准，issue 正文里的
  「7 个 suite」与规则 tuple 的六个成员不一致（第七个是选择器输出，不是规则 target），以命令输出为准。
  **issue 的核心前提被实测推翻**：它假定 `tests/test_precip_overlay.py` 是缺失的行为 oracle，
  但该 suite 读静态 `openapi/nhms.v1.yaml`（`tests/test_precip_overlay.py:1814`），`_patch_precip_openapi`
  置 no-op 后它 106/106 全绿；且 precip 早被 `tests/test_openapi_drift.py:138` 的
  `test_dropping_the_precip_openapi_patch_drifts_runtime_schema_from_static_yaml`（#2010）直接钉住。
  本模块唯一的生产消费者是 `apps/api/main.py` 的 schema hook，可观察输出只有运行时 OpenAPI document，
  所以 oracle 资格由「读不读那份 document」决定，与路由多公开无关。13 行量化 + 5 组 no-op mutant 实测后，
  既未被选中又能观察的只有一个：`tests/test_hydro_display_mvt_scaling.py`
  （`:1865`、`:4239` 断言 `main.create_app().openapi()`），置空 `_patch_mvt_tile_openapi` 精确红它两条。
  另外四个候选（precip_overlay / forecast_api / met_station_series / runtime_mode）结构上不可能红，
  路由它们只会买到「跑了但观察不到」的断言，共 ~17.2s CI，按仓内既有裁定拒收。
  两处**既有事实**决定了改法：(a) `openspec/specs/ci-contract-baseline/spec.md:1192` 已按名钉死
  「`apps/api/openapi_patching.py` 的选择结果 SHALL remain unchanged」，所以本 change 必须带一个
  `## MODIFIED Requirements` 把这条 carve-out，否则合并后规格里会有两条互相矛盾的 requirement；
  (b) exact-set 锚**已经存在**（`tests/test_select_ci_tests.py:449`
  `test_select_tests_maps_openapi_patch_owner_to_drift_plus_api_consumers`，8 元素含
  `WRITE_SURFACE_SCAN_PATH` 与 `FAMILY_GUARD_PATH`），所以是**更新**它，不是再加一条——
  同一路径两条 exact-set 锚正是批次 8 第 3 轮的重复论证块形状。
- **#1827**：与 `test_demote_helper_rule_selects_public_chain_consumer_exactly`
  （`tests/test_select_ci_tests.py:13244`）同构的 exact-set 全等断言。
- **#2107**：记录型 delegating monkeypatch 包 `reconcile_module.subprocess.Popen`，
  启动确认边界落在生产代码真正持有子进程句柄的那一点；删掉对 shell 写 PID 文件的依赖。
- **#1829**：`json_doc()` 内部沿用 `json_cwd()`（`:39-41`）既有的 `python3 -c` 形式。
  hook 运行在 venv 之外，这是 CLAUDE.md「Python 一律用 uv」让位于 hook 运行时现实的那一处，
  不在此引入第三种写法。真正的裸插值槽位是三处（`:19`——`command` 与 `cwd` 都裸插、`:46`、`:354`）；
  `:168` 是无 `%s` 的字面量文档，一并改用 `json_doc()` 但不计入裸插值。实现者先机械枚举确认没有第四处。
- **#1897**：**保留**脚本（issue 的推荐路径），不退役——退役会触及 `scripts/select_ci_tests.py:2003`
  的映射，与 #2211/#1827 的写集冲撞。token 只经 `read_configured_service_token` 进入进程。

## 交付编排

写集重叠已串行化。Wave 1 五个并行（写集两两不交）：

| 组 | issue | 写集 |
|---|---|---|
| A | #1827 → #2211（串行，同一实现者） | `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py` |
| B | #2105 | `.github/workflows/ci.yml`、`docs/VALIDATION.md` |
| C | #2107 | `tests/test_gateway_reconcile_comment_sacct_bounds.py` |
| E | #1829 | `.claude/hooks/large-file-guard/test-large-file-guard.sh` |
| F | #1897 | `scripts/m24_gateway_proof.py`、`tests/test_m24_gateway_proof.py` |

Wave 2 串行：**#2261**——它同时要碰 A 的选择器对和 B 的 `ci.yml`，必须在两者落地之后。

#2105 的反证临时分支在 Wave 2 集成**之后**才切，这样它带的 `ci.yml` 就是本分支的终态；
`ci.yml` 的 `concurrency.cancel-in-progress` 在 PR 上是 true，所以第一次 run 跑完再推第二个 commit。

## 非目标

- 不给 #2107 写 spec delta：它是夹具确定性修复，不改任何行为契约。
- 不修 `tests/test_loop_log_audit_attribution.py` 的恒 skip（D2 尾注），不碰 `preview` lane（#2105 点名不纳入），
  不泛化 `_support_module_closure_offenders` 的反向检查（#1827 明确 out of scope）。
