# Tasks: db-free-loop-filtered-path-normalization

> **坐标取景**：本文件所有 `:NNNN` 均为**规划期坐标**，量于实现前基线（`origin/master`
> = `1be91bd8`，2026-08-20），**有意不随实现漂移**。定位终态代码请按**符号名** grep。
> **本豁免不适用于生产代码与测试中的注释坐标**——那些必须是终态值。
> （design.md 文末「坐标勘误」同框。）

> **round 1**（`.workplans/batch-p2/fixture-review.md`，3 P1 / 6 P2 / 11 P3）：
> 3.9 由不可满足的 ≤3.12 行为差改钉双臂同产物；新增 3.3b（M2 的真 oracle）、3.12、3.13；
> B2 补 `ValueError` 与 `(code, reason)` 几何；4.2 清单从 2 处补到 4 处；5.2 坐标全部重量。
>
> **round 2**（`.workplans/batch-p2/fixture-review-round2.md`，1 P1 / 6 P2 / 5 P3）：
> 守卫由「元组 + 反查谓词」改为**断言违规者 + allowlist**（round-1 的补救经实测不可写，
> 那四个函数无共同命名特征）；新增 3.8b（B2 复查臂**唯一**可达几何）、3.4b、3.13b；
> 3.9 降级为「本地恒真、判别力在 CI 3.11」；2.2 补 `.lower()`；2.3 明确 `except` 只收 `OSError`；
> 变异表补 M11/M12。**另修复了 round-1 修复自身写进 design.md 的一个字面 NUL 字节**
> （曾使该文件对不带 `-a` 的 grep 整体隐形，`openspec validate` 不报）。

## 1. A1 — `_db_free_selector_allowed_roots`（`services/orchestrator/retry.py:1618`，#1427）

- [x] 1.1 ENOENT 臂（`:1652-1659`）加 **loop-filtered admit**：非 strict 回退值再做一次
      `os.path.realpath(fallback, strict=True)`；**仅当**第二次也是 ENOENT（根确实尚未创建）
      **或**干净解析（`<missing>/../<real>` 形态）才 admit，否则落既有
      `db_free_allowed_root_unresolvable`。范式逐字对齐 `_local_runtime_root_safety`
      （`retry.py:1533`，PR #1426）——**不新增 code、不改签名、不改调用方**。
- [x] 1.2 第一次 strict 调用的 `except OSError`（`:1651`）扩为 `except (OSError, ValueError)`，
      `ValueError` 折进同一个 `db_free_allowed_root_unresolvable` 出口（design D4）。
- [x] 1.3 更新 `:1644-1648` 那段注释：它现在只解释了 errno 分流，**没有**解释二次复查为何必要。
      补一句说明 phantom 形态（`<missing>/../<loop>`），并说明本腿与 artifact 腿
      （`spec.md:1484` 的 admitted 残留）**故意口径不同**及其理由（design D2）。
      注释里若出现坐标，必须是**终态**值。

## 2. B1/B2/B4/B3 — path 级与构造层（#1400）

- [x] 2.1 **B1** `_db_free_selector_path_rejection`（`retry.py:1665`）：`:1683-1685` 的
      `path.resolve(strict=False)` + `except OSError` 换成 strict realpath + errno 分流；
      非 ENOENT（含 ELOOP）落既有 `db_free_selector_path_unresolvable`（**复活死码，非新增**）；
      ENOENT 走非 strict 回退 + **loop-filtered 复查**（design D2/D3）；`ValueError` 同折（D4）。
- [x] 2.2 **B2** `_db_free_path_check`（`services/orchestrator/scheduler_config.py:1153`）：
      `:1200-1209` 同范式改造。环路落 **errno 归类的** `db_free_required_path_unsafe` blocker
      （经既有 `_scheduler._scheduler_root_os_error_reason`，
      `scheduler_runtime_roots.py:410-415`，**必须显式 `.lower()`**——该函数返回大写，
      而 `_db_free_blocker` 不做小写化；同文件 `:1133` 已有 `.lower()` 先例可循），
      **不再**降级成下游的 `db_free_required_path_not_found`。
      保留既有 blocker code 与 `error_type` 证据字段。
      **`except` 元组必须含 `ValueError`**（fixture review P2-4）：该站点今天对嵌 NUL 的值
      直接抛 `ValueError`（实测 `lstat: embedded null character in path`），
      而本 change 的 spec delta 明文要求该类落同一个 unsafe blocker——不补则交付即违反自己新增的 scenario。
- [x] 2.3 **B4** `_resolve_config_path_for_mode`（`scheduler_config.py:939`）db-free 臂：
      与同函数 non-db-free 臂**取齐**（`realpath(strict=True)` → `except OSError` → 非 strict 回退），
      **不**加 errno 分流（design D8）。
      **`except` 只收 `OSError`**（round-2 P2-4）：不要写成 `except (OSError, ValueError)`
      ——handler 里那句非 strict realpath 对同一个不可表示的路径串会**再抛一次** `ValueError`，
      变成从 handler 内部逃逸，比现状更糟。B4 的 `ValueError` 逃逸**改前改后都存在**，
      本 change 明确保留（spec delta 已就此补句）。
      **注意（fixture review round-1 P3-5）**：`_optional_config_path_for_mode`（`:970`）与
      `_resolve_optional_config_path_for_mode`（`:964`）**自身没有解析逻辑**，都委托给
      `_resolve_config_path_for_mode`（`:978`）——改 2.3 即自动覆盖，**不要**在委托者里
      再写一份解析（那会造出第二处待漂移的副本）。
- [x] 2.4 **B3** `_db_free_path_identity`（`scheduler_config.py:1143`）：改为
      `Path(os.path.realpath(path))`，删掉 `try/except (OSError, RuntimeError)`（design D5）。
      **签名与消费方（`:777-810`）不动。**
      **已知且保留的既有逃逸**：`ValueError`（不可表示的路径串）改前改后**都**逃逸，
      本 change 不新增也不消除（design D5「代价与一处更正」）。
- [x] 2.5 改完后复核：`retry.py` 的 `_db_free_selector_*` 与 `scheduler_config.py` 的
      `_db_free_path_check` / `_db_free_path_identity` / `_resolve_config_path_for_mode`
      **零** `.resolve()` 调用（由 4.3/4.4 守卫）。**不含** `_safe_preserve_final_component`
      （`:932-936`）——它是另一子族，本 change 不动（design D5 措辞纪律）。

## 3. 测试（`tests/test_production_scheduler.py` 为主）

判别几何与**完整**预占门清单见 design D7；**tmp base 一律先 `os.path.realpath`**（macOS `/var` 陷阱）。

> **§3 通则（#1427 AC-3，fixture review P3-8）**：本节所有新增用例**不得**出现
> `sys.version_info` 分支——判定必须在 3.11 / 3.12 / 3.13+ 上一致。

- [x] 3.1 **A1 phantom-环路**：`_db_free_selector_allowed_roots(src, "<missing>/../<loop>")`
      → `roots == ()` 且 `rejected` 恰为一条 `db_free_allowed_root_unresolvable`（#1427 AC-1）。
- [x] 3.2 **A1 直接环无回归**：既有直接环裁定不变（对照 `tests:40457-40496` 同形断言）。
- [x] 3.3 **A1 良性 phantom 无回归**：`<missing>/../<realdir>` 仍 admitted、零 rejection
      （对照既有 `test_db_free_selector_allowed_roots_admits_missing_root`，`tests:40499`）。
- [x] 3.3b **A1 纯未创建根无回归**（**M2 的指名 oracle**，fixture review P1-3）：
      `<tmp>/not-yet/roots`（strict 与二次 strict **都**是 ENOENT 的那条臂）仍 admitted、
      零 rejection。**3.3 钉不住 M2**——良性 phantom 走的是复查成功路径，errno 谓词不被求值。
- [x] 3.4 **B1 字节兼容锚**：`<clean-root>/never-created/state.json` 仍返回 `None`，
      且解析产物与改前 `path.resolve(strict=False)` 的产物**逐字相等**（design D3 实测锚）。
- [x] 3.5 **B1 环路 path**：环路位于**干净** root 之下、`allowed_roots=` 直接传元组（绕开 A1），
      → `db_free_selector_path_unresolvable`（#1400 AC-1）。直接环形态与 `<loop>/state.json`
      两形态各一条。
- [x] 3.6 **B1 phantom-环路 path**：`<clean-root>/never-created/../<loop>` → 同上 reason
      （钉 design D2 的 recheck 裁定；M5 的指名 oracle）。
- [x] 3.7 **NUL 用例（三站点）**：嵌 `\x00` 的值在 A1、B1、**B2** 各落既有
      `*_unresolvable` / `db_free_required_path_unsafe` 出口、**无异常逃逸**
      （design D4；M6 的指名 oracle）。B2 那条系 fixture review P2-4 补入。
      **只钉「无逃逸 + 出口 code」**，不钉证据 `value` 的形状（P3-6 残留，随 5.2 路由）。
- [x] 3.8 **B2 直接环**（geometry 由 design D7 写死，实施者不得自选）：
      环路目录名**必须避开** `_DB_FREE_CREDENTIAL_WORDS`（`scheduler_config.py:43-56`）里的每个词
      ——既有 `tests:34521` 那条测试就因目录名叫 `secret-token-loop` 而根本到不了 resolve 步。
      **断言 `(code, reason)` 二元组**，不只 code：
      `('db_free_required_path_not_found','not_found')` → `('db_free_required_path_unsafe','unsafe_path')`。
      **不要用 `<loop>/child` 几何**——它今天就已经是 `('db_free_required_path_unsafe','unsafe')`
      （由父 `lstat` 门 `:1220-1231` 产出），只断 code 的测试改前改后都绿。
      **不要写字面 `..` 的 phantom 几何**——`..` 分量被 `:1190` 先拒为 `traversal`（round-2 已复验）。
      **reason 必须写成 `.lower()` 后的值**（round-2 P3-1）：
      `_scheduler_root_os_error_reason` 返回**大写**（`UNSAFE_PATH`），而 `_db_free_blocker`
      （`scheduler_config.py:1483-1501`）**不**做小写化（与 `_scheduler_root_blocker` 不同）。
      按 2.2 直写会得到 `('db_free_required_path_unsafe','UNSAFE_PATH')`，与本项断言相矛盾。
      另需**新增**一条钉住 under-loop 值的 reason 从 `unsafe` 翻成 `unsafe_path`
      ——round-2 P3-2 更正：全仓**没有**断言 B2 reason 为 `"unsafe"` 的既有测试，
      唯一那条 B2 环路测试（`tests:34519-34552`）只断 code 且被 `credential_component` 预占，
      故这是新增覆盖，不是「既有用例的翻转」。
- [x] 3.8b **B2 的 loop-filtered 复查臂**（round-2 P2-6——**这是唯一能走到该臂的几何**）：
      字面 `..` 确实被 `:1190` 拦掉，但该门只看**配置值自身的字面分量**；
      一个**目标文本**携带 phantom 形状的 symlink 可以绕过它：

      ```
      <clean>/indirect -> symlink 指向 "never-created/../ring-a"   （ring-a/ring-b 为真环）
      _db_free_local_path_component_reason(<clean>/indirect) -> None      # 门不响
      realpath(indirect, strict=True) -> ENOENT ；realpath(indirect) -> <clean>/ring-a
      realpath(<clean>/ring-a, strict=True) -> ELOOP                      # 复查在此拒收
      ```

      现状 `('db_free_required_path_not_found','not_found')` → 改后落 errno 归类的 unsafe。
      **不写这条，删掉 B2 的复查臂后所有测试仍绿**（3.8 的直接环由**第一次** strict 调用作答）。
- [x] 3.4b **B2 未创建路径无回归**（round-2 P2-5——spec delta 有该 scenario，section 3 只有 B1 版）：
      干净 root 下最终分量不存在的 required path，改后解析步仍 admit 且产物逐字相同，
      判定仍由下游给出（实测今天为 `('db_free_required_path_parent_missing','parent_missing')`）。
- [x] 3.9 **B4 双臂同产物**（fixture review P1-1 更正——初稿要钉的 ≤3.12 行为差**本地不可观测**）：
      取 `<symdir>/<loop>` 形（裸 `<realdir>/loop_a` 两臂同值，钉不住）断言
      `_resolve_config_path_for_mode(v, db_free_required=True)
       == _resolve_config_path_for_mode(v, db_free_required=False)`。
      配合 4.4 的 AST 守卫共同承担 M9。≤3.12 那一半由 design D9 的论证链结清，**不写盲测试**。
- [x] 3.10 **级联无回归**：全部 root 被拒 → selector path 侧仍落 `db_free_allowed_roots_missing`
      （#1427 AC-4，对照 `tests:40523-40544` 同形断言）。
- [x] 3.11 **journal 腿覆盖确认**：`file_orchestration_journal.py` 与 retry 腿共用同一对
      `_resolve_*_candidate`，故 3.1-3.10 一次覆盖两腿。
      **已由 fixture review 实证**：M2 变异打红了
      `tests/test_file_orchestration_journal.py::test_file_journal_manual_retry_preserves_db_free_runtime_contract`
      与 `::test_file_journal_retry_rejects_db_free_selectors_outside_allowed_roots`。
      本项只需在实现后复核该结论仍成立；若不成立，按偏离记录上报并补该腿用例。
- [x] 3.12 **#1400 AC-1 第二分句**（fixture review P3-7）：环路 selector 值
      **不再进入 `resolved` / `manifest_fields`** ——一条端到端断言（环路值缺席于提交 manifest），
      宿主取 `tests/test_retry.py` 既有 db-free 契约测试。3.5 只验了 adjudicator 孤立行为。
- [x] 3.13 **B1 非 ENOENT 非环类的收口**（fixture review round-1 P3-11）：改后 B1 拒收**任何**非
      ENOENT strict 失败（EACCES / ENOTDIR / ESTALE），不止环路；spec delta 已声明该扩面。
      钉一条（或显式记录「实践上被 root 级先答预占故不钉」的裁定），不留两不管。
- [x] 3.13b **越界环路的归因翻转**（round-2 P2-1——本 change 把 strict 解析挪到了收容门**之前**，
      于是「既是环、又在 allowed roots 之外」这一类的答话人换了）：

      | 站点 | 现状 | 改后 |
      |---|---|---|
      | B1 `<outside>/ring-a` | `db_free_selector_path_outside_allowed_roots` | `db_free_selector_path_unresolvable` |
      | B2 `<outside>/ring-a` | `('db_free_required_path_outside_boundary','outside_boundary')` | `('db_free_required_path_unsafe', <errno 归类>)` |

      对照组：**干净**的越界目录两边都仍是 outside 类（即翻转只发生在环路子类）。
      两边都是拒收故无 fail-open 风险，但这是**运维可见的 reason 变更**，必须有测试、
      有 scenario、有登记——不得作为「顺带发生」。

## 4. lane meta-guard 与既有范围声明同步

- [x] 4.1 **翻转** `tests/test_production_scheduler.py:17686`
      `test_tilde_residue_change_leaves_the_issue_1400_resolve_line_in_place`：
      `_resolve_call_names(...) == ["resolve"]` → `== []`。函数名与注释一并改成陈述终态；
      注释须引用 design D6（它是 #1436 立的范围栅栏，自带退休条件，点名 #1400 为拆除者）。
      **保留而非删除的裁定**（round-2 P3-4 指出它已被 4.4 的模块级守卫蕴含）：
      翻转是一行改动且保住了 #1436 的谱系锚点，删除则要额外向 oracle-integrity 复核
      论证「删一条测试」；两者强度差为零时选不减少测试数的那个。
- [x] 4.2 **同步全部 4 处失效的范围声明**（fixture review P2-1 —— 初稿只列了 2 处）。
      断言全部存活，失效的是**理由/范围陈述**，属注释与 docstring 的同步义务：
      - `tests:17664-17670`——把 retry 腿排除在 `_resolve_call_names` 之外的理由；
      - `tests:17436-17439`——「#1400's territory and is deliberately untouched」；
      - `tests:16527-16533` `test_db_free_config_resolve_arm_keeps_graceful_degradation_for_symlink_loop`
        ——「the fix must not drift it」，**讲的正是 B4**；
      - `tests:39652-39660` `test_db_free_config_keeps_lexical_tolerance_for_preexisting_loop_allowed_root`
        ——「the db-free arm is a declared non-goal and must not drift」。
      **遗漏任何一条 = 在测试文件里留下一句被本 change 悄悄证伪的范围声明。**
- [x] 4.3 **不要**扩展 `_TILDE_RESIDUE_EXPANDUSER_LANES`（`tests:17649-17652`）——
      round-2 P3-4 更正了 round-1 的建议：该元组自带注释声明它**只**承载 expanduser pin
      且**故意**不喂给 `_resolve_call_names`（`tests:17666-17670`），复用它会把两个
      out-of-scope 的 `scheduler_preflight` 函数永久钉进本 change 的范围，且它承载不了 4.5。
      本项只做一件事：按 4.2 同步该元组上方那段失效的注释。
- [x] 4.4 **新守卫按「违规者」而非「成员」构造**（round-2 P1-1；取代 round-1 的元组方案）：
      枚举模块内**所有**仍调用 `.resolve()` 的函数，与一份显式 allowlist 比对：

      ```python
      assert _functions_calling_resolve(retry_module) == set()
      assert _functions_calling_resolve(scheduler_config_module) == {"_safe_preserve_final_component"}
      ```

      **为何改成这个形状**：round-1 要求「完备性独立于元组」，但实测**无任何命名谓词**能
      恰好圈出 4.4 原定的那四个函数（它们既无共同前缀也无共同后缀；`_db_free_path*` 多圈一个
      `_db_free_path_evidence_scalar`，`*_for_mode` 多圈八个，两者并集也不等于那四个）。
      反转后**根本没有元组可供摘名**，M8 结构性死亡；allowlist 那一项把 design D5 的唯一
      故意例外从散文变成代码；且能抓到「往那四个函数之外的兄弟里新加一个 `.resolve()`」。
- [x] 4.5 **登记本守卫的范围裁定**（round-2 残余风险，必须是显式裁定而非副作用）：
      4.4 的形状把 `retry.py` **整模块**钉成 `.resolve()`-free，比两条 issue 各自要求的都强。
      这是**有意采纳**的——它是唯一能证死 M8 的形状，且 `retry.py` 实测改后确为零站点。
      代价写明：日后任何人往 `retry.py` 任何位置加 `.resolve()` 都会撞这道守卫，
      须走 allowlist 并留下理由。该裁定同时写进 PR body（5.1）。

## 5. 文档、留痕与路由

- [ ] 5.1 PR body「oracle 完整性」段显式记录 4.1-4.2 的**全部 4 处**测试文本改动面，
      并复述 design D6 的增强论证——**测试删除/改写面不得静默**。
- [ ] 5.2 **Phase 8 必办**：经 issue-scribe 路由**家族级教条裁决** issue——
      「ENOENT 回退是否一律需要 loop-filtered 复查」。
      **坐标已按 fixture review P2-6 全部重量**（issue 原文那批有 6 处错，不得沿用）：
      `scheduler_runtime_roots.py:506 / :572 / :597`（「有意为之」注释 `:509-511`）、
      `scheduler_preflight.py:541 / :604`（注释 `:544-547`）、
      `scheduler_config.py:1120`（A2）、`scheduler_state_failure.py:1443`、
      `workers/model_registry/basins_package.py:2770`、
      `workers/model_registry/basins_discovery.py:664`。
      另附一个**不同子族**的兄弟：`scheduler_config.py:934` `_safe_preserve_final_component`
      的 `path.parent.resolve(strict=False)`（P3-1）。
      **本 PR 关闭 #1427 会让该冲突失去 tracker，故此项不可省。**
- [ ] 5.3 PR body 记录 **B3 的 evaluate-only 裁定**（#1400 AC-5 要求「改或不改都要留痕」）
      与 **B4 的裁定**（#1400 AC-6 要求「改同范式或显式记录不改裁定及理由」）。
- [ ] 5.4 PR body 写入 **design D9 的论证性结清链**（AST 零 `.resolve()` + realpath 三臂一致 +
      CI 3.11 合并后执行），并说明它同时覆盖 **AC-3 与 B4**——两处都不存在本地可跑的测试。
- [ ] 5.5 PR body 登记两项**范围外只报不修**：
      (a) 既有 artifact 腿守卫（`tests:16005`）的完备性断言在「元组侧掉名」这一向失明，
      系 PR #1618 交付面既有缺口；(b) NUL 值原样进入 rejection 证据 `value` 字段（P3-6）。
      两项随 5.2 一并路由。

## 6. 验证（Evidence Floor）

基线（`1be91bd8`，见 `.workplans/batch-p2/baseline.txt`）：
`test_production_scheduler.py` **1697 passed**；`-k allowed_root` **68 passed / 1629 deselected**；
`test_retry.py` + `test_file_orchestration_journal.py` **495 passed**；ruff **All checks passed**。

- [x] 6.1 `uv run pytest -q tests/test_production_scheduler.py`（#1400 AC-7 / #1427 AC-5）
- [x] 6.2 `uv run pytest -q tests/test_production_scheduler.py -k allowed_root`（#1427 AC-5）
- [x] 6.3 `uv run pytest -q tests/test_retry.py tests/test_file_orchestration_journal.py`
      （B1 的第二消费者腿与 reason 断言面，见 `tests/test_retry.py:1027-1037`、
      `tests/test_file_orchestration_journal.py:3192`）
- [x] 6.4 `uv run ruff check .`
- [x] 6.5 `openspec validate db-free-loop-filtered-path-normalization --strict --no-interactive`
- [x] 6.6 变异证死：**M1-M12** 逐站点单删（**按精确源文本匹配、断言 `count == 1`；严禁按行号**），
      各自转红且由 design 变异表**指名的** oracle 证死。两处已登记的弱证死点：
      M10（B3）与 M9（B4）在本地都只能由 4.4 的违规者守卫证死——它们的行为在 3.14 上
      改前改后全等（round-2 P2-3 实测：3.9 在基线、改后、M9 变异下**三者皆绿**），
      判别力落在 CI 3.11 臂。**不得**因此把 3.9 记作本地 oracle。
- [x] 6.7 oracle 完整性：`tests/` 的删除/放宽面逐条复核，4.1/4.2 之外**零**断言被删除、放宽或改窄。
