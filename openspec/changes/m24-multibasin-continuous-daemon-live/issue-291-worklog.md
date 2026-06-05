# Issue #291 (M24-3B) 动态动作流 + 进度工作日志

> 定制自 `.claude/skills/dual-end-issue-workflow`，按 #291 实际状态裁剪。
> 单一事实来源：本文件记录动作流定义 + 实时进度。每完成一步立即更新。

## 1. 目标与边界

- **Issue**: #291 M24-3B 多流域 live 身份 + partial-success 证明
- **分支**: `feat/issue-291-multibasin-identity`
- **In scope**（OpenSpec §3B.1–3B.4）：第二个可运行 model/basin（最小 fixture 可）；≥2 流域一趟 pass 到 published，per-basin 身份入 evidence；array retry/reindex 身份（`original_task_id` 映回 basin/segment，跨 river network 同名 segment 不合并）；per-basin partial-success 隔离（5 命名阶段 forcing/forecast/parse/frequency/publish 各失败一次：A 失败、B 发布、cycle aggregate=partial、B 排除 A）。
- **Out of scope**：并发机制本身（#290/§3A）、daemon 守护（#292/§4）。

## 2. 关键认定：#291 主要是测试驱动（基础设施已在 master）

Explore scope map 结论：多流域 pass、`original_task_id` reindex、segment 按 `(river_network_version_id, river_segment_id)` 键（构造防碰撞）均已存在并有测试。真正 GAP 是**需求场景未被充分证明**：5-stage partial 矩阵只测了 forcing；forecast/parse/frequency/publish 未逐一测；segment 跨网碰撞无测试；per-basin identity-in-evidence 待证。故按 KISS/YAGNI，本 issue 以**测试为主**，仅在测试暴露真实缺口时做最小代码改动。

## 3. 角色分工（双端）

| 角色 | 职责 |
|------|------|
| **Claude Code**（本地编排） | scope 评估、修复计划、候选去重 + 裁决、证据综合、git/PR、合并门、文档维护 |
| **Explore subagent**（派发，只读） | 代码 scope map |
| **fix subagent**（派发，leaf） | test-first 实现/修复；可编辑、跑 ruff，不 commit |
| **远端 node-22** | 真实 PostgreSQL/TimescaleDB/Slurm 测试 oracle |

## 4. 候选/裁决账本（按轮）

### Round-1（orchestrator 审 fix subagent 首次产出，publish-gate 类）
- **C1 publish quality-gate 破 userspace**：fix subagent 为满足 §3B.4 publish 用例新增 `_apply_publish_quality_gate`，按 `_publish_quality_state(entry).state != "ready"` 排除 basin 并降级 cycle=partial。**CONFIRMED / in-scope / merge-blocking**。证据：
  - chain.py:6654-6662 —— tiles-off basin 产生 residual_blocker，`residual_risk` 文案 "durable model outputs remain reusable"，即系统设计把 display/frequency/station/output_river 的 `unavailable` 当**咨询性 residual_blocker（带风险标注照常发布）**，非排除触发器；`_cycle_residual_blockers` 把它们收进 publish manifest metadata（chain.py:2645）一起广告。
  - `_display_contract`（chain.py:6571-6589）`state="unavailable"` **仅**由 `tiles_enabled is False` 触发；`tiles` 是真实生产可配能力（scheduler.py:5361/6642，`fallback=False`）。故合法「不出 tiles」的 basin 会被误排除、健康 cycle 误降级 partial。现有测试全用 `tiles:True` 故漏网。
  - publish 是单 non-array job，「A 在 publish 失败而 B 仍发布」本就无法 per-basin 表示 → gate 是错误机制。
  - **裁决/修**：回退该生产改动（删 `_apply_publish_quality_gate` + 调用点），publish 用例改证既有 manifest 排除行为——在最后一个 array 阶段 `frequency` 注入 partial，断言 publish manifest 的 `excluded_basins`/`basins`/`quality_states` 自然剔除 A、survivors 发布、cycle=`parsed_partial`、A 身份不泄漏到 B。#291 回归为**纯测试**交付。

### Round-2（综合复审 fix subagent 纠正后产出，全 diff 对抗式）
- 独立 reviewer 审全 diff（确认 chain.py 零改动，仅 2 测试文件 +187）。**无 merge-blocking**。5 个编排测试全 SOUND——断言挂在生产构建的 manifest（`excluded_basins`/`identity_contract`/`quality_states`，chain.py:2589-2622/6643-6690）上，Fake client 仅转发不伪造；isolation/identity 回归会让断言失败。
- 2 个非阻断 nit（转 follow-up，不再 churn 以免无谓重跑 node-22）：
  - **N1** segment-collision 测试（test_partial_success.py）驱动通用 `build_reindexed_manifest` 透传、用单数 `river_segment_id` 键，比 spec「parse rows/published products 不合并」浅一层（证复合键透传而非生产真实键控）。但 non-vacuous（名-only 键控会令其失败），且行为本就按 `(river_network_version_id, river_segment_id)` 安全（model_registry 复合键），跨网分离另由 identity-in-evidence 测试在 manifest 层用 per-basin `river_network_version_id` 独立证明。
  - **N2** parse/frequency partial 测试未断言 `status=="failed"`（已断 `error_code`+model/run，typed-failure 已满足）；forecast 测试有断。矩阵一致性 nit。
- **结论：零 in-scope CONFIRMED、零 merge-blocking PLAUSIBLE = CLEAN。** Round-1 publish-gate 缺陷已回退闭合。

## 5. 验证门（Evidence Floor #291）

```bash
uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py   # node-22 真实 DB
uv run ruff check .
openspec validate m24-multibasin-continuous-daemon-live --strict --no-interactive       # 本地
```

- ≥2-basin live pass receipt（node-22 多流域 live，依赖缺失则记 BLOCKED 合法终态；第二流域选 heihe）。
- reindex 身份测试（`original_task_id` 映回 basin/segment；同名 segment 跨网不合并）。
- partial-success 隔离测试（5 阶段矩阵：forcing 已有，forecast/parse/frequency/publish-manifest 新增）。

## 6. 进度日志（倒序，最新在上）

| 时间 | 阶段 | 动作 | 结果 |
|------|------|------|------|
| 2026-06-05 | Phase 8 live-7 | **真 both-basins published receipt**（cycle `gfs_2026060500`→`complete`，publish job 6043 succeeded）：复用 download/convert/forcing（6029/30/31 未重跑，**跳过下载**），qhh forecast `6040_0 COMPLETED`（不再 `OUTPUT_COLUMN_COUNT_MISMATCH`），parse/frequency 绿，publish 降级 manifest：`status=published` `degraded_to_display=true` `published_basins=2`；qhh layer `segment_count=1633`/274344 行、heihe `segment_count=2352`/395136 行；return-period 降级为 per-basin 诚实 `RETURN_PERIOD_RESULT_UNAVAILABLE` residual_blocker（非硬失败）；per-basin 身份零泄漏（distinct run_id/river_network_version_id/forcing_version_id/basin_version_id）。`hydro.river_timeseries` q_down 两流域齐（qhh 1633 段 / heihe 2352 段），`map.tile_layer` 各 published_flag=true。**两 fix 活体验证通过** | ✅ Receipt |
| 2026-06-05 | Phase 8 live-6 | live submit 抓出**第 4、5 个真实生产缺陷**并修：①**注册保真**——通用注册只 seed seg.shp 几何(qhh 3738/heihe 4759)、漏 `.sp.riv` SHUD 输出层(qhh 1633/heihe 2352)，致 forecast `verify_output` 期望 segment_count+1=3739 列、拒掉正确的 1634 列输出。修 `basins_geometry` 暴露 `output_segment_count` + `basins_registry_import` seed 输出层(`shud_output_river=true`,id `{model_id}_shud_riv_NNNNNN`)且记 `resource_profile.output_segment_count`（chain.py:5084 自动透传 manifest）；qhh 现存注册 profile 从真实 `.sp.riv` 派生补 1633（checksum c59a7fa）。②**publish 降级**——`flood_frequency_curve` 历史基线全库 0 行（两流域皆无、诊断流当年发的本就是流量 display），M3 publish 硬失败 `NO_PUBLISHABLE_PRODUCTS` 破 userspace；修 `_publish_from_database` 无 flood 行时降级走现成 `_publish_qdown_from_database` + 标 `degraded_to_display`，仅两者皆空才真失败（flood happy-path 字节不变）。commits 8cf7130 + 0601cea；新增/修测试本地 publisher 41 + registry 46 passed、ruff clean | ✅ 抓+修 |
| 2026-06-05 | Phase 8 live-5 | live submit 抓出**第三个真实生产缺陷**：`gfs_adapter.discover_cycles` 把 NOMADS 瞬时限流 403（`ForbiddenSourceError`）硬标 `retryable=False`，单次限流即把可用 cycle 永久判 `forbidden` 丢弃（#292 daemon 静默数据缺口，"never break userspace"）。受控探测证 NOMADS 403 数分钟自愈到 200（HEAD，默认/urllib UA 均 200，非 UA 问题）。修 `gfs_adapter.py:290` `retryable=True`（+注释，status/classifier="forbidden" 观测信号保留）+ 回归测试 `test_forbidden_discovery_stays_retryable`（非空洞：翻回 False 即 FAIL）；本地 24 passed、ruff clean。审核 CLEAN | ✅ 抓+修 |
| 2026-06-05 | Phase 8 live-4 | cfgrib 真相：环境**没坏**，是手动登录节点 `nhms-canonical convert` 漏注入 `LD_LIBRARY_PATH=$NHMS_GRIB_ENV_ROOT/lib`（chain.py:7571-7579 仅在 sbatch 渲染路径自动注入；手动路径无 hook）。本地复证 `LD_LIBRARY_PATH=…/nhms-grib/lib uv run python -c "import cfgrib"`→0.9.15.1。**uv venv 未重装**（保 qhh userspace）。补 runbook 手动配方缺的 `LD_LIBRARY_PATH` 行；潜伏脆弱点（daemon 未 source env 则静默漏注入）记入 #292 §4.5 preflight + 调 issue。双流域 download→canonical→forcing 全绿（qhh 386 站 / heihe 1709 站，distinct forcing_version_id/URI，`===ALLDONE===`） | ✅ |
| 2026-06-05 | Phase 8 live-2 | 真跑 live 抓出真实生产 bug：bootstrap forcing 站点 PK 未按 project 命名空间化 → 第二流域撞 qhh 主键（事务守卫回滚保住 qhh）。修 `82a137e`（`{project_name}_forc_`，qhh 字节不变）+ 回归测试 113 passed。**重跑：heihe 注册成功 + active**（1709 `heihe_forc_*` 站点、7111 segments、包已发布；qhh 386 站点完好）；一趟真实 pass **双流域均被发现为 runnable candidate**（per-basin 身份完整）。live-to-published 被 prod-venv cfgrib/eccodes 损坏（GRIB2→canonical 失败，流域无关）阻断 → 用户授权修 | ✅ 注册/发现；live 续修 |
| 2026-06-05 | Phase 8 live-1 | 首轮 live-ops：discover/publish OK，bootstrap 阻断于 forcing 站点 PK 碰撞（见 live-2 修复） | ✅ 抓 bug |
| 2026-06-05 | Phase 8 EF | node-22 权威 EF（c451f03，含 N1/N2 强化）真实 DB **582 passed, 0 failed**；ruff clean | ✅ |
| 2026-06-05 | Round-2 修复 | N1 segment 测试改证生产 `_output_river_contract` 键控 + N2 补 `status=="failed"`（c451f03，纯测试，chain.py 零改动）；本地 137 passed | ✅ 不留 follow-up |
| 2026-06-05 | Round-1 修复 | fix subagent 回退 publish-gate + 改证 manifest 排除；保留 forecast/parse/frequency/segment/identity 测试（2504b68） | ✅ |
| 2026-06-05 | Round-1 | orchestrator 审出 publish-gate 破 userspace（tiles-off 合法降级被误排除）→ CONFIRMED merge-blocking | ✅ |
| 2026-06-05 | Phase 1 实现 | fix subagent test-first 产出 5-stage 矩阵 + segment-collision + identity-in-evidence；发现并（误）修 publish 隔离 | ⚠️ 待纠正 |
| 2026-06-05 | Phase 0 scope | Explore scope map：多流域/reindex/segment 键基础设施已在 master，#291 主要测试驱动 | ✅ |
