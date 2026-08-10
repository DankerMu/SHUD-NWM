# Proposal: forcing-package-neutral-identity

## Why

direct-grid SHUD forcing package 的主站点索引成员对**所有流域**统一发布为 `shud/qhh.tsd.forc`——QHH 是一个具体流域的身份,却被沿用成通用运输名(issue #1176)。node-27 只读核验证实 18 个运行流域各自 SHA-256 不同、站点 bbox 各归其域,**无跨流域数据污染**;问题纯粹是文件名携带错误业务身份,削弱 provenance/可观测性/人工审计。结构性根因:该字面量存在 **10+ 处独立手抄副本**(producer 写点 :2609、role 判定 :1986、保留名集 :2768、file_store 元数据 :843、runtime 七锚——六字面点 :974/:1795/:1800/:1830/:1953/:3748 加 :944 报错文本、mapping_builder 保留名集 binding.py:270、两份 live spec 文本),producer 与 runtime 之间**没有任何共享事实源**——只靠字符串手抄一致,天然漂移风险。

## What Changes

按 issue 推荐方案(contract 先行,producer/runtime 同步迁移;备选"保留 qhh 名 + 仅文档声明"因无法消除文件系统与排障侧的误导语义被 issue 自身否决):

- **共享契约常量模块**(design D2):新增 `packages/common/shud_forcing_contract.py`,零依赖纯常量——canonical 成员 `shud/stations.tsd.forc`、legacy 成员 `shud/qhh.tsd.forc`、成员集合与 basename 集合、role 名 `shud_forcing`。producer/runtime/mapping_builder/QHH 诊断脚本全部消费此单一事实源,消除手抄漂移。
- **canonical identity**(design D1):固定流域中性字面名 `shud/stations.tsd.forc`(内容即"主站点索引";issue In-Scope 明确 filename 不承载任一 basin 身份,故不采用 `{basin_slug}` 参数化名)。
- **producer**:只写 canonical(:2609);role 判定与保留名集改引常量。新 package 一律 canonical,**不再产出 legacy 名**。`file_store.py:843` 的 `source` 元数据默认值**不改**(fixture-r1 C3:该值经 station_inventory → `met.met_station.properties_json` → station API 直达 DB/display 面,且与豁免的 QHH bootstrap 写点 `qhh_production_bootstrap.py:1441/:1452` 同列——改它即引入双写者分裂并触发 node-27 receipt 义务;它是站点 provenance 标签而非 package 成员身份,超出 issue In-Scope,登记为已知残余)。
- **runtime 双名接受、恰一决断、fail-closed**(design D4):staging 探测(:974)、manifest 必需成员/校验和过滤(:1795-1830)、限长读门(:1953/:3748)全部改为"成员 ∈ {canonical, legacy} 且**恰好一个**":零个 → 现有 missing fail-closed 保留;两个(manifest 双条目或磁盘双文件)→ 新 fail-closed 错误 `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`。checksum/identity 绑定沿用现有逐成员校验。staged 目的地命名(`{project}.tsd.forc`,runtime 现状)零改动——package 成员名纯运输身份。
- **legacy 兼容策略**(design D5):历史 object-store package 不重写,runtime 无限期只读接受 legacy,直至 object store 中不再存在缺 canonical 成员的 direct-grid package(核验查询写入 runbook 层文档);producer 侧 legacy 立即停产。
- **QHH 真资产豁免与一个必要例外**:`data/Basins/qhh/input/qhh/qhh.tsd.forc`、`qhh_production_bootstrap.py`、`run_qhh_backend_smoke.sh`(仅 :140 prose 日志)保留 QHH 名——它们指真实 QHH 模型资产。**例外(fixture-r1 C1)**:`scripts/create_qhh_shud_manifest.py` 是 producer 新鲜 `forcing_package.json` 的**消费者**(:319-324 硬性要求 legacy 成员名,否则 RuntimeError),`run_qhh_backend_smoke.sh:158-169` 与 `run_qhh_cycle.sh:499/:509` 在同一 cycle 内先 produce 后调它——producer 停产 legacy 后该链硬断。故该脚本的成员判定**必须迁移**为双名恰一(与 runtime 同契约),不能豁免。
- **spec deltas**:`fixed-station-forcing-production`(scenario :42 的 `qhh.tsd.forc` 通用契约文本 → canonical + legacy 兼容条款)与 `direct-grid-binding-artifact`(:40 保留名碰撞列表纳入 canonical)两处 MODIFIED + 一条 ADDED identity-contract requirement。
- **测试**:package-contract 断言迁移 canonical(runtime 35 处、producer 9 处、binding 3 处、e2e 1 处 :137——r2-C12 补授权,producer 真实产物读点);**禁改**:test_direct_grid_evidence_smoke.py(node-22 手工装配证据树,r1-C4)、test_object_store_forcing.py(站点 property,r1-C6)、test_forcing_domain_handoff_apply.py(两处均为真资产 `source_file` provenance,r2-C13);legacy 兼容 lane 由**显式 runtime 级 legacy package 测试**承担(标准 helper 参数化成员名后以 legacy 名构造,C6/r2-note——录制 fixture 不经过成员解析,其通过不构成兼容断言,仅保持原样不迁移);新增 QHH + 非 QHH 双 deterministic fixture 证站点集来自各自输入(AC-2);runtime 五类 package 覆盖(canonical/legacy/歧义/缺失/**identity-mismatch**——manifest 声明与对象树相异,round-1 B2 补类,AC-3);非 DG 双成员 manifest 锚定解析(round-1 A1)。
- **文档**:`docs/modules/04_forcing_production_design.md:64`、`docs/spec/02_data_product_and_time_semantics.md:120` 更新 canonical 名 + 明示"本迁移解决命名语义,未发现跨流域数据污染"(AC-6);`docs/runbooks/qhh-backend-smoke.md` **记录不改写、加日期注记**(round-2 S4 **推翻** r1-C7/r2-C14 的改写预批):该 runbook 是带日期的冻结复测证据记录(#214 冻结条款),`:129` 与 `:205` 第二处记录的是**当时**package 成员的事实观察,改写即伪造证据——两行**回退为 9af36a16 原文 byte-identical**,在 `:130` 后新增日期注记(2026-08-10,#1176)说明迁移后 producer 只产 canonical、legacy 仅历史只读兼容、当前契约见 `docs/modules/04_forcing_production_design.md`;`:202`/`:126`/`:205` 第一处照旧不动。

## Impact

- 代码:`packages/common/shud_forcing_contract.py`(新)、`workers/forcing_producer/producer.py`(:1986/:2609/:2768 + :1910 注释)、`workers/shud_runtime/runtime.py`(六字面点 + :944 报错文本 + :55/:1862 注释)、`workers/mapping_builder/binding.py`(:270 保留名集改引常量 + :31 注释;`rewrite.py:702` 注释)、`scripts/create_qhh_shud_manifest.py`(:319-324 双名恰一 + :132 station_source 记实际命中成员,r2-note)。**不含** `file_store.py`(r1-C3 裁决,r2-C15 清单残留修正)。
- 规格:`openspec/specs/fixed-station-forcing-production/spec.md`、`openspec/specs/direct-grid-binding-artifact/spec.md`(deltas 见本 change specs/)。
- 测试(与 design D9 清单一致):迁移面 `tests/test_shud_runtime.py`、`tests/test_forcing_producer.py`、`tests/test_mapping_builder_binding.py`、`tests/test_direct_grid_e2e.py`(:137,r2-C12)+ C1 授权扩展 `tests/test_qhh_scripts_static.py`;禁改回归面 `tests/test_qhh_production_bootstrap.py`、`tests/test_forcing_domain_handoff_apply.py`(r2-C13)、`tests/test_object_store_forcing.py`、`tests/test_orchestration_chain.py`(test_direct_grid_evidence_smoke.py 禁改且 env-gated,不入运行清单)。
- 文档:上列两份设计/规格文档。
- 不需要 node-27/node-22 receipt:无 DB/display/调度行为改动,SHUD 作业不重触发(issue Out-of-scope);oracle 为本地 pytest。
- **已知残余(登记不修)**:`tests/test_orchestration_chain.py` 的 `shud_station: "qhh.tsd.forc"` 是 resource_profile 透传测试数据(orchestrator 仅中继,非契约字面量),保持不动;`file_store.py:843` 与 `qhh_production_bootstrap.py:1441/:1452` 的站点 provenance `source` 标签保留 legacy 值(C3,DB/display 可见面,超 In-Scope);`tests/test_direct_grid_evidence_smoke.py` 的 node-22 证据树 legacy 名(C4,env-gated,重装配属 Out-of-scope);pre-existing 泛 package 文档漂移 `docs/ForcingReplace/CMFD 建模资产向 IFSGFS Direct-Grid 的安全迁移.md:141/428/488/842` 与 `docs/forcing数据处理流程与rSHUD一致性说明.md:51/163/309`(C11,历史迁移叙事文档,非本 change 两份权威设计文档,按 DOC_STATUS 层级登记);归档 openspec change 中的历史字面量不改写。
- **producer_version 不升版(有意,fixture-r1 C10)**:`producer.py:427` `m2.1` 保持——升版会使全部历史 package 被判过期重算,违反"不重写历史 package"Non-Goal;代价是同一 producer_version 内 package 布局二态(canonical/legacy),由成员名自描述,登记于 design D6。

## Non-Goals(issue 边界复述)

- 不改插值、source grid、station binding、`.sp.att` FORC 映射、气象值、单位、流域范围。
- 不做跨流域数据污染调查(证据不支持该结论)。
- 不重写既有 object-store 历史 package,不重触发 node-22 SHUD/Slurm 作业。
- 不改 QHH 真资产身份与 bootstrap 语义。
- 不给 `forcing_package.json` 引入顶层 schema_version(considered-and-rejected,见 design D6):恰一成员规则自描述,整份 manifest 版本化是更大契约变更,YAGNI。
