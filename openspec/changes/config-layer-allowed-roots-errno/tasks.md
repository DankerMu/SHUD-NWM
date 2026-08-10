# Tasks: config-layer-allowed-roots-errno

Fixture level: **compact**(单函数判据替换,零调用点改动;issue 未标 Suggested fixture level,按 #1345 同族先例取 compact,无分歧)。
风险轴:cross-version 语义、构造期崩溃→结构化裁决契约升级、#1346 测试时序语义增强。
Seams under test:`_optional_config_path` 三车道返回值、`ProductionSchedulerConfig` 构造存活性、构造→preflight **seam 级**不变量(D3;pass 级残余由 B8 tripwire 钉 + #1348 收口)。
Must-preserve:成功车道规范化产物、ENOENT 规范化语义、db-free 臂(:929-932)行为、facade forwarder 签名、#1346 全部锚点断言强度、既有测试零删除。

## 1. 实现

- [x] 1.1 `scheduler_runtime_roots._optional_config_path` 按 design D2 重写;errno import 行补 `ENOENT`;`Path.resolve` 任何形态不得出现于该函数(D1)。
- [x] 1.2 消费方封闭性双重 grep 复核并留输出:(a) `_optional_config_path` 调用方——生产调用链仅 scheduler_config.py:945←:412(allowed_storage_roots 非 db-free),facade forwarder(scheduler_candidate_runtime.py:549)签名不变零改动;(b) **字段级** `grep -rn "allowed_storage_roots" services workers apps packages`——4 个读点逐一裁定(scheduler_preflight.py:529 剔根+blocker / scheduler_runtime_roots.py:450 #1348 残余 / :431 只取布尔无害 / scheduler_config.py:1060 db-free 车道不可达),与 design D4 表逐行对上。

## 2. 测试锚点(tests/test_production_scheduler.py)

- [x] 2.1 **B1(RED 主锚,preflight seam,生产时序)** 构造**前**已存在的自环 symlink 根 + 非 db-free 真 `ProductionSchedulerConfig`:构造成功(不抛任何异常)、`allowed_storage_roots` 含该根的词法绝对值;**preflight seam**(直接调 `_slurm_preflight(config)`):`status=="blocked"`、`blockers[0]["code"]=="SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH"`、`checks["allowed_roots"]==[]`(config 须安全远端 database_url,防 DATABASE_URL_* 占 index 0)。**命名与断言不得声称 pass 级**(pass 级残余见 B8/D4)。RED 证:py3.11 现行代码构造抛 `RuntimeError`(`pytest.raises` 读数);3.14 腿为绿钉(自环词法值 == resolve 值,红腿在 py3.11,与 issue 影响面一致)。
- [x] 2.2 **B2(errno 二分判别钉,symlink 祖先 ENOTDIR 形状)** 素材:`link -> realdir`(symlink 目录)、`realdir/file.txt` 普通文件、根 = `link/file.txt/sub`。两条车道产物**可观测区分**:词法车道 = `…/link/file.txt/sub`,非 strict realpath 车道 = `…/realdir/file.txt/sub`。断言 `config.allowed_storage_roots == (<link 词法形状>,)`——唯一判别"非 ENOENT 走词法下放"车道;端到端落 UNSAFE_PATH blocker。规范化祖先的 ENOTDIR 钉(值不变)由 #1346 A1b-e2e 既有锚继续覆盖,保持全绿。
- [x] 2.3 **B3(ENOENT 钉)** 缺失根构造:规范化纳入(产物与旧非 strict resolve 一致)、端到端无 allowed-roots blocker、两臂(db-free 用既有 env fixture 或 SimpleNamespace 对照)不回归。
- [x] 2.4 **B4(崩溃车道钉)** `<missing>/../<loop>` 形状根 + 非 db-free 构造:构造永不裸抛、配置层产物 == `Path(os.path.realpath(<该形状>))`(非 strict 折叠为环本身,D3 类 3);端到端 preflight 判 ELOOP → **落 UNSAFE_PATH blocker 车道**(不是纳入——与今日 3.13+ 行为同,非回归)。py3.11 腿为现行代码构造崩溃的红证(#1344 P1 教训第三次押注)。
- [x] 2.5 **B5(db-free 臂钉)** 同一环根 + db_free_required=True:构造成功、词法容忍产物不变(Non-Goal 锁定)。
- [x] 2.6 **B6(workaround 解除,三处)** #1346 遗留:(1) stub docstring(:29900-29906 附近)删除"separate ≤3.12 crash site"免责句(stub 本身保留——单元锚仍然合法);(2) 端到端 ELOOP 锚(:30040-30047 附近)改为**构造前造环**并删除时序注释;(3) A1b-e2e 注释(:30065-30070 附近)——"ELOOP lane is shadowed / only reachable tightening lane / materialise post-construction"三句在本变更后全假,更新为"规范化祖先下 ENOTDIR 值不变的对照锚;symlink 祖先判别锚见 B2 新锚"。**断言体零削弱、零删除**;此为本 fixture 明示授权的既有测试注释/时序修改,PR 偏离记录须列出。
- [x] 2.7 **B7(零回归)** `-k "preflight or allowed_root"` 双腿全绿(123 既有 + 新增);`tests/test_production_scheduler.py` 全文件 3.14 腿全绿。
- [x] 2.8 **B8(pass 级残余 tripwire,版本门)** `@pytest.mark.skipif(sys.version_info >= (3, 13), ...)`:构造前环根 + 非 db-free 真 config,`_scheduler_lock_evidence_root_preflight`(或 run_once 可达的最小等价面)在 ≤3.12 上 `pytest.raises(RuntimeError)`——**钉住 #1348 残余现状**。docstring 写明:#1348 落地时本钉必红,须翻转为修复后断言(结构化裁决)。3.13+ 跳过(该位点行为今日即 fail-open,归 #1348 锚)。

## 3. 突变击杀证

- [x] N1 `:505` 回退 `Path.resolve()` → B1/B4 的 py3.11 腿必死(RuntimeError)。
- [x] N2 ENOENT 回退换 `expanded.resolve()` → B4 的 py3.11 腿必死。
- [x] N3 非 ENOENT 车道改抛 typed error(模拟选项 A)→ B1 构造存活断言必死。
- [x] N4 非 ENOENT 车道改 `return None`(静默丢根)→ B1 的 `allowed_storage_roots` 含根断言必死。
- [x] N5 判据收窄为 `if ENOENT: 非strict回退 / elif ELOOP: 词法下放 / else: 非strict回退` → B2 的 `allowed_storage_roots == (<link 词法形状>,)` 断言必死(ENOTDIR 落入非 strict 车道产物变 `realdir` 形状)——symlink 祖先素材使两车道可观测区分,无对冲。

## 4. 规格

- [x] 4.1 `specs/slurm-array-runner-integration/spec.md` delta:MODIFIED `Array-capable model stages`,`unresolvable allowed storage root` scenario 共 **3 处编辑**:(1) 跨版本一致性主语收敛到 storage preflight(替换原 survive-canonicalization 限定——配置层不再中止,但 pass 级 ≤3.12 残余由 #1348 收口,故一致性只能以 preflight 为主语);(2) 新增 AND 子句(配置构造永不因不可解析 allowed root 中止,分类归 storage preflight);(3) ENOENT 子句限定 "remains merely missing after canonicalization"(D3 类 3 的 ENOENT-掩盖-环路形状最终判 unsafe,不与该句冲突)。`openspec validate config-layer-allowed-roots-errno --strict --no-interactive` 通过。

## Evidence Floor

- RED→GREEN:B1/B4 py3.11 构造崩溃红证 → 修后双腿绿;B2/B3/B5/B8 钉证;N1-N5 击杀证。
- 双腿:3.14 `uv run pytest -q tests/test_production_scheduler.py -k "preflight or allowed_root"`;py3.11 前缀 `UV_PROJECT_ENVIRONMENT=/private/tmp/claude-501/-Users-danker-Desktop-Hydro-SHUD-NWM--claude-worktrees-pr-1286-subagent-workflow-7fb9ee/03b2c0ce-847d-47b7-8b0f-8af56993ac52/scratchpad/py311 uv run --python 3.11 pytest -q ...`(严禁裸 `uv run --python 3.11`;venv 缺失时 `uv venv --python 3.11 <等价路径>` 重建)。
- **node-27 receipt(AC 硬要求,以 issue `Verification:` 字段为准)**:PR 分支临时浅 clone 至 node-27 `~/tmp/nwm-1347-receipt`(不动 `/home/nwm/NWM` ff-only 树),用 `/home/nwm/NWM/.venv/bin/python`(3.11.15):
  1. **主命令(issue Verification 指定选择器)**:`cd <clone> && PYTHONPATH=<clone> /home/nwm/NWM/.venv/bin/python -m pytest -q tests/test_production_scheduler.py -k "preflight or allowed_root"` 全绿;
  2. 补充只读探针:构造前环根 → 构造成功 + preflight `blocked` + 根因码领先;
  3. **provenance 断言**(editable 安装遮蔽核对):探针/会话首行打印 `services.orchestrator.scheduler_runtime_roots.__file__`(必须指向 clone)与 `sys.version`,verbatim 留证;
  4. 完成后清理 clone。
- `uv run ruff check .`;openspec validate;CI targeted Unit Tests(py3.11)绿。

## Non-Goals(复述 proposal)

不动 `_resolve_config_path_for_mode` / `_resolve_optional_config_path` / `*_preserve_final` 家族 / `_scheduler_allowed_roots`(#1348)/ preflight 契约(#1346 定稿)。
