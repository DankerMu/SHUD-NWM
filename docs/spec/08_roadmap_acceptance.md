# 08. 开发路线图与验收标准

版本：v0.2  
日期：2026-05-06

## 阶段 0：项目初始化

交付 Git 仓库、开发规范、CI、配置管理、基础数据库、对象存储目录和最小 API 框架。验收要求：开发者可本地启动 API 和数据库，CI 可运行 lint/test，数据库 migration 可重复执行。

## 阶段 1：GFS + 单/双流域 Forecast 闭环

交付 GFS adapter、canonical converter、forcing producer、model registry、Slurm forecast job、output parser、river timeseries API、前端河网点击曲线。验收要求：至少一个流域、一个 GFS 周期完成未来 7 天预报，`.rivqdown` 转换为 `m3/s` 入库，前端点击河段可展示曲线。

## 阶段 2：Analysis run 与 warm-start

交付 ERA5 adapter、analysis run pipeline、StateSnapshot 管理、forecast 使用 init_state_id、前端过去 7 天 + 未来 7 天拼接。验收要求：Analysis run 能生成状态快照，forecast run 使用最近状态快照启动，曲线明确标注 analysis/forecast 分界线。

## 阶段 3：Slurm 全国化

交付 resource profile、Slurm job array 模板、作业依赖状态机、失败重试、partial success 支持、运行监控看板。验收要求：至少 10 个流域模型可并行提交，单流域失败不阻断其它流域入库和发布。

## 阶段 4：IFS 与多 scenario

交付 IFS adapter、scenario metadata、多 scenario river_timeseries 查询、前端 GFS/IFS 对比曲线。验收要求：同一河段同一起报时刻可展示 GFS 和 IFS 两条曲线；IFS 06/18 周期不足 7 天时，前端明确显示可用时效。

## 阶段 5：洪水频率 / 重现期产品

交付历史样本生产任务、P-III/GEV 或配置化方法、Q2/Q5/Q10/Q20/Q50/Q100 阈值表、ReturnPeriodResult 入库、前端重现期河段配色。验收要求：每个已启用河段有频率曲线或明确缺失原因，预报 run 完成后自动计算重现期。

## 阶段 6：CLDAS restricted → enabled

在权限解决后启用 CLDAS 近实时真实场。交付 CLDAS adapter、权限配置与下载策略、CLDAS QC、best_available 规则更新。验收要求：CLDAS 资料可参与 analysis run，best_available 产品能显示每个时刻的实际来源。

## 最终验收指标

| 类别 | 指标 |
|---|---|
| 功能 | 支持 GFS/IFS 分 scenario 预报。 |
| 功能 | 支持 analysis/forecast 拼接展示。 |
| 功能 | 支持洪水重现期产品。 |
| 功能 | 支持流域版本变化和历史版本查询。 |
| 性能 | 全国河网图层通过瓦片展示，不直接加载全量 GeoJSON。 |
| 可靠性 | 单个流域失败不影响其它流域发布。 |
| 可追溯 | 任意前端曲线点可追溯到 run_id、forcing_version、source cycle。 |
| 运维 | 作业日志、错误码、重试次数可查。 |
