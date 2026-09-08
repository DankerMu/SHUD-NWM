# #2130 fixture review

Initial reviewer `a122d31dff149623d`: revise。Fixture level none 与 docs/spec-only 边界正确；MODIFIED requirement 基本完整，但 BLOCKED scenario 的 WHEN 将 pin 错误折入条件，可能误读为仅 pin 错误也 BLOCKED。

修正：恢复权威 WHEN “frontend/API origin 或 receipt path 缺失”；组合错误的优先级只由 requirement 级顺序与该场景 THEN 表达。Pin 场景仍明确以 receipt path 和两个 URL 已提供为前置，并保留缺失、空白、非法三类 FAIL CONFIG_INVALID。

相同 reviewer 聚焦复核返回 `Fixture review: pass`，无 missing axes；确认两个 WHEN 互斥、单错误分类不变、MODIFIED 保留完整 requirement。本记录不是实现 review，不运行测试或 live。
