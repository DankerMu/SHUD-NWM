# Tasks: registry-retire-declaration-channel

## 1. 实现

- [ ] 1.1 schema（design D1）：`scheduler_registry_package_cutover.schema.json`
      加法扩展——`transition_mode` enum + `"retire"`；`new_checksum`
      anyOf[hex, null]；**`allOf` 包两条 if/then** 钉 mode↔checksum 形；
      其余逐字不动。`schemas/examples/…cutover.example.json` **就地**加一条
      retire entry（CI 同名配对，勿新建示例文件）。
- [ ] 1.2 加载层（D1）：refresh 侧 `CUTOVER_TRANSITION_MODES` 加 `"retire"`
      （:108）；`_load_cutover_declaration` per-entry 镜像 schema 条件
      （retire ⇒ None、replace ⇒ hex）；`:2301-2303` receipt 侧改**按桶**
      校验（cutovers 只许 replace、retirements 只许 retire）；核查其它
      共用常量消费点无意外放宽（结论记 PR body）。
- [ ] 1.3 判据层（D2）：`_classify_registry` 规则 1 retire 豁免（membership
      改查 removed；仍在 prospective / 不在 previous 均 invalid）+ 规则 4
      重写为**两遍法**（R2-1 选 (a)：第一遍判 retire checksum 不符置污染
      位 + refuse_declaration 行，第二遍仅在污染位终值 False 时入桶——
      入桶与否不依赖 removed 行序；污染时全部匹配 entry refuse 不进桶
      （F5）；checksum 不符的 removal 不另出 removal_refused 行（F7）；
      无 entry/replace entry ⇒ 逐字保持 removal_refused；规则 2 单遍现
      行为零改动）；分类段与 dry_run 提前返回逐字不动；拒因阶梯语义不变。
- [ ] 1.4 证据层（D3，F1 路线）：`publish_all_basin_scheduler_registry` 加
      **out-sink 可选参数**（默认 None，与 cutover_gate/
      registry_commit_observer 同形），三处 skip 时点喂行（键名与 `:675`
      一致）；refresh 闭包 nonlocal 承接后以关键字参数传
      `_registry_precommit_gate`；**3 参回调契约与测试 double 逐字不动**；
      gate 侧 removal refusal entry 增补三键（有行时）；列表键定显式上限
      （D3，实现时指认并记录）。
- [ ] 1.5 对账层（D4 全枚举）：`ClassificationResult.declared_retirements`
      + `to_receipt` 桶；新桶进 `_CLASSIFICATION_OPTIONAL_KEYS`（:1877，
      **勿进 required**）；新增容缺访问器（缺键 ⇒ 0/[]）；reconciliation
      增 items ⊆ removed 约束 + **显式拒 `retired_total > removed_total`
      （F4）** + refused 下界扣减 + id-only 恒 0 + legacy 无桶按 0；
      `_validate_object_group` 调用处 `optional_keys` 扩三键（:1964-1970）；
      receipt JSON Schema 四处同步（registry_classification 可选桶、
      refused_group 三可选键、新建 nullable retire `$defs`、不动 cutover
      组 hex 约束）；`_CUTOVER_GATE_KEYS` 零改动。
- [ ] 1.6 runbook（D5）：`current-production-ops.md:508-510` 触发面补全 +
      retire declaration 恢复顺序 + `--allow-uncovered-cutover` 降级标注
      （保持红旗定性）+ `:500-504` 桶枚举加新桶 + 共享消费者一句提示。
- [ ] 1.7 共享消费者（D2/F2）：`services/orchestrator/scheduler_generation.py`
      ——其 `CUTOVER_TRANSITION_MODES`（:143）加 `"retire"`；
      `match_declaration_entry` 链对 retire entry **在 `:816`
      `str(entry["new_checksum"])` 之前短路跳过**（永不匹配任何候选、无
      `"None"` 字符串产物）；replace entry 行为零改动。

## 2. 测试（design D6；先红后绿——绿分支接线前红**分两段**（F6）：
1.1/1.2 前红在加载层（schema enum 拒 → declaration=None →
`__declaration__` 合成行 + removal_refused 行）；1.1/1.2 后 1.3 前红在
规则 1（membership invalid，refused 行带 entry checksum）——两段分别记录）

- [ ] 2.1 端到端双分支主锚：previous={alpha,bravo}、bravo 包 invalid 被
      publish skip——(a) 无声明 ⇒ removal_refused + skip-cause 三键 +
      canonical 字节不变零发布（现状锚定，修后仍成立）；(b) 合法 retire
      declaration ⇒ refresh 成功、canonical 少 bravo、receipt
      declared_retirements=[bravo]、alpha 正常发布（两段红证按上记录）。
- [ ] 2.2 `_classify_registry` 判定表十一格（D6 表）逐格；格 4（replace
      entry 命中 removed）双拒并存形、格 10（规则 1/2 来源污染形）、格 11
      （retire 内部污染 + removed 两行序同结果，两遍法确定性）单独钉。
- [ ] 2.3 schema/加载层格：retire+null 通过 / retire+hex 拒 / replace+null
      拒 / 既有纯 replace v1 文件逐字通过 / enum-常量相等钉测（refresh 与
      scheduler_generation 两份常量都与 schema enum 相等）。
- [ ] 2.4 对账层格：诚实 retired receipt 通过 / retired ⊄ removed 拒 /
      **retired_total > removed_total 拒（F4）** / id-only 非零 retired 拒 /
      legacy 无桶通过 / refused 下界扣减；receipt JSON Schema 格（带新桶
      receipt 过 schema、伪造 cutover 组 retire 行被按桶校验拒）。
- [ ] 2.5 既有锚零改动确认：`:3198` removal 用例、undeclared 族、audit 块
      族、publish 既有断言 diff 级不动全绿。
- [ ] 2.6 证据判别格：目录被删形（无 inventory 行）refusal 无三键；skip
      形有三键——两形可辨（D3）。
- [ ] 2.7 共享消费者格（F2/I8）：retire entry 在位时
      scheduler_generation 文件正常加载、replace entry 照常匹配、retire
      entry 永不匹配、无 `"None"` 字符串产物
      （`tests/test_scheduler_generation.py` 补，现有 `:199` "rebase" 用例
      覆盖不到）。

## 3. 验证（Evidence Floor，per issue Verification）

- [ ] 3.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py
      tests/test_publish_scheduler_file_registry.py
      tests/test_scheduler_generation.py` 通过。
- [ ] 3.2 `uv run ruff check .` 通过。
- [ ] 3.3 `openspec validate registry-retire-declaration-channel --strict
      --no-interactive` 通过。
- [ ] 3.4 JSON Schema 校验链绿（结论已定：CI json-schema-validate 覆盖，
      example 就地扩展后被同名配对校验）：本地等价
      `check-jsonschema --check-metaschema <schema>` +
      `check-jsonschema --schemafile <schema> <example>` 两条过。
- [ ] 3.5 PR body 记录：1.2 常量消费点核查结论、receipt schema 四处改动
      清单、D8 已知残余（退役行的 state snapshot 孤儿 + orchestrator 缺行
      行为）、issue 验收逐条映射。
