/**
 * 单一来源：API 时间实例的秒精度拼写归一（spec design.md D10）。
 *
 * 后端对外只有一种拼写 `YYYY-MM-DDTHH:MM:SSZ`（秒精度、无小数秒、字面 Z），而前端
 * `parseM11QueryState` / `normalizeIsoString` 产出的是 JS 毫秒形 `…T12:00:00.000Z`。
 * 两种拼写混用会在四个地方静默出错，所以这四处必须共用本函数：
 * 1. `{cycle}` / `{valid_time}` 占位符替换（拼出后端不认识的毫秒段 → 404）
 * 2. `valid_times[]` 成员判定（`.000Z` 永远匹配不上 `…:00Z`）
 * 3. store 里 `(source, cycle)` 缓存键（同一周期两种拼写 = 两份缓存 + 多一次请求）
 * 4. 活动 `(source, cycle)` 与 `(metadata.default_source, metadata.default_cycle)` 的相等判定
 *    （误判为非默认周期会多发一次 `/valid-times`）
 *
 * 亚秒部分按后端口径截断（后端从不产出小数秒）。无法解析则返回 null，由调用方 fail-closed。
 */
export function toSecondsPrecisionInstant(value: string | null | undefined): string | null {
  if (!value) return null
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return null
  return `${new Date(timestamp).toISOString().slice(0, 19)}Z`
}
