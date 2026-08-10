# Design: forcing-package-neutral-identity

## 风险三角与档位

- Fixture level: **expanded**(profile 触发词 `forcing`/`shud_runtime`/`run_manifest` 命中;跨 producer/runtime/mapping_builder 三 worker + 两份 live spec + 8 个测试文件;issue 无 Suggested fixture level 字段,无分歧)。
- 风险轴:契约漂移(单一事实源建立)、fail-closed 完备性(歧义/缺失两翼)、legacy 兼容(历史 package 不可破)、豁免面误伤(QHH 真资产)、测试断言大面迁移的 oracle 完整性。
- Seams under test:`format_shud_forcing_package` 写点、manifest role 装配环、`_stage_standard_shud_forcing` 探测、`_direct_grid_runtime_checksum_entries` 必需成员门、`_direct_grid_sensitive_member_limit`、两个保留名集。
- 风险包:run-manifest/QC provenance(选,manifest 成员身份即本题)、oracle-discrimination(选,35+ 断言迁移必须净等价迁移而非削弱)、spec-compliance(选,双 spec delta);geospatial/数值包不选——坐标、插值、数值零触碰(Non-Goal)。

## D1 canonical identity(契约决策,AC-1 的"经评审 contract")

canonical 主索引成员固定为 **`shud/stations.tsd.forc`**:

- 流域中性:不含任何 basin token(issue In-Scope 硬性要求,排除 `{basin_slug}.tsd.forc` 参数化方案——参数化名会让"文件名 == basin 证据"的误用模式换壳复活,且 runtime 需从 manifest 反查名字,引入新耦合)。
- 语义自描述:该成员内容就是 SHUD 主站点索引表(站点行 + CSV 文件名列)。
- 无碰撞:与既有保留名(`forcing_package.json`/`forcing_debug.csv`/`forcing.tsd.forc`/`manifest.json` 等)及站点 CSV 默认名 `forcing_NNN.csv` 均不冲突;`.tsd.forc` 后缀保留 suffix-glob 类消费者(`runtime.py:550`、`provision_direct_grid_scheduler_registry.py:216`、`object_store_validation.py`)零改动兼容。
- **package 成员名是纯运输身份**:runtime staging 读取成员后写出 `{project}.tsd.forc` 到模型输入目录(现状,零改动),SHUD 模型侧命名(如 QHH 项目的 `qhh.tsd.forc` 真资产)不受影响——这是"改名不触模型语义"的结构保证。

## D2 共享契约常量模块(单一事实源)

新增 `packages/common/shud_forcing_contract.py`,**纯常量、零 import**(除 `__future__`),排除循环依赖:

```python
CANONICAL_SHUD_FORCING_INDEX_MEMBER = "shud/stations.tsd.forc"
LEGACY_SHUD_FORCING_INDEX_MEMBER = "shud/qhh.tsd.forc"
SHUD_FORCING_INDEX_MEMBERS = (CANONICAL_SHUD_FORCING_INDEX_MEMBER, LEGACY_SHUD_FORCING_INDEX_MEMBER)
CANONICAL_SHUD_FORCING_INDEX_BASENAME = "stations.tsd.forc"
LEGACY_SHUD_FORCING_INDEX_BASENAME = "qhh.tsd.forc"
SHUD_FORCING_INDEX_BASENAMES = (CANONICAL_SHUD_FORCING_INDEX_BASENAME, LEGACY_SHUD_FORCING_INDEX_BASENAME)
SHUD_FORCING_ROLE = "shud_forcing"
```

消费方(全部改引常量,删除本地字面量):`producer.py:1986/:2609/:2768`、`runtime.py:974/:1795/:1800/:1830/:1953/:3748`(:944 报错文本引用常量插值)、`binding.py:270-272`(`RESERVED_FORCING_FILENAMES` 由常量集合并集构造;r2 残余风险注:字面名 `stations.tsd.forc` 的拒绝理由将从 `reserved_suffix` 变为 `reserved_exact_match`(:1690-1699 exact 先判)——两者均 fail-closed 无行为回归,实现时 grep evidence/receipt 面确认无载荷记录该 reason 字符串,有则登记偏离)、`scripts/create_qhh_shud_manifest.py:319-324`(**fixture-r1 C1:必须迁移**——它是 producer 新鲜 manifest 的消费者,`run_qhh_backend_smoke.sh:158-169` 与 `run_qhh_cycle.sh:499/:509` 同 cycle 内先 produce 后调它,legacy 硬编码会在 producer 停产 legacy 后使链硬断;改为双名恰一判定,与 D4 同契约;同文件 `:132` `station_source` 改记实际命中成员 basename,防触碰文件自相矛盾,r2-note;:327-337 header 校验散文不动)。注释散文同步面(r2-note):`runtime.py:55/:1862`、`binding.py:31`、`rewrite.py:702`(纯注释,canonical 措辞 + legacy 兼容注)。`file_store.py:843` **不改**(C3,DB/display 可见的站点 provenance 标签,超 In-Scope,proposal 已登记残余)。三 worker 均已依赖 `packages.common`(binding.py:197-201、runtime.py:21-36、producer 既有导入;C8 锚点修正),导入面无新增边。

## D3 producer 侧

- `format_shud_forcing_package` 写点:`files[CANONICAL_...] = tsd.getvalue()`——新 package **只含 canonical,不含 legacy**(双写会直接触发 runtime 恰一决断拒绝,见 D4;这是防"过渡期双名并存"的结构闸)。
- role 装配(:1986):`"role": SHUD_FORCING_ROLE if relative_path == CANONICAL_... else "shud_forcing_csv"`。producer 永不产 legacy,故此处只比对 canonical;role 名 `shud_forcing` 零改动(manifest 消费者兼容)。
- 保留名集 `_reserved_shud_station_filenames()`(:2768):替换为 `{*SHUD_FORCING_INDEX_BASENAMES, "forcing_package.json", "forcing_debug.csv", "forcing.tsd.forc"}`——canonical 与 legacy basename **都**保留(站点 CSV 永不得撞任一索引名)。
- producer 内 :1910 注释散文同步 canonical 措辞(C8:此前所引 :1862 非 prose 命中点,唯一 producer prose 位于 :1910)。

## D4 runtime 双名恰一决断(fail-closed 两翼)

统一谓词:成员 `relative_path ∈ SHUD_FORCING_INDEX_MEMBERS`,且**恰好一个**通过。三个消费层同规则:

1. **manifest 必需成员门**(`_direct_grid_runtime_checksum_entries`,:1795-1830):
   - 过滤 `entry["relative_path"] in SHUD_FORCING_INDEX_MEMBERS`;
   - 0 条 → **保持现状返回 `[]`**(fixture-r1 C2:该层今日即空返回,`FORCING_CHECKSUM_MISSING` 只在索引成员存在而派生 CSV 缺席时于 :1845-1849 触发;把空返回改 raise 是对既有路径的未申报行为变更,禁止——direct-grid 缺成员的终态裁决在 staging 层 :942-946 的 `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING`,报错文本改列两名);
   - **canonical 与 legacy 并存**(两个不同成员名各≥1 条)→ **新错误码 `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`**,fail-closed,错误文本列出命中的成员路径集合;
   - **同名重复条目** → 现有 `FORCING_CHECKSUM_INVALID` **原样保留**(fixture-r1 C5::1833-1842 的 duplicate 检查覆盖索引 + 全部站点 CSV,不得并入新码——并入会静默改写站点 CSV duplicate 的错误码;鉴于该码现无测试钉住,本 change 补 pin 测试 B4b)。`required_relative_paths`(:1830)改为"命中的那一个"。
   - 限长读门与 `_direct_grid_sensitive_member_limit`(:1953/:3748):对两名等价生效(`in SHUD_FORCING_INDEX_MEMBERS`),同一 `MAX_DIRECT_GRID_TSD_FORC_BYTES`/`DIRECT_GRID_TSD_FORC_TOO_LARGE` 语义。
2. **staging 文件系统探测**(`_stage_standard_shud_forcing`,:974;**round-1 A1/A2 修正**):双名探测;**歧义 raise 仅限 direct-grid**(`DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`,磁盘层独立裁决——DG staging 本是 manifest 白名单制,双文件意味着更深的污染)。**非 direct-grid 是全前缀递归拷贝**(:1679-1693 无白名单),producer 前缀确定且从不清理(:1970,零 delete 调用),原地再生产会留下孤儿 legacy 成员——双文件在非 DG 属**合法稳态**,按 **manifest 锚定选择**(**round-2 R1/S1 修正锚源**):声明源是**校验和已验证的 package manifest**——`forcing_context.package_manifest["files"]`(下载时经 `_verify_package_manifest` 校验),**不是** run-manifest 的 `forcing.files`(生产装配器 `chain_manifest_contracts.py:15-35` 只发 uri/checksum 字段、从不发 files;仅诊断脚本 `create_qhh_shud_manifest.py:135` 写它)。回退链:package manifest 发布非空 `files` 列表时以它为准(**不论**其是否命名 index 成员;仅 `files` 缺失/空/非 list 时才退下一级)→ run-manifest `forcing.files`(诊断 lane)→ 所选声明源未恰一命名时 canonical 优先兜底(manifest 与 canonical 同笔写入 :2100-2105,manifest-current == 最后一次 produce,legacy-current manifest 不可能伴随新写 canonical;manifest 锚定同时关闭 producer 降级回滚留下 stale canonical 的残洞——盲目 canonical 优先关不掉)。恰一 → 用之;声明**双名**(非 DG)→ canonical 优先选择——**有意与 DG 的恰一 fail-closed 门相异**(round-2 S5:非 DG 是容忍 lane,残余双名属合法稳态,fail-closed 会砖死本可运行的 run);零 → 返回 None(direct-grid 由 :941-946 现有 `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING` fail-closed,非 direct-grid 的 legacy internal-forcing 回退现状保留)。`_prepare_shud_project_forcing`(:927-940)把上述回退链解出的 checksum entries 传入 staging 调用;**禁用** `_authoritative_package_manifest_checksum_entries`(其在 package_manifest 缺失时抛错,会把非 DG lane 新引入 fail-closed)。
3. **identity/checksum 一致性**(**round-1 B2 修正**——原"staged 校验循环 :1926-1970 天然覆盖"声称有误,该循环在此场景不可达):实际裁决层是 `_direct_grid_runtime_checksum_entries` 的对象读取(:1837,先于 staging);manifest 声明 A 而对象树只有 B 时,现状 blanket `except Exception`(:1836-1842)把缺失误报为 `DIRECT_GRID_TSD_FORC_TOO_LARGE`。修复:读取前对声明成员做存在性探针(`object_store.exists`),缺失 → `FORCING_CHECKSUM_READ_FAILED`(与非敏感成员路径 :2014-2021 同码),报错文本点名声明成员;except 子句本体保留(合法超限路径仍经它,6 处既有 TOO_LARGE 断言不动;ObjectStoreError 分型方案因 139 处引用爆炸半径被否决)。station CSV 侧同型 except(:2023-2030)不改——identity-mismatch 类对内容派生名不适用,留注说明。歧义翼由 1/2 双层把守。
- `:944` fail-closed 报错文本更新:列 canonical 为主、legacy 为兼容名。
- 新错误码为 `SHUDRuntimeError` 自由字符串码(与既有码同机制);实现时 grep 确认无错误码闭集枚举/schema 约束(receipt/事件面若存在码白名单则登记并补入,发现即在 PR 偏离记录报告)。

## D5 legacy 兼容策略与截止条件(AC-1 的 transition policy)

- **读侧**:runtime 无限期接受恰一 legacy 成员的历史 package(object-store 历史 package 不重写,Non-Goal);legacy 兼容断言由**显式 runtime 级测试**承担——标准 helper(`tests/test_shud_runtime.py` `_write_standard_shud_forcing` :102-150)以 legacy 名构造完整 package 走 staging + checksum 门(B3)。既有录制 fixture(`tests/fixtures/forcing_domain_handoff/complete/**`、`station_series_baseline_heihe_ifs_2026060100.json`)**保持原样但不承担兼容断言**(fixture-r1 C6:前者的消费测试只复验录制 checksum/形状、后者的 `qhh.tsd.forc` 是站点 property,均不经过成员解析路径)。
- **写侧**:合并即停产 legacy,无豁免(C1:原"诊断脚本产物走 legacy lane"表述错误——该脚本是消费者不是生产者,已列入 D2 迁移面)。
- **截止条件**(文档化,不设代码定时器):当 object store 中不再存在含 `shud/qhh.tsd.forc` 成员的 direct-grid package(核验:按 forcing package manifest 扫描 `files[].relative_path`),legacy 分支可由后续 change 移除;该条件写入 `docs/modules/04_forcing_production_design.md` 迁移小节。不预开跟踪 issue(无到期信号源,YAGNI;残余已在 proposal 登记)。

## D6 considered-and-rejected

- **manifest 顶层 schema_version 门控兼容**:`forcing_package.json` 今日无版本字段;为本迁移引入整份 manifest 版本化是更大契约变更且恰一成员规则已自描述(成员名即版本信号)——拒绝,登记于 proposal Non-Goals。
- **producer_version 升版**(fixture-r1 C10):`producer.py:427` `m2.1` **有意不升**——:2143-2145/:2176-2177 以 producer_version 判 package 是否现行,升版会使全部历史 package 判过期重算,违反"不重写历史 package"Non-Goal;代价是同一 producer_version 内布局二态,由成员名自描述。既有 fingerprint 守卫(test_forcing_producer.py:5256-5350)只钉单位/系数,不受影响。
- **role-driven 发现**(runtime 按 `role == "shud_forcing"` 查名):staging 层是文件系统探测(解包后、无 manifest 上下文),改 role-driven 需重排 staging 数据流;字面量漂移风险已由 D2 单一事实源消除——拒绝(KISS)。
- **`{basin_slug}.tsd.forc` 参数化名**:见 D1,issue In-Scope 明文排除。

## D7 测试锚点(B 系列;授权改写范围)

**授权改写**:package-contract 测试中 `qhh.tsd.forc`/`shud/qhh.tsd.forc` 的**新 package 语义**断言(test_shud_runtime 35 处中的新包面、test_forcing_producer 9 处、test_mapping_builder_binding 3 处、**test_direct_grid_e2e.py:137 一处**——producer 真实产物读点,r2-C12)迁移为 canonical 常量引用;`tests/test_qhh_scripts_static.py` 允许**仅为 C1 迁移**扩展(双名恰一判定的静态锚 + legacy 仍被接受的断言,既有断言零削弱);`_write_standard_shud_forcing` helper(tests/test_shud_runtime.py:101-~165,成员名硬编码于 :129/:137/:138)**参数化成员名**(默认 canonical)供 B2/B3 复用,不 fork helper(r2-note)。**禁改**:test_qhh_production_bootstrap(14 处)、orchestration_chain 的 `shud_station` 透传数据、legacy 录制 fixture JSON、`tests/test_direct_grid_evidence_smoke.py`(C4:node-22 手工装配证据树 `forcing/qhh.tsd.forc`,非 producer 成员且 env-gated)、`tests/test_object_store_forcing.py`(C6:唯一命中是站点 property)、`tests/test_forcing_domain_handoff_apply.py`(r2-C13:两处均为真资产 `source_file` provenance,镜像 qhh_production_bootstrap.py:1442,与 C3 同豁免类)。**例外钉**(r2-note):`test_forcing_producer.py:2267` 的 `"forcing_filename": "qhh.tsd.forc"` 是保留名撞名用例——**不整替**,保留为 legacy 拒绝面并新增 canonical 兄弟用例(B7 措辞优先于"9 处整迁")。断言迁移必须**净等价**(路径串替换,断言结构/强度零削弱)。

- B1 producer canonical 写点钉:QHH 与非 QHH 双 deterministic fixture 各产 package → 成员名 == canonical、role == `shud_forcing`、manifest checksum 与内容一致、站点行来自各自输入(站点数/坐标区分两 fixture)——AC-2。
- B2 runtime canonical package 端到端:staging 发现 canonical、checksum 门通过、staged 目的地 `{project}.tsd.forc` 内容与成员一致。
- B3 runtime legacy package 兼容:恰一 legacy 成员的 package(标准 helper 以 legacy 名构造,走 staging + checksum 全路径)行为与今日一致——AC-3 legacy 合法面(C6:显式测试,不依赖录制 fixture)。
- B4 manifest 歧义 fail-closed:canonical+legacy 双条目 manifest → `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`,错误文本含两路径。
- B4b 同名重复钉(C5 新增 pin):同名双条目 → 现有 `FORCING_CHECKSUM_INVALID` 保持(该码此前无测试钉住)。
- B5 磁盘歧义 fail-closed:解包后两文件并存 → `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`(staging 层独立裁决)。
- B6 缺失 fail-closed(C2 修正):direct-grid 零索引成员 → staging 层 `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING`(:942-946,报错文本双名);manifest 门空命中保持返回 `[]` 现状(以断言钉住"不 raise"防未申报行为变更)。
- B7 保留名钉(C9 收窄):producer `_validate_unique_station_forcing_contract` 对 canonical 与 legacy basename **都**拒绝站点撞名;binding 侧因 `RESERVED_FILENAME_SUFFIXES` 已含 `.tsd.forc`(:290-301,:1696)任何 `*.tsd.forc` 站名今日即被拒——binding 断言改为直接钉 `RESERVED_FORCING_FILENAMES` 集合由共享常量构成(单元级)。
- B8 限长读门对两名等价:canonical 超限 → `DIRECT_GRID_TSD_FORC_TOO_LARGE`(legacy 面既有测试保留即可)。
- B9 豁免面回归:`tests/test_qhh_production_bootstrap.py` **零改动**全绿;`tests/test_qhh_scripts_static.py` 仅含 C1 授权扩展、其余断言零改动全绿。
- B10 QHH 诊断链恰一迁移钉(C1):`create_qhh_shud_manifest.py` 成员判定接受 canonical 或 legacy 恰一、双存拒绝、缺失报错文本双名(静态锚或单元级,沿该测试文件既有形态)。

**Round-1 修复新锚(A1/A2/B2/V3-1)**:

- B11 identity-mismatch 双向钉(B2):manifest 声明 canonical/对象树只有 legacy、及反向,均 → `FORCING_CHECKSUM_READ_FAILED` 且消息点名声明成员(参数化一测)。
- B12 非 DG 双成员 manifest-canonical 钉(A1 回归锚):`shud_project` 无 DG 元数据、双文件 staged、manifest 声明 canonical → 走 `prepare_workspace` 全路径成功,`{project}.tsd.forc` 内容源自 canonical 行。
- B13 非 DG 双成员 manifest-legacy 钉(降级残洞面):历史 legacy manifest + 孤儿 canonical → 按 manifest 锚定 stage legacy,成功。
- B14 非 DG 单成员钉:canonical-only 与 legacy-only 各自成功(既有参数化覆盖为 DG lineage,需非 DG 变体)。
- B15 DG 双文件 raise 保持:既有 B5 锚零改动继续绿。
- B16 诊断脚本 header 消息插值钉(V3-1):header 失败消息含 resolved member basename(`test_qhh_scripts_static.py` 既有 `match="station header"` 断言零削弱)。

## D8 突变击杀集(shasum 还原校验)

- N1 D4-1 歧义判定删除(canonical+legacy 并存不报)→ B4 死。
- N2 producer 写点回退 legacy 名 → B1 死。
- N3 **producer** 保留名集漏 canonical basename → B7 producer 面死(C9:binding 侧被 `.tsd.forc` 后缀规则遮蔽,不作为击杀面)。
- N4 runtime 成员集合删 legacy → B3 死。
- N5 staging 双文件裁决删除 → B5 死。

## D9 evidence 映射

- 本地:`uv run pytest -q tests/test_forcing_producer.py tests/test_shud_runtime.py tests/test_qhh_production_bootstrap.py tests/test_qhh_scripts_static.py tests/test_mapping_builder_binding.py tests/test_forcing_domain_handoff_apply.py tests/test_direct_grid_e2e.py tests/test_object_store_forcing.py tests/test_orchestration_chain.py` + `uv run ruff check .` + `openspec validate forcing-package-neutral-identity --strict --no-interactive` + 新锚 RED 证 + N1-N5 击杀证(test_direct_grid_evidence_smoke.py 出清单——C4 禁改面且 env-gated 本地恒 skip;test_object_store_forcing/test_orchestration_chain 留清单作禁改面回归)。
- 远端 receipt:**不需要**——`file_store.py:843` 保持不动后无任何 DB/display 面改动(C3 裁决),SHUD 作业不重触发(issue Out-of-scope 明文),oracle 即本地 pytest;此为对 CLAUDE.md oracle 路由表的显式适用判定,若评审认定需 node-27 面则升级。
- CI targeted Unit Tests 绿。
