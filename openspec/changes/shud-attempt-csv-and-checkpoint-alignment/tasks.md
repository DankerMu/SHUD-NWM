## A. Lane #1491（direct-grid station CSV 的 attempt 起点卫生）

> round-0 审把本车道的锚定从「本次 `.tsd.forc` 行集的**名字集合**」改成
> **来源/时序**。理由（编排者已独立复核）：谓词唯一生产调用点
> `runtime.py:1113` 的目标路径按定义名字就在行集里，「行集外文件」恒假；
> 而第二分支今天真正保护的是**本次 attempt 刚 staging 的 model package 成员**
> （`prepare_workspace` :572 先 staging model package，:582 才拷 CSV）。

- [x] A.1 **不变量**：只有**早于本次 attempt staging** 的同名残留可被清除；
      本次 attempt 自己 staging 出来的东西（model package / forcing package /
      IC 成员）落在声明的 station CSV 目标路径上时**仍 fail-closed**。
      删除必须 **no-follow + containment 约束于 `model_input_dir`**
      （沿用 safe_fs / `_clear_packaged_initial_states`(:1595) 既有原语，
      不新增 follow 面），且**只针对声明的 station CSV 目标**，
      不得无差别清 `input/<project>/`。
      **顺序约束（无论选哪条机制）：所有删除必须先于任何 station CSV 拷贝**
      ——否则第 k 行删除失败时第 1..k-1 份已拷完，A.6(iv) 的
      「无部分 staging 产物」不可满足

      **落账**：删除点落在 `prepare_workspace`（`workers/shud_runtime/runtime.py:570-583`）
      的**最开头**——早于 packaged-IC clear、早于 model package staging、早于
      forcing 包 staging、早于 IC staging、早于 CSV 拷贝。**结构论证**：因为卫生
      整体先于 :583-597 的全部 staging，*任何* stager（model package / forcing
      package / IC）写到声明 station CSV 目标上的文件都还不存在于删除时刻，必然
      存活到 :1113 的谓词 → 仍 collision。不变量与「哪个 stager 写的」无关。
      顺序约束是**结构满足**（删除先于一切 staging），故 A.6(iv) 的「无部分
      staging 产物」比要求更强：一份 staging 产物也没有。
      删除原语：`unlink_no_follow(model_input_dir / name, containment_root=model_input_dir,
      missing_ok=True)`（`_clear_predecessor_direct_grid_station_csvs`，
      `runtime.py:1296-1347`），目录/symlink 形态由原语自身拒绝；
      名字来自 `_declared_direct_grid_station_csv_names`（`runtime.py:3310-3336`），
      经 `_direct_grid_station_filename` 校验（无 `/`、无 `..`、必须 `.csv`），
      故 containment 是双保险，且**保留名结构上不可达**（无保留名以 `.csv` 结尾）。
      删除集合 = 「本次声明的 station CSV 名字」∩「`model_input_dir` 当前条目」，
      用一次 `list_directory_no_follow` 求交；不存在的名字**不发起 unlink**
      ——这条对既有测试 `tests/test_shud_runtime.py:1911`（全局 monkeypatch
      `unlink_no_follow` 抛 OSError）是必要的：空工作区不触发新删除路径，
      该用例的 #1355 错误消息断言保持不变

- [x] A.2 **机制由实现者裁定并在本 task 落账**（fixture 不预先钉死）。
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

      **落账——裁定：选 (i)，但必须加一条 fixture 未预见的修正（best-effort 早
      调用 + 失败时在原站点重跑）。**

      *为什么选 (i)*：(i) 让谓词 `_validate_direct_grid_station_filename_target`
      **一字未动**（A.3 零成本达标，`tests/test_shud_runtime.py:3239` 直接调用点
      也不用改），并把「本次 attempt 已 staging 的东西仍 fail-closed」变成
      **结构性质**（删除早于一切 staging）而不是一份需要维护的路径集合。
      (ii) 要把「本次 staging 产生的路径集合」从 `_stage_artifact` /
      `_stage_initial_state` 一路 plumb 到谓词，谓词签名必改，且新增一份必须与
      三个 stager 保持同步的可变状态——同样的不变量，代价高一个量级。

      *fixture-(i) 的字面写法会破坏 A.7（本实现的事实修正，已上报为偏离）*：
      `_prepare_forcing_package_context` 内部会经
      `_direct_grid_runtime_checksum_entries`(:2158) → `_direct_grid_station_filename`
      抛 `DIRECT_GRID_STATION_FILENAME_INVALID`。既有 e2e
      `test_runtime_direct_grid_station_filename_collision_fails_without_overwriting_sp_att`
      在该码抛出后读 `input/alias-a/alias-a.sp.att` 的字节
      （`tests/test_shud_runtime.py:3304`）——那份文件正是 :572 model package
      staging 写的。无条件把调用点提前，该读变成 `FileNotFoundError`，A.7 的
      「不改断言」不可能满足。round-0 审把两条机制都标为「已核可行」，(i) 在
      字面上并不可行。

      *修正*：`_forcing_package_context_for_attempt_hygiene`
      （`runtime.py:1222-1252`）以 **best-effort** 方式提前调用——任何异常
      吞掉并返回 `None`，`prepare_workspace` 在 model package staging **之后**
      于历史站点无条件重跑该调用并让它抛。于是：
      成功路径只解析 **1 次**（结果由 `prepare_workspace` 的局部变量复用，
      **不是** memo 缓存——round-1 D.2 已纠正此措辞）；失败路径的
      **错误码与次序逐字不变**（round-1 D.1 后失败路径解析 **3 次**：
      早探 + 原地重试 + 历史站点，每次都重算全部成员哈希）。
      吞异常是 fail-**closed**：跳过卫生 = 退回本次改动前的行为（collision
      门照样拒），不会给任何路径新增覆盖写许可。

      *实测代价*：`tests/test_shud_runtime.py` 全套 + `..._fails_before_staged_status`
      家族（:805 / :1114 / :1147 / :1187）+ oversized-manifest 的
      `tracking_store.checksum_calls` 断言（:1109）**零改动全绿**；
      C.1 三套件见 C 段。happy path 无额外 object-store 读

- [x] A.3 谓词 `_validate_direct_grid_station_filename_target`（:3208-3224）
      **力争一字不动**；保留名分支**必须一字不动**。若不变量要求改签名，
      **`tests/test_shud_runtime.py:3239` 的直接调用点必须跟着改**
      （round-0 审点名的、初稿未列的站点）

      **落账**：谓词**一字未动**（`git diff` 中该函数零 hunk），签名未变，
      `tests/test_shud_runtime.py:3239` 未动。保留名分支一字未动

- [x] A.4 删除失败 fail-loud：**复用**既有码
      `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`（`runtime.py:1978`，#1355
      引入；同函数族/同删除原语/同 retry 语义，且已有「不入
      `TRANSIENT_ERROR_CODES`」测试托底 `tests/test_shud_runtime.py:1946`）。
      **不新铸**同类第二个码；**不得**复用
      `DIRECT_GRID_STATION_FILENAME_COLLISION`（会把新故障伪装成旧故障）

      **落账**：复用该码，未新铸。两个抛出点：目录列举失败、单个 unlink 失败
      （`runtime.py:1295-1310`）。既有 retry 断言
      `test_direct_grid_residue_cleanup_failed_is_not_auto_retryable` 未改动、
      仍绿

- [x] A.5 红证（确定性）：同 `run_id`/同 manifest 连跑两次 `prepare_workspace`
      —— pre-fix 第二次逐字抛
      `DIRECT_GRID_STATION_FILENAME_COLLISION | ... : forcing.csv`
      （编排者与 round-0 审**各自独立实测**于 master `094caea4`），
      post-fix 第二次成功且每个 station CSV 内容 == **本次** staging 产物
      （断言内容，不只断言存在）

      **落账**：`test_runtime_direct_grid_second_attempt_restages_station_csvs_over_prior_attempt_residue`
      （`tests/test_shud_runtime.py:3331`）。**pre-fix 红形逐字复现**（本实现者
      在本分支 `fb1b4178` 树上实测，源码未改）：
      ```
      workers.shud_runtime.runtime.SHUDRuntimeError: Direct-grid SHUD forcing
      station filename collides with a staged model/runtime file: forcing.csv
      workers/shud_runtime/runtime.py:3220: SHUDRuntimeError
      ```
      与编排者/round-0 审的实测形状一致（`: forcing.csv`）。
      **判别力加强**：两次之间把三份已 staging 的 CSV 全部改写成
      `b"residue drift from the previous attempt\n"`，第二次必须让内容重新等于
      object store 里的字节——「同 manifest 两次 = 字节天然相同」的伪绿被排除，
      钉的是**删除后重拷**，不是「容忍既有文件」。post-fix 绿

- [x] A.6 边界缝四条：
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

      **落账**（红 vs 守门的区分，防止把「红跑里 pass」误读成假红）：
      - (i) `test_runtime_direct_grid_model_package_member_on_station_csv_target_still_fails_closed`
        （:3368）——**守门用例，pre-fix 与 post-fix 都绿**，钉的是本改动
        **不得**放宽的东西。断 `message.endswith(": forcing.csv")` +
        `input/alias-a/forcing.csv` 字节等于 model package 成员原文
      - (ii) `test_direct_grid_station_filename_target_still_refuses_every_reserved_name`
        （:3452）——helper 层，遍历全部 8 个保留后缀。**守门用例，两侧都绿**。
        既有 A.7 冻结用例 :3238-3244 已覆盖 `.sp.att` 一例，这里补全其余 7 个，
        不动那份冻结用例
      - (iii) `test_runtime_direct_grid_duplicate_station_filenames_in_one_row_set_fail_closed`
        （:3397）——两行都声明 `forcing_002.csv`（id 2/3，满足 `.sp.att` FORC
        ownership），包 manifest 只给一条 `shud/forcing_002.csv`。
        **守门用例，两侧都绿**；断 collision 码 + 首份拷贝字节未被第二行覆盖
      - (iv) **真红**，三个用例：
        `test_runtime_direct_grid_station_csv_residue_that_will_not_unlink_fails_loud[directory]`
        / `[symlink]`（:3485，参数化）与
        `test_runtime_direct_grid_station_csv_residue_unlink_io_error_fails_loud`（:3518）。
        pre-fix 红形逐字：
        ```
        # directory / symlink 形态
        AssertionError: assert 'WORKSPACE_PATH_UNSAFE' == 'DIRECT_GRID_...LEANUP_FAILED'
        tests/test_shud_runtime.py:3513: AssertionError
        # monkeypatch OSError 形态
        AssertionError: assert 'forcing.csv' in 'Undeclared staged SHUD station-index
        member shud/qhh.tsd.forc could not be removed from the run input workspace
        before direct-grid staging: read-only file system'
        tests/test_shud_runtime.py:3541: AssertionError
        ```
        （即 pre-fix 根本没有 station-CSV 清理路径：目录/symlink 形态一路撞到
        拷贝原语的 `WORKSPACE_PATH_UNSAFE`；OSError 形态命中的是 #1355 的
        index-member 清理而非 station CSV。）
        「无部分 staging 产物」的判别力靠：两次 attempt 之间删掉
        `alias-a.sp.att`（model package 成员标记），失败的第二次必须让它**仍然
        缺席**——证明中止发生在 model package staging 之前。post-fix 全绿

- [x] A.7 既有 e2e 用例
      `test_runtime_direct_grid_station_filename_collision_fails_without_overwriting_sp_att`
      **不拆分、不改断言**（初稿要求拆分建立在误读上，round-0 审已纠正）；
      仅在 A.3 改签名时跟着改传参。若最终确实改了它的任何断言，
      必须在本 task 逐条论证判别力不降

      **落账**：该用例**一字未动**（不拆分、不改断言、不改传参）。
      它正是 A.2 选型修正的驱动力（见 A.2 落账）：正因为要保住它第 3304 行
      「`.sp.att` 已 staging 且字节可读」的断言，提前调用才必须是 best-effort
      而非无条件搬迁

- [x] A.8 非 direct-grid staging 分支（:1121 `_copy_staged_file_no_follow`）
      行为逐字不变——既有用例全绿 + 该分支不经过新删除路径的结构论证

      **落账**：结构论证——`_clear_predecessor_direct_grid_station_csvs` 首行
      `if not forcing_context.is_direct_grid: return`（`runtime.py:1326-1327`），
      非 direct-grid manifest 一条 unlink 都不发起；`is_direct_grid` 来自与
      staging 层同一个 `_ForcingPackageContext`，不可能与拷贝分支的判定分叉。
      既有 `test_runtime_non_direct_grid_staging_leaves_residual_index_member_alive`
      （:1949）未改动、仍绿。新增
      `test_runtime_non_direct_grid_second_attempt_behaviour_is_unchanged`
      （:3545）——**守门用例，两侧都绿**：该车道靠 `_copy_staged_file_no_follow`
      的覆盖写，本来就不会被卡住，改动后依然如此

## B. Lane #1317（checkpoint 与 `Update_IC_STEP` 对齐）

- [x] B.1 半(a) recovery rerun 侧：`_recover_missing_state_checkpoints`
      （`runtime.py:767-920`）在改写 `END`/`END_TIME`/`OUTPUT_DIR` 的同一段
      加 `Update_IC_STEP` = `hour * 60`。
      **前置事实（已核实，勿再自行推断）**：该循环**逐小时**
      （`for hour in checkpoint_tracker.missing_hours():` :817），故 `hour*60`
      对本次 rerun 的 END 恒整除，**不需要 gcd**。
      **门控须与 `generate_cfg_para` 一致**（后者只在 project 模式写该键，
      :621-624）——无条件注入会给 non-project cfg 追加一个其产地从不写的键
      （`_replace_or_append` 不存在即 append，:3819-3820）

      **落账**：注入落在既有 `if project_mode:` 分支内、`END` 改写的紧邻下一行
      （`workers/shud_runtime/runtime.py:858-873`），与 `OUTPUT_DIR` 改写同属
      一段。**门控只按 `project_mode`**，**不**额外要求
      `_update_ic_step_minutes(manifest) is not None`：B.1 的括号注释把「一致」
      定义为 project-mode 条件，而 spec 场景要求 recovery「独立于该配置」
      保持正确——再叠一层 manifest 条件会把配置依赖重新引回来，
      manifest 未声明 cadence 时反而恢复不了目标小时。
      cfg-style 车道不受影响（新增守门用例见 B.4 落账）

- [x] B.2 半(a) 的隔离证据：rerun 结束后**主 run 已发布的 cfg 文本逐字回到
      rerun 前**（既有 inner try/finally 恢复契约 :884-893；既有断言
      `tests/test_shud_runtime.py:4851` 可托底）——防止注入泄漏进主 run

      **落账**：既有 :4851 的 `cfg_path.read_bytes() == cfg_before`
      （project + cfg 两种 command_style 参数化）未改动、仍绿。新增 B.4(a)
      用例内额外断言 `cfg_path.read_bytes() == cfg_before` **且**
      `"Update_IC_STEP\t360" in cfg_path.read_text()`——注入了 720 的那次 rerun
      结束后，主 run cfg 仍是原来的 360。**守门断言，两侧都绿**

- [x] B.3 半(b) manifest 前置门：对 `hour*60 % update_ic_step_minutes != 0`
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

      **落账**：新增稳定 typed 码 `STATE_CHECKPOINT_HOURS_UNREACHABLE`
      （`services/orchestrator/chain_manifests.py:119`，并入 `__all__`），
      经共享 helper `_require_reachable_state_checkpoint_hours`（:402-449）
      抛 `OrchestratorError`，`details` 携带
      `run_id` / `state_checkpoint_hours` / `update_ic_step_minutes` /
      `unreachable_hours` / `forecast_horizon_hours` / `allowed_cycle_hours_utc`。
      **两处产地各自调用**：`build_forecast_runtime_manifest`（:541-546）与
      `build_forecast_run_manifest`（:705-710）——共享 helper 消除 copy drift，
      调用点仍是两个，测试仍分别驱动。fail-closed，不做 gcd 自动纠正。
      测试按 :11667 先例搭台（保留其余 assembly helper 的 monkeypatch，
      **移除** `_forecast_state_checkpoint_hours` 的 monkeypatch，改由
      `monkeypatch.setenv` 驱动真实推导），两处产地各自参数化驱动

- [x] B.4 红证（确定性）：
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

      **落账 (a)**：`test_run_shud_recovery_rerun_sets_update_ic_step_to_its_own_target_hour`
      （`tests/test_shud_runtime.py:5189`）。
      **约束 (i) 落地**：`state_checkpoint_hours=[6,12]` / `update_ic_step_minutes=360`，
      发布的 cfg 携带 `Update_IC_STEP\t360`（即 `generate_cfg_para` 在该
      manifest 下会写的值），f012 需要的 720 与 pre-fix 文本**确实不同**。
      **约束 (ii) 落地**：新增 project-mode stub `_PROJECT_CFG_CAPTURING_SOLVER_STUB`
      （:4813），rerun **进行中**把 `sys.argv` 指向的 cfg 原文抄进自己的
      `-o` 输出目录（`state_checkpoint_recovery/f<hhh>/captured.cfg.para`），
      测试读那份副本；rerun 后主 cfg 已被 inner `finally` 还原，读它看不到注入。
      **pre-fix 红形逐字**：
      ```
      AssertionError: assert 'Update_IC_STEP\t720' in 'START\t0\nEND\t0.5\n
      ASCII_OUTPUT\t1\nSCR_INTV\t1440\nSEGMENT_COUNT\t2\nUpdate_IC_STEP\t360\n
      OUTPUT_DIR\t.../state_checkpoint_recovery/f012\n'
      tests/test_shud_runtime.py:5229: AssertionError
      ```
      post-fix 绿。f006 一并断言 `Update_IC_STEP\t360`，钉的是**本行的小时**
      而不是「碰巧等于 manifest 全局 min」；并断 `"Update_IC_STEP\t360" not in
      f012_cfg`。附带守门用例
      `test_run_shud_cfg_style_recovery_rerun_does_not_append_update_ic_step`
      （:5240，新增 `_CFG_STYLE_CAPTURING_SOLVER_STUB` :4841）钉 B.1 的
      project-mode 门控——**两侧都绿**。

      **落账 (b)**：`test_forecast_manifest_assembly_rejects_unreachable_state_checkpoint_hours`
      （`tests/test_orchestration_chain.py:11924`），双重参数化 =
      2 配置 × 2 产地 = 4 例，全部**真红**。pre-fix 红形逐字：
      ```
      Failed: DID NOT RAISE <class 'services.orchestrator.chain_types.OrchestratorError'>
      tests/test_orchestration_chain.py:11946: Failed
      ```
      horizon 写死 `_CHECKPOINT_ALIGNMENT_HORIZON_HOURS = 72`（≥19）。
      **本实现的一处自纠（非 fixture 偏离）**：初版断言值误写成 `[7]`，
      与 fixture 自己的「2/3 不可达（含 T+12）」口径不符——`12*60 % 300 = 120 ≠ 0`，
      12 同样不可达。已更正为 `[19]` / `[7,12]`，与 proposal / B.4(b) 原文一致

- [x] B.5 零回归：默认配置 `0,12`（hours `[12]`/720）与等间距 `0,6,12,18`
      （hours `[6]`/360）行为逐字不变
      （`tests/test_warm_start.py:162-184` 与 `tests/test_shud_runtime.py`
      相关用例全绿、零断言改动）

      **落账**：新增
      `test_forecast_manifest_assembly_keeps_aligned_cycle_configurations_unchanged`
      （`tests/test_orchestration_chain.py:11968`），2 配置 × 2 产地 = 4 例，
      断 `state_checkpoint_hours == [12]` / `[6]` 且
      `update_ic_step_minutes == min*60`。**守门用例，两侧都绿**。
      `tests/test_warm_start.py` 与 `tests/test_shud_runtime.py` 零断言改动，
      见 C.1

- [x] B.6 analysis 侧（`chain_manifests.py:693-696` /
      `_analysis_update_ic_step_minutes`:729）**不改**——它不设
      `state_checkpoint_hours`（round-0 审已复核），不在缺陷面内；
      在 spec 场景里写明边界，避免下一位误以为漏改

      **落账**：`build_analysis_run_manifest` 与 `_analysis_update_ic_step_minutes`
      零改动（`git diff` 无 hunk）。边界已写进
      `specs/cross-cycle-warm-start-chaining/spec.md` 的
      "aligned configurations are unaffected" 场景末句，并在
      `_require_reachable_state_checkpoint_hours` 的 docstring 里再述一次，
      让读代码的人不必回头翻 spec

## C. Verification

- [x] C.1 `uv run pytest -q tests/test_shud_runtime.py tests/test_warm_start.py
      tests/test_orchestration_chain.py`

      **落账（round-0 口径，已被 D.5 取代——终态见 §D.5 的 649）**：
      `647 passed in 954.79s (0:15:54)`，exit 0，零 failed / 零 error。
      本地 macOS，随机顺序插件启用（未加 `-p no:randomly`）——env 驱动的
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` 用例用 `monkeypatch.setenv`，
      不泄漏到其他用例。新增 **11 个测试函数 / 18 个用例实例**
      （**终态 20 实例**——D.3 补的 `("0,6,18", [6, 12])` × 2 产地）：
      `tests/test_shud_runtime.py` Lane A 7 函数 8 实例（含
      `..._residue_that_will_not_unlink_fails_loud` 的 directory/symlink 参数化）
      + Lane B(a) 2 函数 2 实例（含 cfg-style 门控守门）；
      `tests/test_orchestration_chain.py` Lane B(b) 2 函数 8 实例
      （各 2 配置 × 2 产地）

- [x] C.2 `uv run ruff check .`

      **落账**：`Found 1 error.` —— 唯一一条是**未跟踪的本地工具投影**
      `skills/subagent-workflow/scripts/review_gate.py:369:121` E501，
      与本次改动无关（brief 已点名，不修）。本次改动的文件零告警

- [x] C.3 `openspec validate shud-attempt-csv-and-checkpoint-alignment
      --strict --no-interactive`

      **落账**：`Change 'shud-attempt-csv-and-checkpoint-alignment' is valid`

- [ ] C.4 merge 后 node-27 receipt（C.1 三套件）：**直接在 `umask 022` 下跑**
      ——默认 umask(0002) 会有 ~80 条 #1513 file-provider 预置红淹掉真实回归
      （2026-08-19 实测口径，见 #1513 评论）。记 #1491 与 #1317

## D. Round-1 fix（verifier verD 裁定：4 CONFIRMED，3 条 FIX_NOW）

> 两位独立 reviewer（revD-a 文件 IO/错误处理、revD-b 红证/legacy/spec）各自实测出
> 候选，verD 独立复验并给出最小修复边界。候选 5 的子主张 (b) 被 verD 实测**推翻**
> （「全目录清」变异实为 10 failed 含 A.5 本身红，套件稳健度高于 reviewer 评价），
> 整条 DISCARD，不立 follow-up。

- [x] D.1 **C1（CONFIRMED，P2，FIX_NOW）早探吞掉瞬时失败 → 卫生静默跳过 →
      #1491 的楔死原样复发**。verD 亲验：monkeypatch 使
      `_prepare_forcing_package_context` 第一次抛、之后委托真实实现 →
      `CALLS: 2` + `DIRECT_GRID_STATION_FILENAME_COLLISION ... : forcing.csv`；
      且该码 `transient=False`（`retry.py` 12 个码不含它）→ run_id 永久卡死。
      PR body「已知限制 2」的 justification（「那类 run 本来就在更早位置失败」）
      **被推翻**——只在失败确定性时成立。
      **最小修复边界（verD 已实测）**：
      (a) `runtime.py:1222-1294` 早探失败后**在 model package staging 之前
          原地重试一次再吞**（实测：`tests/test_shud_runtime.py` 260 passed
          零回归，且 C1 探针从 `CALLS:2 + COLLISION` 变成 `DID NOT RAISE`）；
      (b) **最终那次吞异常处必须留一行 log/receipt**（记 `run_id` + 被吞的
          error_code）——这是本条里唯一无争议必须补的，否则运维无法区分
          「卫生跑了但没用」与「卫生被静默跳过」。
      **绑定约束（verD 实测，别踩）**：**不能**让早探直接抛。换成无条件
      `return self._prepare_forcing_package_context(manifest)` 会红 3 条：
      `..._invalid_package_manifest_fails_closed_without_sp_att_rewrite`、
      `..._unreadable_package_manifest_fails_closed_without_sp_att_rewrite`、
      以及 **A.7 冻结 e2e** `..._station_filename_collision_fails_without_overwriting_sp_att`。
      **成本申报**：修法 (a) 会让确定性失败路径把 context 跑 **3 次**
      （现为 2 次），该函数目前只读——需在 PR body 已知限制里更新这个数字

      **落账**：`runtime.py:1222-1294` 改为「早探 → 失败则原地重试一次 →
      再失败才写 receipt 并 `return None`」，两次 `except Exception` 都不外抛
      （绑定约束照守，早探绝不直接抛）。receipt 走本文件既有惯例
      （`_log_recovery_refusal:938` 的 best-effort `_write_text_no_follow`
      写进 `log_dir`，不新引入 logger）：新增
      `_log_attempt_hygiene_probe_skip`，log_dir 按 `execute()` 同一布局
      `Path(config.workspace_root)/"runs"/<run_id>/"logs"` 推导，写
      `attempt_hygiene_probe.err.log` 一行；helper 整体（含会抛 `ValueError` 的
      `_safe_path_component`）包在 `(OSError, ValueError, SHUDRuntimeError,
      SafeFilesystemError)` 里吞掉，保证早探恒不抛。

      零回归：`uv run pytest -q -p no:randomly tests/test_shud_runtime.py`
      → `260 passed in 26.05s`，与基线逐字相同。

      **修后判别力（C1 探针，`-s` 逐字）**：探针 monkeypatch 实例的
      `_prepare_forcing_package_context` 第一次抛 `OSError`、之后委托真实实现，
      在留有前次 attempt 残留的第二次 `prepare_workspace` 上——
      ```
      CALLS: 2
      OUTCOME: DID NOT RAISE
      STATION CSV BYTES RESTAGED: True
      ```
      （verD 修前口径是 `CALLS: 2` + `DIRECT_GRID_STATION_FILENAME_COLLISION
      ... : forcing.csv`，楔死解除）。

      **receipt 逐字**（两次都确定性失败时）：
      ```
      HISTORICAL SITE RAISED: FORCING_PACKAGE_MANIFEST_READ_FAILED
      RECEIPT: direct-grid attempt-start hygiene skipped for run
               fcst_gfs_2026050100_demo_model: FORCING_PACKAGE_MANIFEST_READ_FAILED
      ```
      即历史站点仍原样抛原码，同时留下 run_id + 被吞 error_code 各一
- [x] D.2 **C4（CONFIRMED，Note，并入 D.1 同函数）docstring 与事实相反**：
      `runtime.py:1251-1260` 的「The result is memoized … must not run twice」
      **无任何缓存机制**（只是调用者局部变量复用）。verD 实测三模式：
      happy `{reads:6, verify:1, ctx:1}`、fail_early `{ctx:2}`、
      fail_late `{reads:12, verify:2, ctx:2}`。改成事实描述（成功路径跑一次、
      结果由调用者局部复用非 memo；失败路径该调用连同
      `_verify_forcing_object_checksums` 跑两遍——**修完 D.1 后是三遍，按实际写**）

      **落账**：删掉「The result is memoized …」整句，改写为事实段落
      （`runtime.py:1251-1260`）：成功路径解析 **1 次**、结果由
      `prepare_workspace` 局部变量复用（**非 memo**）；确定性失败路径解析
      **3 次**——早探 + 原地重试 + 历史站点，且
      `_verify_forcing_object_checksums` 每次都重算全部声明成员的哈希。
      同段补上重试存在的理由（`DIRECT_GRID_STATION_FILENAME_COLLISION`
      不在 `TRANSIENT_ERROR_CODES` 里，调度层永不重试）
- [x] D.3 **C2（CONFIRMED，P2，FIX_NOW）B.5 零回归守门对「过严门」判别力为零**：
      verD 把判据换成 `hour != min(checkpoint_hours)`（过严门）后跑**全文件**
      `tests/test_orchestration_chain.py` → **355 passed**，与基线逐字相同。
      根因：两条 aligned 参数的 checkpoint 集合**都是单元素**（`[12]` / `[6]`），
      过严门恒等于真判据；两条 misaligned 参数的 `unreachable_hours` 在两个判据下
      **逐字相同**，连红侧断言也无判别力。真实反例：`"0,6,18"` → hours `[6,12]`
      / step 360、`12*60 % 360 == 0` **完全合法**却会被过严门 typed 拒掉
      （`"0,8"` → `[8,16]`/480 同理）。
      **最小修法**：`tests/test_orchestration_chain.py:11957-11959` 的 aligned
      参数表补一条 `("0,6,18", [6, 12])`（期望值取自真实
      `_forecast_state_checkpoint_hours(72)` 推导，**不要手写**），保留现有
      `update_ic_step_minutes == min(expected_hours) * 60` 断言。一条参数即可
      杀死过严门，**不改被测代码**

      **落账**：`tests/test_orchestration_chain.py:11957-11967` aligned 参数表
      补 `("0,6,18", [6, 12])`（只改参数表 + 表头注释，**零生产代码改动**）。
      期望值由真实函数推导，非手写：
      ```
      $ NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,6,18 uv run python -c \
        "from services.orchestrator.chain_manifest_contracts import \
         _forecast_state_checkpoint_hours as f; print(f(72))"
      allowed=0,6,18 horizon=72 -> [6, 12]
      update_ic_step_minutes = 360
        6*60 % 360 = 0
        12*60 % 360 = 0
      ```
      （builder 的 `_CHECKPOINT_ALIGNMENT_HORIZON_HOURS = 72`，与推导口径一致）。
      新参数 × 2 产地 = **+2 用例实例**：
      `pytest -k keeps_aligned_cycle_configurations_unchanged` → `6 passed`
      （修前 4）。修后判别力见 D.5 的过严门 mutant
- [x] D.4 **C3（CONFIRMED，P2，FIX_NOW）A.8 新增用例是装饰品**：
      `tests/test_shud_runtime.py:3545` 用 `_drop_runtime_forcing_files` 把
      `forcing.files` 删光 → 非 direct-grid 车道 `checksum_entries` 为空 →
      `declared_names` 恒空 → 在 `runtime.py:1329-1330` 就 return，**根本走不到**
      它声称要钉的 guard。verD 实测：删掉 `runtime.py:1326-1327` 的
      `if not forcing_context.is_direct_grid: return` 后 `tests/test_shud_runtime.py`
      **260 passed**（与基线逐字相同）。
      **guard 本身必要且正确**（verD 用真实形状验证：未 drop 的
      `_shud_project_manifest_with_forcing_checksums` 本就声明
      `relative_path: "shud/forcing.csv"`，`DECLARED_NAMES: {'forcing.csv'}` 非空；
      去掉 guard 会让该车道发起本不存在的 unlink，并把 symlink/directory 形残留的
      typed 码从 `WORKSPACE_PATH_UNSAFE` **翻转**成
      `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`）——**不要动生产代码**。
      **最小修法**：该用例去掉 `_drop_runtime_forcing_files(...)` 包裹，
      并加一条真正钉 guard 的断言，二选一：
      (i) spy `runtime_module.unlink_no_follow`，断言第二次 attempt **零调用**；
      (ii) 残留换成 symlink/directory 形态，断言仍是 `WORKSPACE_PATH_UNSAFE`
      （而非 `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`）

      **落账**：取修法 (ii)，**只改测试**（`tests/test_shud_runtime.py:3545-3592`），
      生产代码 guard 一字未动。去掉 `_drop_runtime_forcing_files(...)` 包裹
      —— 这是关键：保留 `forcing.files` 后该车道
      `declared_names == {'forcing.csv'}` 非空，`runtime.py:1329-1330`
      的空集早退不再兜底，唯一挡在删除路径前面的就是 `is_direct_grid` guard。
      原有「覆盖写仍生效」两条断言保留（现在才真正跑到 guard），再追加第三次
      `prepare_workspace`：残留换 symlink 形，断言
      `error_code == "WORKSPACE_PATH_UNSAFE"`（拷贝原语判的），而非
      `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`（本车道不该发起的删除判的）。
      `pytest -k non_direct_grid_second_attempt_behaviour_is_unchanged`
      → `1 passed`。判别力见 D.5 的删 guard mutant
- [x] D.5 修复后复跑 C.1 三套件 + C.2 + C.3，并**逐条给出修后判别力证据**：
      D.1 的 C1 探针不再楔死、D.3 的过严门 mutant 必红、D.4 的删 guard mutant 必红

      **落账 — 三条验证命令（本地 macOS）**：
      - C.1 `uv run pytest -q tests/test_shud_runtime.py tests/test_warm_start.py
        tests/test_orchestration_chain.py` → `649 passed in 954.43s (0:15:54)`，
        exit 0，零 failed / 零 error（基线 647，+2 = D.3 新参数 × 2 产地）。
        随机顺序插件启用（未加 `-p no:randomly`）
      - C.2 `uv run ruff check .` → `Found 1 error.`，仍是那条与本次改动无关的
        未跟踪本地工具投影 `skills/subagent-workflow/scripts/review_gate.py:369`
        E501（不修）；本次改动文件零告警
      - C.3 `openspec validate shud-attempt-csv-and-checkpoint-alignment --strict
        --no-interactive` → `Change 'shud-attempt-csv-and-checkpoint-alignment' is valid`

      **落账 — 三条修后判别力（mutant 一律跑在 `git worktree add` 出来的临时工作树里，
      跑完 `git worktree remove --force`；仓库内零变异残留，`git stash list` 为空）**：
      1. **D.1 / C1 探针**（运行期 monkeypatch，非变异）：早探第一次抛 `OSError`、
         之后委托真实实现 → `CALLS: 2` / `OUTCOME: DID NOT RAISE` /
         `STATION CSV BYTES RESTAGED: True`。修前 verD 口径是同样的 `CALLS: 2`
         但伴随 `DIRECT_GRID_STATION_FILENAME_COLLISION ... : forcing.csv`——楔死解除
      2. **D.3 / 过严门 mutant**（`chain_manifests.py:433` 判据换成
         `[hour for hour in checkpoint_hours if hour != min(checkpoint_hours)]`）：
         全文件 `tests/test_orchestration_chain.py` → **`2 failed, 355 passed in
         866.43s`**（修前该 mutant 是 `355 passed` 零红）。红形逐字：
         ```
         E  services.orchestrator.chain_types.OrchestratorError: Forecast state
            checkpoint hours are not reachable under the derived restart cadence:
            [12] (update_ic_step_minutes=360). ...
         FAILED ...::test_forecast_manifest_assembly_keeps_aligned_cycle_configurations_unchanged[run-..-0,6,18-expected_hours2]
         FAILED ...::test_forecast_manifest_assembly_keeps_aligned_cycle_configurations_unchanged[runtime-..-0,6,18-expected_hours2]
         ```
         即：合法配置 `0,6,18`（step 360、`12*60 % 360 == 0`）被过严门 typed 拒掉，
         两个产地各红一条
      3. **D.4 / 删 guard mutant**（删掉 `runtime.py:1326-1327` 的
         `if not forcing_context.is_direct_grid: return`）：全文件
         `tests/test_shud_runtime.py` → **`1 failed, 259 passed in 26.18s`**
         （修前该 mutant 是 `260 passed` 零红）。红形逐字：
         ```
         >   assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
         E   AssertionError: assert 'DIRECT_GRID_...LEANUP_FAILED' == 'WORKSPACE_PATH_UNSAFE'
         E     - WORKSPACE_PATH_UNSAFE
         E     + DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED
         ```
         即 verD 预测的那次 typed 码翻转被钉住了

      **PR body 待更新**：「已知限制 2」里的「失败路径 context 跑两次」
      → **三次**（早探 + 原地重试 + 历史站点），该调用只读
