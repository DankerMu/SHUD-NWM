# Design: node27-compression-timeout-walls

## D1 — 旋钮与解析(fixture-r1 C12 后:默认注入口径显式化)

| env 变量 | 默认 | 解析 | 落点 |
|---|---|---|---|
| `NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS` | 840000 | `_parse_positive_int(minimum=1000)`(亚秒 statement_timeout 无运维意义,拒绝) | config 字段 `compress_timeout_ms` |
| `NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS` | 900 | `_parse_positive_int(minimum=1)`;wrapper 侧同名变量 shell 守卫(D4) | config 字段 `wrapper_wall_seconds` + wrapper `WALL` |
| `NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS` | 940 | `_parse_positive_int(minimum=1)`;**声明值**(Python 无法读 systemd 实配,见 D2 第二腿) | config 字段 `systemd_wall_seconds` |

**默认注入**(区别于 LAG/PER_TICK 的必填语义,C12):`raw = env.get(name)`;`raw in (None, "")` → 取默认;否则 `_parse_positive_int(raw, ...)`。空串取默认与 shell `${VAR:-900}`(unset **或** empty 都替换)对齐——两侧对同一字符串的边界处置一致,单一事实源无漂移。解析失败 → `CompressionConfigError`(既有 fail-closed 形态,scripts/node27_timeseries_compression.py:157-171 同模式)。

## D2 — 预算链不变式(fail-closed,DB 前;fixture-r1 C4/C8/C9 后:两腿 + 余量推导)

```
_CLEANUP_MARGIN_SECONDS = 60      # 今日 900-840 余量:reconciliation + receipt + cleanup
_SYSTEMD_MARGIN_SECONDS = 40      # 今日 940-900 余量 = timeout 的 --kill-after=30s + 10s ε
                                  # (TERM 后最坏再 30s 才 KILL,systemd 墙必须 > wall + kill-after)
腿 1: ceil(compress_timeout_ms / 1000) + _CLEANUP_MARGIN_SECONDS <= wrapper_wall_seconds
腿 2: wrapper_wall_seconds + _SYSTEMD_MARGIN_SECONDS <= systemd_wall_seconds
```

- 任一腿违反 → `CompressionConfigError`,消息含全部数值与换算式;检查位于 `config_from_args` 内。"DB 前拒绝"的可观测语义在 `main()` 路径(config 之后第一个 DB 触点是 `fetch_display_watermark` 调用,:973;`main()` 对 config 阶段 `CompressionConfigError` 返回 1,:959-968)——锚点据此设计(B3,C7)。
- **腿 2 是声明值交叉校验**(issue In-scope 第三层"wrapper 墙 <= systemd 墙"的可实现形态):Python 校验的是 env 里的声明,声明与 systemd 实配(drop-in)的一致性由 runbook 强制顺序 + example 注释维护。默认三元组 (840000, 900, 940) 两腿均恰好成立。
- **不变式界定的是单 chunk 预算,不是整 tick**(C9):一个 tick 最多压 `per_tick_bound` 个 chunk 共享同一 wrapper 墙;追赶配方必须同时设 `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=1`(D6 表第 4 行),否则大 chunk 之后的 chunk 仍会在墙上被砍。
- ms→s 用 ceil(保守),防 `840001ms → 840s` 型边缘挤兑。

## D3 — hook 注入(fixture-r1 C5 后:装配点 partial,协议不动)

- `CompressChunk = Callable[[str, ChunkRow], None]`(:338)协议与调用点 `compress_chunk(config.database_url, chunk)`(:605)**均不变**——测试文件 12 处 `compress_chunk=` fake 零改动(must-preserve)。
- `_default_compress_chunk(database_url, chunk, *, compress_timeout_ms)`:keyword-only 且**无默认值**——装配点漏绑即 TypeError,不会静默回退 840000。
- 装配点(`main()` :1031 `compress_chunk or _default_compress_chunk`)改为 `compress_chunk or functools.partial(_default_compress_chunk, compress_timeout_ms=config.compress_timeout_ms)`。
- `SET statement_timeout = {compress_timeout_ms}` 插值对象是 `_parse_positive_int` 产物(int),无注入面。receipt schema 零改动;实现时 grep 复核 receipt 构造无被改常量旁路读点并留输出。

## D4 — wrapper 守卫(与既有 JSON stderr 错误形态一致)

env 文件 source(:23 `set -a` + :25 `. "$ENV_FILE"`)之后:

```sh
WALL=${NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS:-900}
case "$WALL" in
  ''|*[!0-9]*) echo '{"status":"failed","reason":"wrapper wall must be a positive integer"}' >&2; exit 1 ;;
esac
[ "$WALL" -ge 1 ] || { echo '{"status":"failed","reason":"wrapper wall must be a positive integer"}' >&2; exit 1; }
exec /usr/bin/timeout --signal=TERM --kill-after=30s "${WALL}s" "$PYTHON_BIN" "$SCRIPT" "$@"
```

要点:`*[!0-9]*` 拒绝负号/小数/空白(fail-closed,AC-3);前导零按十进制接受(`timeout` 与 `[ -ge ]` 语义一致)。空串经 `:-` 取默认(与 Python 的 D1 口径一致,C12)。`--signal=TERM --kill-after=30s` **逐字保留**——它是 D2 腿 2 余量推导的前提(C8)。

## D5 — 测试改写与新锚

**授权改写清单(4 处,fixture-r1 C1 后补第 4 处)**:

1. `tests/test_node27_timeseries_compression.py:124-125` → config 默认三元组 (840000, 900, 940) + 两腿不变式对默认值成立。
2. `:1281` `TimeoutStartSec=940` → 解析单元文件实值,断言 `== 默认 systemd_wall_seconds`(且 `== 默认 wall + _SYSTEMD_MARGIN_SECONDS`,不再双写 940)。
3. `:1283` exec 整行断言 → **保留整行等值断言**(含 `--signal=TERM --kill-after=30s`,C8),仅 `900s` 换 `"${WALL}s"`;另断言默认赋值 `:-900` 与守卫 case 块存在。
4. `tests/test_node27_wrapper_pythonpath.py:34` `_PINNED_LAUNCHER_EXEC`(被 :52 公共夹具 `count == 1` 与 :1010 断言消费)→ 同步为 `"${WALL}s"` 形态,该文件其余断言零改动。

**新锚**:

- **B1(默认不变钉)**:`_base_env` 不含新变量 → config 三字段等于今日字面量;既有 receipt 断言零改动即回归证明。
- **B2(override 端到端传播钉,C6/fixture-r2 F1)**:走 **`main(["--enforce", ...])`**——`build_receipt` 只在 enforce 分支调 compress hook(:602-607),dry-run 永远够不到 `_default_compress_chunk`;`monkeypatch.setenv`(compress=1800000, wall=1900, systemd=1940)+ `monkeypatch.setitem(sys.modules, "psycopg2", fake)`,**不注入** `compress_chunk`,并 patch `fetch_display_watermark`(其自带 psycopg2.connect,packages/common/display_watermark.py:44-47,须以 monkeypatch.setattr 或 `now_utc` seam 绕开)、注入 `fetch_chunks`/`measure_chunk_bytes`/reconcile、patch `_current_head_sha`;断言 fake cursor 捕获到 `SET statement_timeout = 1800000`。杀"装配点漏传 config"突变。既有 :312-331 范式是 dry-run + 注入 fake hook,**不可照抄**(正是 C6 否决的弱形态)。
- **B8(wrapper 行为钉,fixture-r2 建议采纳,r3 Note 精化)**:复用 `tests/test_node27_wrapper_pythonpath.py` 的 stub-launcher 捕获范式——**必须走 `_relaunched_wrapper`(:48-55,唯一无条件替换 launcher 的入口),不得用 `_wrapper_under_test`(:58-75,Linux 上直接返回生产 wrapper 不替换)**。以 `NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=1900` 执行,断言捕获序列中 `timeout` 的 DURATION 操作数为 `1900s`(按 `_CAPTURE_SCRIPT` :17-28 的行序是第 4 个 token:`[PYTHONPATH, --signal=TERM, --kill-after=30s, 1900s, ...]`,非首 token);非法值(`abc`)断言 stub 零执行且 wrapper exit 非 0。杀"守卫与 exec 之间 WALL 被重赋值"类文本锚够不到的突变;也是 wall 传播的确定性证据(receipt 阶段 A 不再依赖 racy 的 `ps` 抓取,D7)。
- **B3(不变式违反钉,走 main(),C7)**:腿 1 违反(900000/900/940)与腿 2 违反(840000/900/920)各一形状 + ceil 边缘(840001/900/940 拒)+ 恰好相等(840000/900/940 过);sys.modules 注入 fake psycopg2 记录 connect 调用,断言 main 返回非 0 且 **connect 调用数 == 0**。
- **B4(解析 fail-closed 钉)**:compress:0/-1/abc/999(低于 minimum=1000);wall:0/abc;systemd:abc;空串 → 取默认(不报错,C12 口径)。
- **B5/B6/B7**:即授权改写 2/3 + example 文本锚(两变量名 + drop-in 换算注释;第三变量 SYSTEMD 同锚)。

**突变击杀**:N1 删腿 1 → B3 死;N2 腿 1 `<=` 改 `<` → B3 恰等形状死;N3 装配点不传 config(partial 去掉)→ B2 死;N4 wrapper 守卫 case 删除 → 授权改写 3 的文本锚死;N5 ceil 改截断 → B3 的 840001 形状死;N6 删腿 2 → B3 腿 2 形状死。

## D6 — runbook(fixture-r1 C2 后:锚点重定位;C4/C9/C10 后:强制顺序)

- **锚点实况**(HEAD 复核):手动 `compress_chunk` 禁令在 `docs/runbooks/tier-node27-timeseries-storage.md:1261`(gated first-enforce step 9 协议内),issue 引的 :824 实为 product-archive `.archive-guards` 内容;"14 min ... 900 s ... 940 s walls" 句全文不存在(issue :1753-1754 证据 stale)。:1310/:1507-1510/:1666 的 900-second 表述属 supervisor/replay lane(`--wall-seconds 900`),是 Non-Goal,**不改**——新小节须明示 runner wall 与 supervisor wall 是两个东西。
- 新增"大 chunk 追赶"小节,**四行配方 + 强制顺序**:
  1. 先装 systemd drop-in(`TimeoutStartSec = 声明 systemd wall`)+ `daemon-reload`;
  2. 追赶窗内 `systemctl stop` + `mask` 定时 timer(防定时 tick 读到 override env 却撞未改的 systemd 墙——b21e2453 要防的形态,C4;也防 receipt 覆写,C10);**且不得触发 supervisor/replay lane 的 compression 任务**(fixture-r2 F2:supervisor 子进程走同一 wrapper,`CHILD_ENV_ALLOWLIST` 不含 `..._ENV_FILE`,replay 子进程回落到同一默认 env 文件,而 supervisor `--wall-seconds 900` HardWall 与 replay `TimeoutStartSec=920` 不随 override 调整——override 窗内跑 replay 即 TERM-mid-DDL 的另一 lane 复刻);
  3. env 设四值:`COMPRESS_TIMEOUT_MS` / `WRAPPER_WALL_SECONDS` / `SYSTEMD_WALL_SECONDS` / `PER_TICK_BOUND=1`(C9);
  4. dry-run tick 核选择集 → `--enforce` tick(独立 `--receipt-path`,锁路径共享,C10)→ 清理,**顺序硬性**(fixture-r3 C-r3-2):**先删 env override**,再 unmask timer、撤 drop-in、恢复默认 receipt 路径——若先撤 drop-in/unmask 而 override 残留,定时 tick 会以 wall=1900 通过 Python 腿 2(声明 1940)却撞回落为 940 的 systemd 实墙,正是 b21e2453 的 TERM-mid-DDL;整个追赶流程不得在 override 残留状态下结束(残留亦重臂 F2 的 replay 风险)。
  人工静默窗 `SET statement_timeout=0; SELECT compress_chunk(...)` 明示为**最后手段**。supervisor/replay lane 代码零改动(Non-Goal)——共享 env 文件耦合只以 runbook 顺序纪律覆盖,并在 PR 中登记为已知残余。
- **作用域划界**:对 **:1261**——该禁令属一次性 gated first-enforce 取证协议(取证期),新小节属事故追赶期,互不覆盖,两处互相引用。

## D7 — node-27 receipt 契约(fixture-r1 C3/C10/C13 后:两阶段,provenance 纯净)

**阶段 A(合并前,PR 分支浅 clone,零 DB 突变)**:clone 至 `~/tmp/nwm-1156-receipt`,`NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=<clone>` 走 **clone 自己的 wrapper**(不用生产 wrapper——其墙仍是旧 900s 硬编码),加 `NODE27_TIMESERIES_COMPRESSION_PYTHON=/home/nwm/NWM/.venv/bin/python`(fixture-r2 F5:clone 无 `.venv`,wrapper :34/:40 的 `PYTHON_BIN` 守卫会中止)。**该解释器变量必须在调用 shell 中 export,不得写入 env 文件**(fixture-r3 C-r3-1:wrapper :5-7 在 source 之前快照 `CALLER_PYTHON_OVERRIDE`,env 文件中的值被静默忽略、回落 clone `.venv` 复刻 F5 失败;该语义已被 tests/test_node27_wrapper_pythonpath.py:614-617 `env-file-python-must-not-win` 钉死。`REPO_ROOT` 相反在 source 之后读,env 文件可载)。解释器 override provenance-safe:`scripts` 命名空间由 :61-88 的 import-origin 检查以显式 `PathFinder.find_spec(name, path)` 绑定 clone;`packages` 的 clone 绑定来自 sys.path 顺序——`PYTHONPATH=$REPO_ROOT` 先于生产 `.venv` editable finder(其以 `meta_path.append` 追加在 `PathFinder` 之后),故 clone 胜出(r3 Note 修正后的理由;receipt 的 provenance 断言仍须双打印 `scripts`+`packages` 模块 `__file__` 实证)。**禁用的只有 `NODE27_TIMESERIES_COMPRESSION_SCRIPT` override**(C3 的混合 provenance 否决收窄到此)。clone 内自建 0600 env 文件(独立 `--receipt-path`/锁路径共享):
1. override env + **dry-run tick**:选择集 + env 文件回显(凭据遮蔽);wall 传播的确定性证据由 B8 单元锚承担,receipt 不做 racy 的 `ps` 抓取;
2. 不变式违反 env → wrapper/runner fail-closed 实录;
3. provenance:`__file__`/`$0` 指向 clone + `sys.version`;
4. 清理 clone 与临时 env。

**阶段 B(合并后,`/home/nwm/NWM` 生产树 pull)**:按 D6 强制顺序跑一次 override `--enforce` tick(独立 receipt 路径)+ 删 override 默认 tick,两份 receipt 对比;若届时无待压大 chunk(2026-07 积压已清的正常态),"大 chunk 压缩成功/exit 124 不再发生"以阶段 A 的 wall 传播实证 + 阶段 B 正常 tick 替代,记 PR 偏离——不制造 333 GB chunk、不人为 decompress(Non-Goal)。阶段 B 结果以 PR 评论补录(合并前 PR 挂阶段 A receipt + 阶段 B 计划)。

**通用**:receipt 不得含 DATABASE_URL 凭据;生产 env 文件如被触碰,前后各做 `stat -c '%a %U:%G'` 复核(0600 + nwm)写入 receipt(C13);override 用后必删并留恢复步骤。

## D8 — 旁支字面量登记(fixture-r1 C11,fixture-r2 F3 后裁定翻转)

live-evidence 模块是**归档证据验证器**:其期望值是冻结的历史契约(`EXPECTED_LAG_SECONDS`/`EXPECTED_BOUND` 同族),从可变默认派生会让共享默认一变、历史 bundle 验证静默漂移——r1 的"派生杀漂移"方向恰好反了。登记两处、**均保留字面量 + 注释**:

- `scripts/node27_timeseries_compression_live_evidence.py:354-358` `_TIMEOUT_PREFIX`(`900s`,期望 `launcher_argv`;脚本内 call-site-dead,消费者是 tests/test_node27_timeseries_compression_live_evidence.py:263);
- `:65` `EXPECTED_TIMEOUT_SECONDS = 900`(**live 常量**,`verify_bundle` 于 :2617 benchmark、:2933/:2937 capture wall、:3297 authorization 消费)。

注释口径:"frozen archival-evidence contract; documents the wall at capture time, deliberately NOT derived from the runner default"。若本变更触碰该文件(哪怕仅注释),Evidence Floor 补跑 `uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py`。
