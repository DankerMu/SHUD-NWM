# Proposal: node27-compression-timeout-walls

## Why

node-27 压缩 runner 的三层超时预算全部硬编码:`_COMPRESS_TIMEOUT_MS = 840_000`(scripts/node27_timeseries_compression.py:71,注释 :63-69 声明预算链)、wrapper `timeout ... 900s`(scripts/node27_timeseries_compression_once.sh:90)、systemd `TimeoutStartSec=940`(infra/systemd/nhms-node27-timeseries-compression.service:11)。任何单 chunk 压缩超过 14 分钟在自动化 lane 结构性不可能成功(2026-07-25/26 事故:333 GB chunk 实测约 20 分钟,两次失败——wrapper exit 124 与 `QueryCanceled`——最终人工 `statement_timeout=0` DDL 收场),且最老优先选择(:280 `ORDER BY range_end ASC` + :506 `eligible[:per_tick_bound]`)使该 chunk 每 tick 重选、烧掉整轮预算,head-of-line blocking(issue #1156)。

## What Changes

按 issue 推荐方案(env 可配,复用既有 `_parse_positive_int` 模式;备选的一次性 mutation 子命令因引入第二条授权/锁/receipt 语义被 issue 自身否决):

- **Python runner**:`_COMPRESS_TIMEOUT_MS` 从模块常量降为 config 字段 `compress_timeout_ms`(`NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS`,默认 840000);新增 `wrapper_wall_seconds`(`..._WRAPPER_WALL_SECONDS`,默认 900)与 systemd 墙**声明值** `systemd_wall_seconds`(`..._SYSTEMD_WALL_SECONDS`,默认 940;Python 无法读 systemd 实配,声明值与 drop-in 的同步义务由 runbook/example 承担)。`_default_compress_chunk` 以 keyword-only 无默认参数注入,装配点 `functools.partial`,`CompressChunk` 2 参协议与 12 处既有 fake 零改动(design D3)。
- **fail-closed 两腿预算链不变式**(design D2):腿 1 `ceil(compress_timeout_ms/1000) + 60 <= wrapper_wall_seconds`;腿 2 `wrapper_wall_seconds + 40 <= systemd_wall_seconds`(40 = kill-after 30s + 10s ε,issue In-scope 的"<= systemd 墙"第三层以声明值交叉校验落地)。违反即 `CompressionConfigError`,在任何 DB 连接之前;不变式界定单 chunk 预算,追赶配方须同设 `PER_TICK_BOUND=1`。
- **wrapper**:`exec timeout` 墙改读同一 env 文件的 `..._WRAPPER_WALL_SECONDS`(默认 900);shell 守卫对非正整数 fail-closed;`--signal=TERM --kill-after=30s` 逐字保留(腿 2 余量前提)。
- **systemd**:仓库单元文件保持 `TimeoutStartSec=940`;override 场景由 runbook drop-in 表 + 强制顺序文档化(drop-in 先行、追赶窗 stop+mask timer——防定时 tick 读 override env 撞旧 systemd 墙,重现 b21e2453 要防的形态)。
- **测试**:4 处钉死字面量授权改写(tests/test_node27_timeseries_compression.py:124-125、:1281、:1283 + tests/test_node27_wrapper_pythonpath.py:34 的 `_PINNED_LAUNCHER_EXEC`);新增覆盖/不变式/守卫锚(tasks B 系列,B2/B3 走 `main()` 端到端)。
- **env 模板**:`infra/env/node27-timeseries-compression.example` 增三个变量及 drop-in 换算/同步注释。
- **runbook**:`docs/runbooks/tier-node27-timeseries-storage.md` 增"大 chunk 追赶"小节(四行配方 + 强制顺序 + 独立 receipt 路径;静默窗人工 `statement_timeout=0` 降级为兜底),并显式与 **:1261** 的 first-enforce 手动 `compress_chunk` 禁令划清作用域(issue 所引 :824/:1753-1754 锚点经 HEAD 复核不存在/错位,不产生对应编辑;:1310/:1507-1510/:1666 的 900-second 表述属 supervisor/replay lane,Non-Goal 不改)。

## Impact

- 代码:`scripts/node27_timeseries_compression.py`(三字段、解析、两腿不变式、hook partial 注入)、`scripts/node27_timeseries_compression_once.sh`(WALL 读取+守卫+exec 行)、`scripts/node27_timeseries_compression_live_evidence.py:354-358`(`_TIMEOUT_PREFIX` 第 5 处 900s 字面量,call-site-dead,按 design D8 处置)。
- 配置:`infra/env/node27-timeseries-compression.example`;`infra/systemd/nhms-node27-timeseries-compression.service` 默认值不变(仅当注释需要时微调)。
- 测试:`tests/test_node27_timeseries_compression.py`(3 处改写 + 新锚)、`tests/test_node27_wrapper_pythonpath.py`(:34 `_PINNED_LAUNCHER_EXEC` 第 4 处授权改写)。
- 文档:runbook(新小节 + :1261 划界;详见 What Changes 末条)。
- 远端:node-27 实机 receipt 两阶段(design D7:阶段 A 合并前 clone 自身 wrapper + 生产解释器 override(`..._PYTHON`,provenance-safe)dry-run 零突变;阶段 B 合并后生产树 enforce tick,PR 评论补录;无大 chunk 时以传播实证替代并记偏离——不制造 333 GB chunk)。
- **已知残余(r2 F2,登记不修)**:supervisor/replay lane 的 compression 子任务走同一 wrapper 且回落同一默认 env 文件(`CHILD_ENV_ALLOWLIST` 不含 `..._ENV_FILE`),override 窗内触发 replay 会撞未调整的 supervisor HardWall/`TimeoutStartSec=920`——以 runbook 强制顺序(override 窗内禁 replay + override 删除为硬性最后一步)覆盖,supervisor 代码零改动(Non-Goal)。
- **已知残余(round-1 V2-2,登记不修)**:`openspec/changes/tier-node27-timeseries-storage/design.md:974-975`(他变更的 Round-2 冻结审结记录)仍写字面 `900s` wrapper——按 DOC_STATUS rung 1 以合入代码为准,该行属 audit-only 冻结上下文,本 PR 不改写他变更设计文档;在此登记分歧(DOC_STATUS :107-108 要求记录而非静默)。
- **已知残余(round-1 V3-1,DEFER 路由跟踪 issue)**:三个预算值可 env 覆盖后 receipt 不再记录预算(schema `additionalProperties:false`,issue Out-of-scope 明禁改 schema)——审计归因缺口路由至跟进 issue #1351(additive optional budget 对象 + schema 分支 + 消费者容差)。

## Non-Goals(issue 边界复述)

- 不改选择算法(size-aware 排序/大 chunk 降优先级另开 issue)。
- 不碰 `_QUERY_TIMEOUT_MS`(catalog-only,60s 宽裕)。
- 不新增自动 decompress / 自动重试(ADR 0002 decision 3)。
- 不改 receipt schema、不改 retention lane。
- replay lane 的同形态墙(nhms-node27-timeseries-compression-replay.service:14 `TimeoutStartSec=920`)不改,仅登记同 pattern。
