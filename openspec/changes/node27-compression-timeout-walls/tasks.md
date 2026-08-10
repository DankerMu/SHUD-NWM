# Tasks: node27-compression-timeout-walls

Fixture level: **expanded**(6+ artifact 面:py + sh + systemd + env 模板 + 两个测试文件 + runbook;issue 无 Suggested fixture level,按多 artifact 运维面取 expanded,无分歧)。
风险轴:默认行为保真(AC-1)、fail-closed 配置链(解析/两腿不变式/shell 守卫)、跨 artifact 一致性(三层墙单一事实源 + 声明值交叉校验)、文档作用域冲突(:1261 first-enforce 禁令)、运维时序(timer 污染/receipt 覆写)。
Seams under test:`config_from_args`(解析+不变式)、`main()` 的 DB 前拒绝与 hook 装配、wrapper WALL 读取与守卫、单元文件与默认值关系。
Must-preserve:receipt schema、`CompressChunk` 2 参协议与 12 处既有 fake、既有全部测试断言(**4 处授权改写除外**,design D5)、选择算法、`_QUERY_TIMEOUT_MS`、replay/supervisor lane(含 runbook :1310/:1507-1510/:1666 的 900-second 表述)、`--enforce`/dry-run 语义。

## 1. 实现

- [x] 1.1 `scripts/node27_timeseries_compression.py`:三常量/字段(D1,含 systemd 声明值)、默认注入口径(`raw in (None,"")` → 默认)、`_CLEANUP_MARGIN_SECONDS=60` + `_SYSTEMD_MARGIN_SECONDS=40` 命名常量、两腿不变式(D2,ceil,`CompressionConfigError`,位于 config_from_args)、`_default_compress_chunk` keyword-only 无默认注入 + `main()` 装配点 `functools.partial`(D3;**`functools` 现未导入,:20-33,需补 import**);模块头预算链注释(:63-69)同步为"默认值 + env 可配 + 两腿"口径。
- [x] 1.2 `scripts/node27_timeseries_compression_once.sh`:WALL 读取 + 守卫 + exec 行(D4 逐字范式,`--signal=TERM --kill-after=30s` 保留)。
- [x] 1.3 `infra/env/node27-timeseries-compression.example`:三变量 + 注释(默认值、两腿不变式换算、systemd drop-in 与声明值同步义务、追赶时 `PER_TICK_BOUND=1` 提示)。
- [x] 1.4 receipt 面复核 grep:确认 receipt 构造不含被改常量旁路读点(有则同步并留输出;schema 零改动);live-evidence 两处字面量(`_TIMEOUT_PREFIX` :354-358、`EXPECTED_TIMEOUT_SECONDS` :65)按 D8 **保留字面量 + frozen-contract 注释**,若触碰该文件则 Evidence Floor 补跑其测试文件。
- [x] 1.5 `infra/systemd/nhms-node27-timeseries-compression.service` 默认值不变;仅当注释与新口径冲突时改注释。

## 2. 测试锚点(design D5;4 处授权改写 + 新锚)

- [x] 2.1 B1 默认不变钉(无新 env → 三字段 == 今日字面量)。
- [x] 2.2 B2 override 端到端传播钉(走 **`main(["--enforce", ...])`**——dry-run 够不到 compress hook(:602-607);不注入 compress_chunk,patch `fetch_display_watermark`(自带 connect)+ 注入 fetch_chunks/measure/reconcile,fake psycopg2 捕获 `SET statement_timeout = 1800000`;design D5 B2,r2 F1)。
- [x] 2.2b B8 wrapper 行为钉(stub-launcher 捕获范式:wall=1900 → argv 首位 `1900s`;`abc` → stub 零执行 + wrapper exit 非 0;design D5 B8)。
- [x] 2.3 B3 不变式违反钉(走 `main()`:腿 1 违反 900000/900/940、腿 2 违反 840000/900/920、ceil 边缘 840001/900/940 拒、恰等 840000/900/940 过;fake connect 调用数 == 0 且 main 返回非 0)。
- [x] 2.4 B4 解析 fail-closed 钉(compress:0/-1/abc/999;wall:0/abc;systemd:abc;空串→默认)。
- [x] 2.5 授权改写 1:`test_node27_timeseries_compression.py:124-125` → 默认三元组 + 两腿不变式断言。
- [x] 2.6 授权改写 2:`:1281` → 单元文件实值 == 默认 systemd 声明值 == 默认 wall + 40。
- [x] 2.7 授权改写 3:`:1283` → **整行等值**断言(仅 `900s`→`"${WALL}s"`)+ `:-900` 默认赋值 + 守卫 case 块文本锚。
- [x] 2.8 授权改写 4(fixture-r1 C1):`tests/test_node27_wrapper_pythonpath.py:33-34` `_PINNED_LAUNCHER_EXEC` 同步 `"${WALL}s"` 形态——**该常量是 f-string,须写 `${{WALL}}s` 转义**(r2 cosmetics);该文件其余断言零改动,全文件跑绿。
- [x] 2.9 B7 env 模板钉(三变量名 + drop-in 注释文本锚)。
- [x] 2.10 双文件全绿:`uv run pytest -q tests/test_node27_timeseries_compression.py tests/test_node27_wrapper_pythonpath.py`;既有断言(4 处授权改写外)零削弱零删除。

## 3. 突变击杀证(shasum 还原校验)

- [x] N1 删腿 1 检查 → B3 死。
- [x] N2 腿 1 `<=` 改 `<` → B3 恰等形状(840000/900/940 必须通过)死。
- [x] N3 装配点不传 config(partial 去掉,函数若有默认则静默回退)→ B2 死。
- [x] N4 wrapper 守卫 case 块删除 → 2.7 文本锚死。
- [x] N5 ceil 改整除截断 → B3 的 840001 形状死。
- [x] N6 删腿 2 检查 → B3 腿 2 形状(840000/900/920 必须拒)死。

## 4. 规格

- [x] 4.1 `specs/hypertable-compression/spec.md` delta:ADDED Requirement(两腿预算链 + 声明值口径 + wrapper 守卫 + 默认不变),scenario 覆盖 AC 集;`openspec validate node27-compression-timeout-walls --strict --no-interactive` 通过。

## 5. 文档

- [x] 5.1 runbook 新增"大 chunk 追赶"小节(D6:四行配方 + 强制顺序[drop-in 先行、stop+mask timer、**override 窗内禁 supervisor/replay compression 任务**(r2 F2)、独立 receipt 路径、override 删除为硬性最后一步] + 与 **:1261** first-enforce 禁令作用域划界 + runner wall vs supervisor `--wall-seconds` 区分);**不改** :1310/:1507-1510/:1666 与 supervisor/replay 代码(Non-Goal);issue 所引 :824/:1753-1754 锚点已核实不存在/错位,不产生对应编辑(fixture-r1 C2);共享 env 文件耦合在 PR 中登记为已知残余。

## Evidence Floor

- 本地:`uv run pytest -q tests/test_node27_timeseries_compression.py tests/test_node27_wrapper_pythonpath.py` 全绿(若触碰 live-evidence 文件则加 `tests/test_node27_timeseries_compression_live_evidence.py`);`uv run ruff check .`;openspec validate;N1-N6 击杀证。
- **node-27 receipt(AC-8,D7 两阶段契约)**:阶段 A(合并前,clone 自己的 wrapper + clone 内 0600 env,dry-run 传播实证 + fail-closed 实录 + provenance,零 DB 突变);阶段 B(合并后生产树,D6 顺序下 override `--enforce` tick + 默认恢复 tick,PR 评论补录);无大 chunk 时按 D7 替代形态记偏离;receipt 无凭据;生产 env 触碰前后 `stat` 复核。
- CI targeted Unit Tests 绿。

## Non-Goals(复述 proposal)

不改选择算法 / `_QUERY_TIMEOUT_MS` / 自动 decompress 或重试 / receipt schema / retention lane / replay+supervisor lane 墙与其文档表述。
