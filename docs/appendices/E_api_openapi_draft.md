# 附录 E. OpenAPI 草案片段

版本：v0.2  
日期：2026-05-06

说明：本附录保留 2026-05-06 的 v0.2 历史草案片段。片段中的
`/api/v1/river-segments/{segment_id}/forecast-series` 是历史 shorthand 示例；
当前 canonical active runtime route 是
`/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`。
这不是运行时 deprecation 标记，也不表示有 shorthand endpoint 被发布或移除。

```yaml
openapi: 3.0.3
info:
  title: 全国水文模拟系统 API
  version: 1.0.0
paths:
  # Historical v0.2 draft snippet. Use the canonical active route:
  # /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series
  /api/v1/basins:
    get:
      summary: 查询流域列表
      responses:
        '200':
          description: OK
  /api/v1/layers/{layer_id}/valid-times:
    get:
      summary: 查询图层有效时间列表
      parameters:
        - name: layer_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: OK
  /api/v1/river-segments/{segment_id}/forecast-series:
    get:
      summary: 查询河段 analysis + forecast 曲线
      parameters:
        - name: segment_id
          in: path
          required: true
          schema:
            type: string
        - name: issue_time
          in: query
          schema:
            type: string
            example: latest
        - name: scenarios
          in: query
          schema:
            type: string
            example: GFS,IFS
      responses:
        '200':
          description: OK
```
