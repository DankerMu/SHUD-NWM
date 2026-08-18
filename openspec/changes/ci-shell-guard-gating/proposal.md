## Why

Issue #1138: `scripts/**/*.sh` 改动既进不了 CI 的 `backend` paths-filter（第一层），
`scripts/select_ci_tests.py` 也没有任何 `.sh` → 守卫测试的映射（第二层）。12 个被
`tests/` 真实 subprocess 执行的 shell wrapper（node-22 systemd 生产入口在内）的
改动会得到一个"绿得没有信息量"的 CI 结果——守卫用例一条不跑。

## What Changes

- `.github/workflows/ci.yml` `backend` filter 增加 `scripts/**/*.sh`。
- `scripts/select_ci_tests.py`：`PATH_TEST_RULES` 增加 12 个有守卫覆盖的
  `scripts/*.sh` → 守卫 test 文件映射；未匹配映射的 `scripts/**/*.sh` 走
  CORE_SMOKE_TESTS 兜底（不再返回空集）。
- `tests/test_select_ci_tests.py` 新增 sh-only / sh+docs / sh+py 三种改动集断言。

## Non-Goals

- 门控强度（draft/ready、全量 unit-test 是否上 PR）——#1129 地盘。
- wrapper 脚本自身逻辑；shellcheck / shell lint 引入。

## Risk triage

- Fixture level: compact（CI 门控 wiring + 选择器纯函数映射，无运行时行为改动）。
- Repair intensity: low（隔离面：选择器 + YAML 一行；爆炸半径=CI 选测集合）。
- Risk packs:
  - selected: test-evidence（门控正确性即本 issue 主题；三场景断言 + 红证）。
  - not selected: path-safety/auth/publish 等（无文件 IO/权限/发布行为）；
    geospatial/db/slurm 域包（不触及）。

## Must preserve

- 既有 `.py` 路径的选测行为不变（不缩小任何现有选择）。
- 空选集的 collect-only 冒烟分支语义不变（仅入口收窄：有映射的 .sh 不再落入）。
