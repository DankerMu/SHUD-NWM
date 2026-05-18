import { describe, expect, it } from 'vitest'

import { parsePlaywrightWorkers } from '../../playwright.config'

describe('Playwright config helpers', () => {
  it('uses bounded deterministic worker counts', () => {
    expect(parsePlaywrightWorkers(undefined)).toBe(1)
    expect(parsePlaywrightWorkers('3')).toBe(3)
    expect(parsePlaywrightWorkers('999')).toBe(4)
  })

  it('fails clearly for invalid worker counts', () => {
    expect(() => parsePlaywrightWorkers('0')).toThrow('PLAYWRIGHT_WORKERS must be a positive integer.')
    expect(() => parsePlaywrightWorkers('abc')).toThrow('PLAYWRIGHT_WORKERS must be a positive integer.')
  })
})
