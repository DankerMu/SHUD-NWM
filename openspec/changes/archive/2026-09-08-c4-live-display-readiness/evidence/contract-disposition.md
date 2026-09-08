# C4-CONTRACT-1 — responsibility attribution

Independent verifier `af623916be0fdf7d3`: CONFIRMED / FIX_NOW。对比对象为 master `e6b5e4ff` 上 staged C4 与 readiness 保存点 `2f7e95b4`；该 master SHA 不包含受审实现，不能记作 reviewed implementation SHA。

缺陷：新拆分 fixture/issue 把 exact SHA/digest 校验错误地归到 C4 CLI。既有 C4 CLI/G7 命令只有 receipt、两 origin、basin、segment、cmd-start/end；闭集 receipt 无 head_sha。新增参数或 receipt 字段会改变既有接受协议，而不是修复上游漏门。

已按裁决同步 design/spec/tasks/issue-body/README：C4 binder 保留五输入、秒级 bracket、POSIX facts/TOCTOU；交付/G0 强制 frozen SHA，#1895 C3 publisher/binder 强制 C4 sha256 与 reviewed-SHA，`C3_BIND_C4_TAMPER` 拒绝字节摘要变化。端到端验收未删，明确不以 C4 CLI PASS 代替外层门。生产实现未改。

Verifier 核对源为 readiness 保存点的 G7 命令、`packages/common/node27_issue1895_publication_current.py` `_read_c4_receipt` / `bind_c3_receipt`，及当前 C4 binder/schema。该外层运行证据仍由 #1895 承担，不当作本切片 live PASS。

附带 verification 命令纠正：frontend 根 tsconfig 的 files 为空，裸 `tsc --noEmit` 不覆盖引用项目；使用 CI 的 `corepack pnpm typecheck`（tsconfig.app.json），并保留 tooling typecheck。不是弱化检查。

主流程目标 strict validation PASS。GitHub body 同步与最终 Phase 2 结果另记录，不把本次 spec 修正当完整运行验证。
