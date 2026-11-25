# analysis-forecast-curve-splicing

## REMOVED Requirements

### Requirement: Frontend Curve Splicing Display

**Reason**: 河段曲线窗请求 forecast-series 时带 `include_analysis=false`，非预报 scenario 的 series 会校验失败；前端不绘制 analysis 段、分界线或资料来源标注。

**Migration**: 无；前端拼接展示已放弃。曲线的渲染见 `multi-source-comparison-ui` "GFS 与 IFS 预报曲线同图渲染"。

### Requirement: Issue Time Display

**Reason**: 它规定的是同一条起报分界线及其标注，前端已不绘制。

**Migration**: 无；起报时次的选择见 `map-feature-popups` 中关于起报时次选择器的各条 requirement。
