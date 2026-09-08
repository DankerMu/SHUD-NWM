# c4-live-display-readiness

拆分 #1895 的独立 C4 浏览器证据前置能力。本项只合并未部署代码；生产激活、node-27 访问与 C1–C4 live receipt 仍由 #1895 完整 readiness merge 后执行。不关闭 #1895/#1891。

## 生产禁区

- 完整 readiness 合并之前 **禁止** 访问 node-27、禁止部署、禁止把本切片本地/mock/API-only/历史 receipt 写成 live PASS。
- **不要** 更新 cold-tablespace / #1895 readiness runbook；操作合同只以本文为准。
- **不要** 设置 `VITE_AUTH_ROLE`、`VITE_ENABLE_ROLE_OVERRIDE`，也不要设置六个 river override 键（出现即 FAIL，空值也算）：`PLAYWRIGHT_LIVE_RIVER_RUN_ID`、`PLAYWRIGHT_LIVE_RIVER_MODEL_ID`、`PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID`、`PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID`、`PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME`、`PLAYWRIGHT_LIVE_RIVER_SCENARIO`。
- **不要** 使用 mock/HAR、`page.route` / `routeFromHAR`、角色伪装、river-click receipt 或历史 C4 receipt 代替当次真实运行。
- 清理只允许本次创建的临时对象；已有目标 no-clobber，故障不得覆盖旧文件。

## 五个显式环境变量

| 键 | 含义 | 缺失/非法 |
|---|---|---|
| `PLAYWRIGHT_LIVE_BASE_URL` | 前端 bare `http(s)` origin，无 userinfo/path/query/fragment | 缺/空 → BLOCKED `REQUIRED_ENV_MISSING`；非法 → FAIL `CONFIG_INVALID` |
| `PLAYWRIGHT_LIVE_API_BASE_URL` | API bare `http(s)` origin，同上 | 同上 |
| `PLAYWRIGHT_LIVE_C4_BASIN_ID` | 当前 live basin pin，`[A-Za-z0-9._:-]{1,96}` | 缺/空/非法 → FAIL `CONFIG_INVALID` |
| `PLAYWRIGHT_LIVE_C4_SEGMENT_ID` | 当前 live segment pin，同上 | 同上 |
| `PLAYWRIGHT_LIVE_C4_RECEIPT_PATH` | 当次唯一、尚不存在的绝对 receipt 路径 | 缺/空 → BLOCKED `REQUIRED_ENV_MISSING`（无文件）；词法非法 → FAIL（无文件）；父目录不安全/目标已存在 → BLOCKED（无文件） |

`pnpm` script 只透传两个 URL；basin/segment/receipt **必须**由调用方显式导出，没有默认值。

## 私有 receipt 路径

父目录必须已存在、canonical（无 symlink 分量）、euid 所有、mode **恰好 0700**。最终路径必须事先不存在。basename 必须匹配：

```
nhms-frontend-c4-live-evidence-[A-Za-z0-9._-]{1,96}.json
```

发布结果：regular file、euid、mode **0600**、`nlink=1`、O_NOFOLLOW / exclusive / link-first、fsync + readback。目标已存在、symlink、非 regular、权限/nlink 不合格一律拒绝且不覆盖。

## 真实 profile 命令

在仓库根、`set -euo pipefail` 下准备当次私有目录（不要复用共享 base）：

```bash
REPO_ROOT="$(pwd)"
RUN_ROOT=$(mktemp -d "$REPO_ROOT/.nhms-issue2123-c4-XXXXXX")
chmod 0700 "$RUN_ROOT"
test "$(stat -f '%u' "$RUN_ROOT" 2>/dev/null || stat -c '%u' "$RUN_ROOT")" = "$(id -u)"
RECEIPT="$RUN_ROOT/nhms-frontend-c4-live-evidence-$(date -u +%Y%m%dT%H%M%SZ).json"
test ! -e "$RECEIPT"
CMD_START=$(date -u +%s)

cd "$REPO_ROOT"
set +e
PLAYWRIGHT_LIVE_BASE_URL="${PLAYWRIGHT_LIVE_BASE_URL}" \
PLAYWRIGHT_LIVE_API_BASE_URL="${PLAYWRIGHT_LIVE_API_BASE_URL}" \
PLAYWRIGHT_LIVE_C4_BASIN_ID="${PLAYWRIGHT_LIVE_C4_BASIN_ID}" \
PLAYWRIGHT_LIVE_C4_SEGMENT_ID="${PLAYWRIGHT_LIVE_C4_SEGMENT_ID}" \
PLAYWRIGHT_LIVE_C4_RECEIPT_PATH="$RECEIPT" \
corepack pnpm@10.11.0 --dir "$REPO_ROOT/apps/frontend" run test:e2e:live-c4-display
CMD_EXIT=$?
set -e
CMD_END=$(date -u +%s)
```

该入口使用独立 Playwright profile（`playwright.live-c4-display.config.ts`，workers=1、retries=0、C4 globalSetup），真实 Chromium 访问 `/` 与 GFS/IFS strict `/ops`。普通 `pnpm test` / mocked regression **排除** 此 spec，也不要求浏览器二进制。

macOS `stat -f`、Linux `stat -c`；node-27 验收窗口用 Linux 形式。上式不是生产验收，只是本切片合并后的调用合同。

## 退出与 receipt 行为

| 情况 | 进程 | 文件 |
|---|---|---|
| 缺 receipt path / 父目录不安全 / 目标已存在 | 非 0；stderr 含 `BLOCKED:` | 不写或未覆盖旧文件 |
| 缺 frontend/API URL（path 已安全） | 非 0；`BLOCKED` + `REQUIRED_ENV_MISSING` | 写一份 BLOCKED receipt |
| 角色/river override、非法 URL/pin/path | 非 0；`FAIL` + `CONFIG_INVALID` | 能发布则写 FAIL receipt |
| lane required failure / 控制请求 / 超时 | 非 0；`FAIL` | 写 FAIL receipt（单次 publish） |
| 双源只读观察完整且无控制副作用 | 0 | 写 schema-1.0 PASS receipt |

没有 live 事实时不能变成 PASS。正常非必需 `ERR_ABORTED` 不单独失败；required 请求失败或任何 Slurm / 非 GET/HEAD 控制请求阻止 PASS。

## Binder 命令

Node-20 stdlib，只接受 schema-1.0 **PASS** 终态。绑定当次五输入、秒级 `CMD_START`/`CMD_END` bracket、以及 receipt 的 POSIX identity facts（euid/mode/nlink/size/mtime/dev/ino，read 期间不得变化）。交付/G0 门负责 frozen reviewed SHA；#1895 C3 publisher/binder 负责 C4 文件字节 sha256 与 reviewed-SHA 的绑定、重验和篡改拒绝。这些端到端义务必须履行，**不是**本 CLI 的额外 `--sha`/`--digest` 参数，也不能用本 CLI PASS 代替。

```bash
test -f "$RECEIPT" && test ! -L "$RECEIPT"
node "$REPO_ROOT/apps/frontend/scripts/c4-receipt-binder.mjs" \
  --receipt "$RECEIPT" \
  --frontend-origin "$PLAYWRIGHT_LIVE_BASE_URL" \
  --api-origin "$PLAYWRIGHT_LIVE_API_BASE_URL" \
  --basin-id "$PLAYWRIGHT_LIVE_C4_BASIN_ID" \
  --segment-id "$PLAYWRIGHT_LIVE_C4_SEGMENT_ID" \
  --cmd-start "$CMD_START" --cmd-end "$CMD_END"
```

成功只打印 `BINDER: PASS` 并 exit 0。拒绝时 stderr 一行 `BINDER: …` 并 exit 1；不回显路径、origin、identity 或 OS error；不改任何文件。

## `/ops` 只读例外

viewer 可达 `/ops` **当且仅当** runtime 同时报告 `service_role=display_readonly` 与 `display_readonly=true`。config 缺失、双字段冲突或加载超过 **10s** 按既有 RBAC 拒绝；晚到的有效 readonly config 可恢复。`/monitoring` 与 `/system/model-assets` 不放宽。不得出现 role selector / retry / cancel。
