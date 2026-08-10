# Tasks: forcing-package-neutral-identity

Fixture level: **expanded**(profile 触发词命中 + 三 worker 跨面;issue 无 Suggested fixture level,无分歧)。
Must-preserve:staged 目的地 `{project}.tsd.forc` 命名、站点 CSV 名从 tsd 内容派生、`.sp.att` FORC 映射、非 direct-grid legacy internal-forcing 回退现状(runtime.py:947-963)、既有错误码语义(`DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING`/`DIRECT_GRID_TSD_FORC_TOO_LARGE`;`FORCING_CHECKSUM_INVALID` 保持覆盖同名 duplicate 含站点 CSV,r1-C5;`FORCING_CHECKSUM_MISSING` 语义与触发位置 :1845-1849 不动,r1-C2)、manifest 门空命中返回 `[]` 现状(r1-C2)、`producer_version = "m2.1"` 不升版(r1-C10,design D6)、QHH 真资产面(bootstrap/runbook :126 真资产行/`data/Basins/**` 零改动)、`file_store.py:843` 与 QHH bootstrap 的站点 provenance `source` 值(r1-C3,登记残余)、legacy 录制 fixture 原样、`shud_forcing`/`shud_forcing_csv` role 名、suffix-glob 消费者(`runtime.py:550` 等)。

## 1. 实现

- [x] 1.1 新增 `packages/common/shud_forcing_contract.py`(D2 常量集,零依赖)。
- [x] 1.2 `workers/forcing_producer/producer.py`:写点 :2609、role 判定 :1986、保留名集 :2768 改引常量(D3);:1910 注释同步(r1-C8 锚点)。
- [x] 1.3 `scripts/create_qhh_shud_manifest.py`::319-324 成员判定迁移双名恰一(r1-C1——producer 消费链 `run_qhh_backend_smoke.sh:158-169`/`run_qhh_cycle.sh:499,:509` 依赖它;canonical 或 legacy 恰一接受、双存拒绝、缺失报错双名);:132 `station_source` 改记实际命中成员 basename(r2-note);:347 后取 `member_name = PurePosixPath(member).name`,:352/:355/:359/:362 四条 header 失败消息插值 `member_name`(round-1 V3-1 解冻——其中 :359/:362 无 URI 兜底;`test_qhh_scripts_static.py` 的 `match="station header"` 子串断言零削弱)。
- [x] 1.4 `workers/shud_runtime/runtime.py`:D4 三层恰一决断——manifest 门(:1795-1830,并存 → 新码 `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`;空命中保持 `[]`,同名 duplicate 保持 `FORCING_CHECKSUM_INVALID`)、staging 探测(:974,双文件裁决)、限长门(:1953/:3748 双名等价);:944 报错文本双名;:55/:1862 注释同步(r2-note);实现前 grep 确认错误码无闭集约束(有则登记 PR 偏离记录)。
- [x] 1.5 `workers/mapping_builder/binding.py:270-272`:`RESERVED_FORCING_FILENAMES` 改由常量并集构造(canonical + legacy 都保留);:266-269 锚注释同步 spec delta;:31 注释同步;`rewrite.py:702` 注释同步(r2-note);grep evidence/receipt 面确认无载荷记录 `reserved_suffix`/`reserved_exact_match` reason 字符串(r2 残余风险,有则登记偏离)。
- [x] 1.6 豁免面零改动核验:`git diff --stat` 不含 `qhh_production_bootstrap.py`/`run_qhh_backend_smoke.sh`/`seed_qhh_forcing_stations.py`/`file_store.py`/`data/Basins/**`;`docs/runbooks/qhh-backend-smoke.md` 按出现粒度(r2-C14):仅 :129 与 :205 第二处(package 成员散文)变更,:126/:130/:202/:205 第一处(真资产与 staged 目的地散文)不动(r1-C7/r2-C14)。

## 2. 测试锚点(design D7)

- [x] 2.1 B1 producer canonical 双 fixture 钉(QHH + 非 QHH,站点集来自各自输入;AC-2)。
- [x] 2.2 B2 runtime canonical 端到端 + B3 legacy 兼容(标准 helper 以 legacy 名构造,走 staging + checksum 全路径,r1-C6;AC-3)。
- [x] 2.3 B4 manifest 并存歧义 + B5 磁盘歧义 → `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`(错误文本含命中路径);B4b 同名重复 → `FORCING_CHECKSUM_INVALID` pin(r1-C5,该码此前无钉)。
- [x] 2.4 B6 缺失 fail-closed(r1-C2 修正:staging 层 `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING` 文本双名;manifest 门空命中钉住"返回 `[]` 不 raise")。
- [x] 2.5 B7 保留名钉(r1-C9 收窄:producer 双 basename 拒绝;binding 侧单元级钉常量集构成;r2-note:`test_forcing_producer.py:2267` 撞名用例**不整替**——保留为 legacy 拒绝面 + 新增 canonical 兄弟用例)+ B8 限长门 canonical 面。
- [x] 2.6 授权改写:package-contract 断言净等价迁移 canonical(test_shud_runtime[helper :101-165 参数化成员名,不 fork]/test_forcing_producer[:2267 除外,见 2.5]/test_mapping_builder_binding/**test_direct_grid_e2e.py:137**[r2-C12]);`test_qhh_scripts_static.py` 仅 C1 授权扩展(B10:双名恰一 + legacy 仍接受);**禁改**清单(D7:test_qhh_production_bootstrap、orchestration_chain 透传、录制 fixture、test_direct_grid_evidence_smoke、test_object_store_forcing、**test_forcing_domain_handoff_apply**[r2-C13,真资产 source_file provenance])零触碰。
- [x] 2.7 B9 豁免回归:`tests/test_qhh_production_bootstrap.py` 零改动全绿;`tests/test_qhh_scripts_static.py` 除 C1 扩展外零改动全绿。
- [x] 2.8 D9 全清单绿:九个测试文件 + ruff。

## 3. 突变击杀证(shasum 还原校验)

- [x] N1 并存歧义判定删除 → B4 死。
- [x] N2 写点回退 legacy → B1 死。
- [x] N3 producer 保留名集漏 canonical → B7 producer 面死(binding 侧被后缀规则遮蔽,不作击杀面,r1-C9)。
- [x] N4 runtime 成员集删 legacy → B3 死。
- [x] N5 staging 双文件裁决删除 → B5 死。

## 4. 规格

- [x] 4.1 `specs/fixed-station-forcing-production/spec.md` delta:MODIFIED "SHUD forcing package is produced"(canonical 名 + 中性身份条款)+ ADDED identity-contract requirement(恰一决断、legacy 兼容、fail-closed 两翼、filename 非 basin 证据);`specs/direct-grid-binding-artifact/spec.md` delta:MODIFIED 碰撞 scenario(双保留名)。`openspec validate forcing-package-neutral-identity --strict --no-interactive` 通过。

## 5. 文档

- [x] 5.1 `docs/modules/04_forcing_production_design.md:64` 与 `docs/spec/02_data_product_and_time_semantics.md:120`:canonical 名 + legacy 兼容与截止条件(D5)+ 明示"解决命名语义冲突,未发现跨流域数据污染"(AC-6);`docs/runbooks/qhh-backend-smoke.md` package 散文行(:129/:202/:205)同步 canonical、:126 真资产行不动(r1-C7)。

## Evidence Floor

- 本地:D9 pytest 清单(九文件)全绿 + ruff + openspec validate + 新锚 RED 批证 + N1-N5 击杀证(shasum 还原)。
- 豁免面证:B9 回归 + 1.6 diff-stat 核验输出。
- 远端 receipt:不需要(D9 判定;file_store.py:843 不动后无 DB/display 面改动,无调度改动)。
- CI targeted Unit Tests 绿。

## Non-Goals(复述 proposal)

不改插值/source grid/station binding/`.sp.att` FORC/气象值/单位/流域范围;不做污染调查;不重写历史 package;不触发 node-22 作业;不改 QHH 真资产;不引入 manifest 顶层 schema_version;不升 producer_version;不改站点 provenance `source` 标签。

## 6. Round-1 修复(A1/A2/B2/V3-1/V3-3;V3-2 DISCARD 留痕)

- [x] 6.1 A1/A2:staging 歧义 raise 收窄 direct-grid;非 DG 双成员按 manifest 锚定选择(canonical 兜底);`_prepare_shud_project_forcing` 传入 manifest 声明成员(design D4-2 修订文)。
- [x] 6.2 B2:`_direct_grid_runtime_checksum_entries` 读取前存在性探针,缺失 → `FORCING_CHECKSUM_READ_FAILED` 点名声明成员;except 本体与 station CSV 侧同型 except 不动(design D4-3 修订文)。
- [x] 6.3 V3-1:诊断脚本四条 header 消息插值 resolved member(见 1.3 修订)。
- [x] 6.4 V3-3:`docs/spec/02_data_product_and_time_semantics.md:122` 按裁决措辞改写(并存两层 fail-closed[DG];全缺仅 DG staging 层;manifest 门空命中返回空列表;非 DG 回退/manifest 锚定)。
- [x] 6.5 新锚 B11-B16 全绿 + 既有 B4/B5/B8 锚零改动继续绿;spec delta 修订(非 DG 解析场景 + mismatch 场景)validate 通过。
- [x] 6.6 V3-2 DISCARD 留痕:binding 值等断言维持设计档位,不加源码级断言(verifier 裁决:行为面被后缀规则冗余覆盖,producer 侧击杀面完好)。
