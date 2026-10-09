import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * 移动 spec 的 oracle 纪律（openspec mobile-responsive-display task 5.1，
 * specs/mobile-regression-evidence「No pixel or document-width oracles」）：`e2e/` 下文件名含 `.mobile.`
 * 的 spec 不做截图比对，也不读文档滚动宽度（外壳 `overflow-hidden`，它恒等于窗口宽，是个恒真判据）。
 * 读某个元素自己的 `scrollWidth`（具体滚动容器对 `clientWidth`）不在此列。
 */
const e2eDir = path.resolve(__dirname, '../../e2e')
/** 实测：本检查落地时共 20 个移动 spec。少于这个数说明目录或命名规则变了，检查在空转。 */
const MIN_MOBILE_SPECS = 20

const FORBIDDEN: Array<{ name: string; pattern: RegExp }> = [
  { name: '截图比对断言 toHaveScreenshot', pattern: /toHaveScreenshot/ },
  { name: '截图比对断言 toMatchSnapshot', pattern: /toMatchSnapshot/ },
  { name: '文档滚动宽度 documentElement.scrollWidth', pattern: /documentElement\s*\??\.\s*scrollWidth/ },
  { name: '文档滚动宽度 body.scrollWidth', pattern: /\bbody\s*\??\.\s*scrollWidth/ },
  { name: '文档滚动宽度 scrollingElement.scrollWidth', pattern: /scrollingElement\s*\??\.\s*scrollWidth/ },
]

const mobileSpecs = readdirSync(e2eDir)
  .filter((file) => file.includes('.mobile.') && file.endsWith('.spec.ts'))
  .sort()

describe('mobile e2e specs use no pixel or document-width oracles', () => {
  it('scans every `.mobile.` spec under e2e/', () => {
    expect(mobileSpecs.length).toBeGreaterThanOrEqual(MIN_MOBILE_SPECS)
    expect(mobileSpecs).toContain('m11-touch-audit.mobile.mocked.spec.ts')
  })

  it('finds no screenshot comparison and no document scroll-width read', () => {
    const violations = mobileSpecs.flatMap((file) => {
      const source = readFileSync(path.join(e2eDir, file), 'utf8')
      return FORBIDDEN.filter(({ pattern }) => pattern.test(source)).map(({ name }) => `${file}: ${name}`)
    })

    expect(violations).toEqual([])
  })
})
