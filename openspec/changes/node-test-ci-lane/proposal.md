# node-test-ci-lane

## Why

`scripts/node27_display_v2_browser_evidence.mjs` 是每个展示端 PR 在 node-27 出 live receipt 用的浏览器证据 oracle。它的判定逻辑与入口守卫只由 `scripts/__tests__/node27_display_v2_browser_evidence.test.mjs` 的 `node:test` 用例钉住，而 CI 里没有任何 job 跑它（#2815 把接 CI 列为 Non-goal，#2856 接手）。守卫若被改坏，脚本会静默 exit 0，桌面 oracle 误绿，而 CI 不会出红。

## What Changes

- `.github/workflows/ci.yml`：`changes` job 增加一个 filter / output，匹配 `scripts/**/*.mjs`、`scripts/__tests__/**` 与 `.github/workflows/ci.yml`；新增一个按该 output 触发的轻量 job，用 Node 20 跑 `scripts/__tests__/` 下全部 `*.test.mjs`，不安装任何依赖。
- 调用形式是 shell 展开的 `node --test scripts/__tests__/*.test.mjs`（node 收到的是具体文件路径）。裸目录参数在 Node 22+ 报 `Cannot find module`，加引号交给 node 展开的 glob 在 Node 20 报 `Could not find`，两者都不用。run 前置 `shopt -s failglob`：glob 零匹配时直接失败，而不是在 Node 22+ 上变成零用例通过。
- `tests/test_ci_shard_tests.py` 里一条 pytest 元守卫钉住：新 job 存在、受新 output 门控、filter 含上述三个路径、命令不是目录形式也不是加引号的 glob、带零匹配防护、不装依赖。
- `docs/runbooks/display-mobile-evidence.md` 里“目前不在 CI 里”的表述改为实际情况；根指令不改。

不改脚本与测试本身；不在 CI 里跑真实浏览器采集；不给 `scripts/` 引入 package.json 或依赖。design.md 省略（compact）。

## Triage

```text
Issue type: test
Fixture level: compact
Upstream suggested level: absent
Blast radius: 新 job 配错时要么永远 skipped / 零用例假绿（oracle 继续无人看守），要么对无关 PR 假红；changes job 改坏会影响全部车道的门控
Selected risk packs: Config / project setup; Documentation
Evidence floor: 新 job 的命令在 Node 20 与当前本地 Node 上各跑出全部用例通过；入口守卫改成恒假时同一命令 exit 非 0；读 ci.yml 的 pytest 元测试全绿；PR 上新 job 实际运行并通过
```

## Impact

- 受影响文件：`.github/workflows/ci.yml`、`tests/test_ci_shard_tests.py`（一条元守卫）、`docs/runbooks/display-mobile-evidence.md`。
- 受影响规格：`ci-merge-gates`。
