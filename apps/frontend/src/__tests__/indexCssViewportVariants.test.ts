import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { MOBILE_FORM_QUERY, MOBILE_LANDSCAPE_QUERY } from '@/hooks/useMobileForm'

const indexCss = readFileSync(path.resolve(__dirname, '../index.css'), 'utf8')

/**
 * 取出 `@custom-variant <name> { ... }` 块写法的块体（花括号配对）。
 * 单行简写 `@custom-variant <name> (...);` 没有块体，返回 null——简写遇到逗号分隔的查询列表
 * 会静默丢掉第二条（design.md D1），所以这里只认块写法。
 */
function customVariantBlock(css: string, name: string): string | null {
  const head = new RegExp(`@custom-variant\\s+${name}\\s*\\{`).exec(css)
  if (!head) return null
  const start = head.index + head[0].length
  let depth = 1
  for (let index = start; index < css.length; index += 1) {
    if (css[index] === '{') depth += 1
    if (css[index] === '}') depth -= 1
    if (depth === 0) return css.slice(start, index)
  }
  return null
}

describe('index.css viewport-form custom variants', () => {
  it.each([
    { name: 'mobile', query: MOBILE_FORM_QUERY },
    { name: 'mobile-landscape', query: MOBILE_LANDSCAPE_QUERY },
  ])('declares `$name` in block syntax with the exported query string and @slot', ({ name, query }) => {
    const block = customVariantBlock(indexCss, name)

    expect(block, `@custom-variant ${name} { ... } block not found in src/index.css`).not.toBeNull()
    expect(block).toContain(`@media ${query}`)
    expect(block).toMatch(/@slot\s*;/)
  })

  it('declares each variant exactly once and never in single-line shorthand', () => {
    expect(indexCss.match(/@custom-variant\s+mobile\s*[({]/g)).toHaveLength(1)
    expect(indexCss.match(/@custom-variant\s+mobile-landscape\s*[({]/g)).toHaveLength(1)
    expect(indexCss).not.toMatch(/@custom-variant\s+mobile(-landscape)?\s*\(/)
  })
})
