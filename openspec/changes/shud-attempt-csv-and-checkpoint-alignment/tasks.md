## A. Lane #1491（direct-grid station CSV 的 attempt 起点卫生）

> round-0 审把本车道的锚定从「本次 `.tsd.forc` 行集的**名字集合**」改成
> **来源/时序**。理由（编排者已独立复核）：谓词唯一生产调用点
> `runtime.py:1113` 的目标路径按定义名字就在行集里，「行集外文件」恒假；
> 而第二分支今天真正保护的是**本次 attempt 刚 staging 的 model package 成员**
> （`prepare_workspace` :572 先 staging model package，:582 才拷 CSV）。

- [ ] A.1 **不变量**：只有**早于本次 attempt staging** 的同名残留可被清除；
      本次 attempt 自己 staging 出来的东西（model package / forcing package /
      IC 成员）落在声明的 station CSV 目标路径上时**仍 fail-closed**。
      删除必须 **no-follow + containment 约束于 `model_input_dir`**
      （沿用 safe_fs / `_clear_packaged_initial_states`(:1595) 既有原语，
      不新增 follow 面），且**只针对声明的 station CSV 目标**，
      不得无差别清 `input/<project>/`。
      **顺序约束（无论选哪条机制）：所有删除必须先于任何 station CSV 拷贝**
      ——否则第 k 行删除失败时第 1..k-1 份已拷完，A.6(iv) 的
      「无部分 staging 产物」不可满足
- [ ] A.2 **机制由实现者裁定并在本 task 落账**（fixture 不预先钉死）。
      已核可行的两条：(i) 把行集从 object store 侧提前读出
      （`_prepare_forcing_package_context`:1166-1190 纯读、checksum 已验），
      在 model package staging **之前**做起点卫生；(ii) 记录**本次 attempt
      staging 产生的路径集合**，谓词只对该集合 fail-closed。
      选型成本（round-0 审复核后的准确版，别踩）：
      行集当前从**已 staging** 的 `shud/` 读（`shud_dir = model_input_dir /
      "shud"` :1042、`source_tsd` :1088）；走 (i) 不只是「另取来源」，还要把
      `_prepare_forcing_package_context` 的**调用点从 :573 提到 model package
      staging(:572) 之前**，这会**改变失败次序**（forcing 包校验失败将先于
      model package staging），必须顺带跑既有
      `..._fails_before_staged_status` 家族（`tests/test_shud_runtime.py:805`、
      `:1114`、`:1147`、`:1187`）。
      **便利**：object-store 侧的行集**已经现成**——
      `_direct_grid_runtime_checksum_entries`（:2095，`:2157-2159`）就是从
      object store 的 tsd 生成 `shud/<csv>` 条目集的，不必新造读取路径
- [ ] A.3 谓词 `_validate_direct_grid_station_filename_target`（:3208-3224）
      **力争一字不动**；保留名分支**必须一字不动**。若不变量要求改签名，
      **`tests/test_shud_runtime.py:3239` 的直接调用点必须跟着改**
      （round-0 审点名的、初稿未列的站点）
- [ ] A.4 删除失败 fail-loud：**复用**既有码
      `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`（`runtime.py:1978`，#1355
      引入；同函数族/同删除原语/同 retry 语义，且已有「不入
      `TRANSIENT_ERROR_CODES`」测试托底 `tests/test_shud_runtime.py:1946`）。
      **不新铸**同类第二个码；**不得**复用
      `DIRECT_GRID_STATION_FILENAME_COLLISION`（会把新故障伪装成旧故障）
- [ ] A.5 红证（确定性）：同 `run_id`/同 manifest 连跑两次 `prepare_workspace`
      —— pre-fix 第二次逐字抛
      `DIRECT_GRID_STATION_FILENAME_COLLISION | ... : forcing.csv`
      （编排者与 round-0 审**各自独立实测**于 master `094caea4`），
      post-fix 第二次成功且每个 station CSV 内容 == **本次** staging 产物
      （断言内容，不只断言存在）
- [ ] A.6 边界缝四条：
      (i) **model package 携带与声明 station CSV 同名的成员** → 仍抛
          `DIRECT_GRID_STATION_FILENAME_COLLISION`、其字节不变
          （审者实测该形状 pre-fix 即红，是**可构造**的那条；替换掉初稿里
          「行集外文件」那条恒假场景）
      (ii) 保留名 → 仍抛 collision 码，**在 helper 层直接调谓词**断言；
           **不要**写成 staging 层 e2e —— `_direct_grid_station_filename`
           （:3134-3150）要求 `.csv` 后缀，保留名无一以 `.csv` 结尾，
           staging 层会先抛 `DIRECT_GRID_STATION_FILENAME_INVALID`
           （既有 e2e 用例 `tests/test_shud_runtime.py:3301` 断言的正是该码）
      (iii) 同一行集内**两行同名**（`_read_shud_forcing_station_rows`:3104-3122
            不查重，`.tsd.forc` 是非受信输入）→ 仍抛
            **`DIRECT_GRID_STATION_FILENAME_COLLISION`**（钉死码名，
            不得新铸第三个码），不得退化为静默 last-write-wins。
            可构造性已核：`required_relative_paths` 是 **set**（:2157-2159），
            两行同名只产生一个 required path，包 manifest 给一条
            `shud/forcing.csv` 条目即可，`rows` 仍是两行——checksum 层
            不会提前拦住它
      (iv) 残留 CSV 删除失败（只读目录 / unlink 抛错）→
           `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` 终止 attempt，
           **无部分 staging 产物**（依赖 A.1 的顺序约束）。
           现成先例可抄：`tests/test_shud_runtime.py:1879`（undeletable）/
           `:1911`（unlink IO error）
- [ ] A.7 既有 e2e 用例
      `test_runtime_direct_grid_station_filename_collision_fails_without_overwriting_sp_att`
      **不拆分、不改断言**（初稿要求拆分建立在误读上，round-0 审已纠正）；
      仅在 A.3 改签名时跟着改传参。若最终确实改了它的任何断言，
      必须在本 task 逐条论证判别力不降
- [ ] A.8 非 direct-grid staging 分支（:1121 `_copy_staged_file_no_follow`）
      行为逐字不变——既有用例全绿 + 该分支不经过新删除路径的结构论证

## B. Lane #1317（checkpoint 与 `Update_IC_STEP` 对齐）

- [ ] B.1 半(a) recovery rerun 侧：`_recover_missing_state_checkpoints`
      （`runtime.py:767-920`）在改写 `END`/`END_TIME`/`OUTPUT_DIR` 的同一段
      加 `Update_IC_STEP` = `hour * 60`。
      **前置事实（已核实，勿再自行推断）**：该循环**逐小时**
      （`for hour in checkpoint_tracker.missing_hours():` :817），故 `hour*60`
      对本次 rerun 的 END 恒整除，**不需要 gcd**。
      **门控须与 `generate_cfg_para` 一致**（后者只在 project 模式写该键，
      :621-624）——无条件注入会给 non-project cfg 追加一个其产地从不写的键
      （`_replace_or_append` 不存在即 append，:3819-3820）
- [ ] B.2 半(a) 的隔离证据：rerun 结束后**主 run 已发布的 cfg 文本逐字回到
      rerun 前**（既有 inner try/finally 恢复契约 :884-893；既有断言
      `tests/test_shud_runtime.py:4851` 可托底）——防止注入泄漏进主 run
- [ ] B.3 半(b) manifest 前置门：对 `hour*60 % update_ic_step_minutes != 0`
      的组合以**稳定 typed error code 拒绝**（fail-closed；gcd 自动纠正方案
      已在 proposal 显式否决，不得改选）。
      `services/orchestrator/chain_manifests.py:486` 与 `:643` 是**同一模式的
      两份兄弟副本，必须两处一起改**——测试必须**分别**驱动两处产地
      （`build_forecast_runtime_manifest` / `build_forecast_run_manifest`），
      单点覆盖不算达标。**先例指针（round-0 审更正）**：抄
      `test_chain_manifest_legacy_builders_use_monkeypatched_helper_aliases`
      （`tests/test_orchestration_chain.py:11667`），它**真的**分别建出两份
      manifest 并断言 `:11768-11769`（run manifest）与 `:11790`（runtime
      manifest）；**不要**抄 `:11586`/`:11601`——那是 mock 掉 builder 后断言
      委托参数的绑定测试，不是真驱动。全仓设 `state_checkpoint_hours` 的**只有这两处**
      （round-0 审已复核无第三处）
- [ ] B.4 红证（确定性）：
      (a) 断言 recovery rerun **写出的 cfg 文本**含
          `Update_IC_STEP<sep>{hour*60}`。**两条构造约束，缺一红证失效**：
          **(i)** 必须保证 pre-fix cfg 的 `Update_IC_STEP` **缺失或 ≠ hour\*60**
          ——`generate_cfg_para` 在 project 模式已按 manifest 写该键，
          用 `hours=[12]`/step 720 恢复 hour=12 时 pre-fix 文本恰好相同、
          **红不了**（改用如 `[6,12]` 恢复 12）；
          **(ii)** 断言须在 rerun **进行中**由 stub solver 捕获 cfg
          （stub 从 `sys.argv[1]` 读 cfg，先例
          `tests/test_shud_runtime.py:4486-4517`）——cfg 在 inner `finally`
          被逐字还原，rerun 之后再读只看得到原文
      (b) 用 `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC="0,5"`（最小反例：
          hours `[5,19]` / step 300、`19*60 % 300 = 240`）与 `"0,5,12"`
          （hours `[5,7,12]` / step 300，2/3 不可达）驱动**两处产地**，
          断言 typed 拒绝；pre-fix 必红。
          **horizon 必须写死 ≥ 19（如 72）**——`_forecast_state_checkpoint_hours`
          过滤 `hour <= horizon`（`chain_manifest_contracts.py:422`），
          horizon < 19 时 19 被丢掉、`"0,5"` 反例消失
- [ ] B.5 零回归：默认配置 `0,12`（hours `[12]`/720）与等间距 `0,6,12,18`
      （hours `[6]`/360）行为逐字不变
      （`tests/test_warm_start.py:162-184` 与 `tests/test_shud_runtime.py`
      相关用例全绿、零断言改动）
- [ ] B.6 analysis 侧（`chain_manifests.py:693-696` /
      `_analysis_update_ic_step_minutes`:729）**不改**——它不设
      `state_checkpoint_hours`（round-0 审已复核），不在缺陷面内；
      在 spec 场景里写明边界，避免下一位误以为漏改

## C. Verification

- [ ] C.1 `uv run pytest -q tests/test_shud_runtime.py tests/test_warm_start.py
      tests/test_orchestration_chain.py`
- [ ] C.2 `uv run ruff check .`
- [ ] C.3 `openspec validate shud-attempt-csv-and-checkpoint-alignment
      --strict --no-interactive`
- [ ] C.4 merge 后 node-27 receipt（C.1 三套件）：**直接在 `umask 022` 下跑**
      ——默认 umask(0002) 会有 ~80 条 #1513 file-provider 预置红淹掉真实回归
      （2026-08-19 实测口径，见 #1513 评论）。记 #1491 与 #1317
