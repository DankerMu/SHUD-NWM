# Tasks —— ENOENT 非严格兜底的家族级裁定（#1627）

## A —— 裁定文档（编排者）

- [x] A-1 `docs/adr/0009-path-canonicalization-dereference-doctrine.md`：裁定、**三条**从句、
      19 成员快照表（逐行归到具名从句）、wrapper 消费者清单、三个立场的去向、
      两个子族的定性、三条 report-only、以及给下一个作者的三条情报。
- [x] A-2 tracker 指针双边闭合。**终态一律指向 ADR 0009，不指向任何 issue**
      （该 change 合并后 #1627 关闭）。**指向 spec 一律用 Scenario / requirement 名锚，
      不用行号**——被指文件同在本次改动范围内，行号会被写入它的那次编辑顶走。
      - [x] A-2a `openspec/specs/slurm-array-runner-integration/spec.md` 的 Scenario
            「parent-segment loop no longer aborts construction on 3.11/3.12」
      - [x] A-2b `openspec/specs/runtime-evidence-and-operations/spec.md` 的 Scenario
            「unexpandable tilde in allowed storage roots is tolerated without crashing the preflight」
      - [x] A-2c `openspec/specs/runtime-evidence-and-operations/spec.md` 的 requirement
            「DB-free scheduler config path adjudication survives symlink loops」
            ——`archive/2026-09-02-journal-root-realpath-and-job-id-scope-census/design.md:152-155`
            把 `_db_free_path_check` 的义务派给了 #1627 并指向该 requirement。
            **裁定：该 requirement 正文不含 #1627，无需改写**；缺的是可追溯性，
            已由 ADR「权威集合是守卫的嗅探边界」一节的消费者表闭合。
      - [x] A-2d 以下 5 处 **tracker 语义**的活引用（实现者改，见 B-6）：
            `services/orchestrator/journal_root_authority.py:79`、
            `tests/test_scheduler_journal_root_authority.py` 的模块 docstring、
            `::test_alias_ancestor_root_passes_db_free_path_check` 与
            `::test_symlink_leaf_and_loop_roots_are_refused_with_the_same_code` 的 docstring、
            `tests/test_production_scheduler.py::test_tilde_residue_preflight_allowed_roots_is_admitted_by_the_existing_enoent_arm`
      - [x] A-2e 以下**不改**（属出处/沿革，非 tracker）。C-1 接线后该类引用从 3 条增至 40+ 条，
            均为 selector 路由的出处注释，不是 tracker：`services/orchestrator/scheduler_core.py:24`、
            `docs/adr/0003-review-lens-rotation-keep.md:185`、`openspec/changes/archive/**`
      - [x] A-2f 合并前重跑全仓 `grep -rn '#1627'`，结果随 PR body 交付。
            **实测：零条 live tracker 指针**；剩余全部是沿革注释（`scripts/select_ci_tests.py` 与
            `tests/test_select_ci_tests.py` 的 C-1 路由出处、`scheduler_core.py`、
            `docs/adr/0003`、ADR 0009 自述其单号），与 `#1656`/`#2185` 在同一文件里的写法同源。
- [x] A-3 `openspec/specs/job-retry-mechanism/spec.md` 的立场 C 措辞：按验收项「保留或改写，
      二选一并说明」——**保留**，ADR 写明它已被实核为立场 B 的实例（依从句 1，`:1401`）。
- [x] A-4 `openspec validate enoent-fallback-family-ruling --strict --no-interactive` 通过。

## B —— 守卫与注释（implementer）

- [x] B-1 AST 扫描器：枚举「调用 `os.path.realpath` 的 (模块, 函数) 对」，形状无关，
      覆盖 `services/`、`workers/`、`packages/`、`apps/` 四棵树。
- [x] B-2 守卫测试：断言集合内每个成员**至少有一处 `strict=True`**；违规者集合为空。
      **具名白名单恰好三项**（每项带理由注释 + 所依从句）：
      `journal_scope_census::_require_output_outside_root`（从句 2）、
      `shud_preflight::check_shud_executable`（从句 1）、
      `scheduler_config/db_free.py::_db_free_path_identity`（从句 1）。
      断言违规者集合，**不**断言成员清单（design D3）。
- [x] B-3 红证据（两向实跑，不接受结构性论证）：
      - [x] B-3a 新增一个只写非严格 realpath 的桩 → 守卫失败；删桩 → 恢复绿。
      - [x] B-3b 从白名单删掉 `_db_free_path_identity` → 守卫失败；恢复 → 绿。
            （证明三项白名单**每一项都承重**，不是凑数。）
- [x] B-4 `services/orchestrator/scheduler_config/path_modes.py::_resolve_config_path_for_mode`
      的注释补 ADR 0009 指针（符号锚：该文件在本次改动范围内，行号锚会被顶走）。
- [x] B-5 `services/orchestrator/journal_scope_census.py` 在 `:557` 的 docstring 留一条锚：
      该站点依从句 2，且该从句**不覆盖 check-then-act 竞态**（design D5.2、ADR 0009「已知限制」3）。
- [x] B-6 A-2d 的 5 处 tracker 引用改指 ADR 0009。**只改措辞，不动任何断言或行为。**

> **定性已在本 fixture 内完成，不作为实现阶段的任务下派**：`_safe_preserve_final_component`
> 的消费者链与跨解释器行为见 ADR 0009「子族定性」，实现侧无需复核，也不得据此改行为。

## C —— 守卫接线与站点标记

- [x] C-1 守卫接入 PR-time CI 选择（`scripts/select_ci_tests.py` supplemental 路由 +
      `tests/test_select_ci_tests.py` 从守卫自己的 `_SCAN_ROOTS` AST 推导所需根的元测试）。
      接线前实测：8 条路径全部不选中该守卫，即「新站点被拦」这个场景在合并门上恒绿。
- [x] C-2 19 个权威成员各写 `ADR 0009 clause N` / `ADR 0009 loop-filtered` 处置标记，
      并给守卫加第二条断言（无标记者集合为空，函数区间内检索，裸 `ADR 0009` 不算过）。
      理由：delta spec 的「recorded at the site」若只是一条散文 SHALL，
      就没有任何东西能让它变红。
- [x] C-3 live spec 的跨解释器 `lstat` 断言补仓内实测凭据，不转述仓外报告的结论。

## D —— fixture 收敛与 selector 修复

- [x] D-1 design.md 不保留任何一段与 ADR 重复的论证，一律改为指针：
      **两份手抄件必然各自漂移**，而漂移后两份都还是绿的。
- [x] D-2 ADR 与本 fixture 不写关于本 change 自身产物的计数——那类数字在仓里
      没有任何权威可以回算。
- [x] D-3 守卫断言二收紧两处（`tokenize.COMMENT` 判定；同限定名多次绑定判红），
      delta spec 正文与 Scenario 同步补齐。
- [x] D-4 新增测试文件的三条 importer 边未在 selector 立处置，使
      `tests/test_select_ci_tests.py` 5 红。处置：subject 边加 per-file 规则，
      两条 import-order 夹具边立 `edge-consumer`。任何被改动的 `tests/` 文件都会把
      selector 元守卫累积进选择集（`scripts/select_ci_tests.py` 的
      `SELECTOR_META_GUARD_TEST`），故该回归在定向 CI 上可见。
- [x] D-5 从 live spec `runtime-evidence-and-operations/spec.md` 移除
      「a phantom base admits no real object」——该性质对 `<base>/missing/../real`
      这类输入为假（ADR 0009 从句 3）。

## Evidence Floor

- [x] EF-1 `uv run ruff check .` 干净。
- [x] EF-2 `uv run pytest -q <守卫测试文件>` 通过。
- [x] EF-3 B-3a / B-3b 的红证据两向实测输出（不是论证）。
- [x] EF-4 `openspec validate enoent-fallback-family-ruling --strict --no-interactive` valid。
- [x] EF-5 权威集合基数实测值随 PR body 交付；若与 design D2 的 19 不符，
      **以实测为准并改 design**，不得反向修饰。
- [x] EF-6 ADR / design / proposal / tasks / delta spec 内**带路径的** file:line 引用由脚本逐条现证。
      **覆盖面说准**：脚本只提取 `<路径>:<行>` 形，**不提取**裸续锚（`:806` 这类，
      从上文继承路径）与 `#N`。两类的处置：裸续锚承重的那些手工现证；
      `#N` 由 A-2f 的全仓 grep 与 EF-7 覆盖。
      **回执：`openspec/changes/enoent-fallback-family-ruling/cite-check-receipt.txt`**
      （机械校验器输出，随 PR 入库并随 change 归档；不放 `.workplans/`——那是 gitignored，
      指向它等于指向仓外，正是本项要避免的失败形态）。
      回执由 `scripts/cite_check.py` 生成，脚本随 PR 入库，**故归档后仍可复跑**；
      一份没有生成器的回执等于一份不可复核的证据。
      **本项的判据是该脚本的退出码，不是一句自述**：在终态树上
      `uv run python scripts/cite_check.py <ADR + 四份 fixture>` **exit 0**。
      缩写路径（`config.py` 这类，tracked 树里同名文件不止一个）一律在正文写成全路径
      ——工具报歧义时要改的是散文，不是工具。
      工具**判不了**「解析到的那一行是否真支持引用它的那句话」，那仍是人读回执。
- [x] EF-7 A-2f 的全仓 `#1627` grep 结果为空或仅剩 A-2e 的沿革引用。
- [x] EF-8 C-1 的 selector before/after 实测表（接线前后各路径选中数 + 守卫是否被选中）。
- [x] EF-9 C-2 的红证据三向实跑：删标记 → 红；标记退化为裸 `ADR 0009` → 红；
      标记挪到函数体外 → 该成员仍报违规。三者还原后均绿。
