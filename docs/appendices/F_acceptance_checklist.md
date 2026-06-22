# 附录 F. 总体验收清单

版本：v0.2  
日期：2026-05-06

## 1. 系统级验收

- [ ] 系统名称在前端、API 文档和部署文档中统一为“全国水文模拟系统”。
- [ ] GFS、IFS、ERA5、CLDAS 均在 data_source 表中登记，CLDAS 可标记为 restricted。
- [ ] GFS 与 IFS 结果以不同 scenario 保存和展示。
- [ ] 前端时间轴由图层 `valid_times[]` 驱动。
- [ ] SHUD run 全部通过 Slurm 提交。
- [ ] Forecast run 使用 analysis StateSnapshot warm-start。
- [ ] 河段点击可展示过去 7 天 analysis 与未来 7 天 forecast。
- [ ] 洪水重现期产品可在地图上展示。
- [ ] 流域版本变化后，历史结果仍可按旧版本查询。

## 2. 数据与血缘验收

- [ ] 每个曲线点可追溯到 run_id。
- [ ] 每个 run_id 可追溯到 forcing_version_id。
- [ ] 每个 forcing_version 可追溯到 source/cycle/canonical product。
- [ ] 每个重现期结果可追溯到 flood_frequency_curve。
- [ ] 对象存储文件和数据库 URI 一致。

## 3. 运行与可靠性验收

- [ ] 单流域失败不影响其它流域完成发布。
- [ ] 下载、forcing、SHUD、解析、频率计算均可单独重跑。
- [ ] 作业日志可按 run_id 查询。
- [ ] 产品未通过 QC 不会出现在 viewer 用户前端。
- [ ] Slurm 作业状态与系统状态机一致。

## 4. 前端验收

- [ ] 支持地形、影像、矢量三种底图。
- [ ] 支持气象代站图层。
- [ ] 支持河段径流、水位、重现期图层。
- [ ] 支持 GFS/IFS 曲线对比。
- [ ] 曲线显示 Q2/Q5/Q10/Q20/Q50/Q100 阈值线。
- [ ] 当前资料源、起报时间、有效时间、analysis/forecast 分界线清楚可见。
