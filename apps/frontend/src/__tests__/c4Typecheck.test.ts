import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const repoRoot = path.resolve(__dirname, '../../../..')

describe('C4 Node/Playwright typecheck gate (static wiring)', () => {
  it('lists the C4 lane modules in the typecheck include', () => {
    const tsconfig = JSON.parse(readFileSync(path.resolve(__dirname, '../../tsconfig.node-playwright.json'), 'utf8'))
    const include = tsconfig.include as string[]
    expect(include).toContain('playwright.private-receipt-publication.ts')
    expect(include).toContain('playwright.c4-display-evidence.ts')
    expect(include).toContain('playwright.c4-display-evidence-owner.ts')
    expect(include).toContain('playwright.c4-display-lane.ts')
    expect(include).toContain('playwright.c4-display-lane-preflight.ts')
    expect(include).toContain('playwright.c4-display-terminal.ts')
    expect(include).toContain('playwright.live-c4-display.config.ts')
    expect(include).toContain('playwright.live-c4-display.global-setup.ts')
    expect(include).toContain('e2e/live-c4-display.spec.ts')
    expect(include).toContain('src/lib/c4DisplayEvidence/**/*.ts')
    expect(include).toContain('scripts/c4-receipt-binder-core.d.mts')
  })

  it('enumerates every C4-authored test/fixture path for the app-wide tsc diagnostic check', () => {
    const paths = [
      'src/__tests__/c4DisplayLane.test.tsx',
      'src/__tests__/c4DisplayPublisher.test.ts',
      'src/__tests__/c4DisplayOwner.test.ts',
      'src/__tests__/c4LiveDisplayContract.test.ts',
      'src/__tests__/c4ReceiptBinderCore.test.ts',
      'src/__tests__/c4SchemaNegative.test.ts',
      'src/__tests__/c4Typecheck.test.ts',
      'src/test/c4DisplayFakePage.ts',
      'src/lib/c4DisplayEvidence/__tests__/config.test.ts',
      'src/lib/c4DisplayEvidence/__tests__/receipt.test.ts',
    ]
    for (const relative of paths) {
      const full = path.resolve(__dirname, '../../', relative)
      expect(readFileSync(full, 'utf8').length).toBeGreaterThan(0)
    }
    expect(new Set(paths).size).toBe(paths.length)
  })

  it('no C4 production or test file exceeds the 1000-line guard', () => {
    const files = [
      'playwright.private-receipt-publication.ts',
      'playwright.c4-display-evidence.ts',
      'playwright.c4-display-evidence-owner.ts',
      'playwright.c4-display-lane.ts',
      'playwright.c4-display-lane-preflight.ts',
      'playwright.c4-display-terminal.ts',
      'playwright.live-c4-display.config.ts',
      'playwright.live-c4-display.global-setup.ts',
      'playwright.river-click-evidence.ts',
      'e2e/live-c4-display.spec.ts',
      'src/lib/c4DisplayEvidence/constants.ts',
      'src/lib/c4DisplayEvidence/config.ts',
      'src/lib/c4DisplayEvidence/receipt.ts',
      'src/lib/c4DisplayEvidence/dom.ts',
      'src/lib/c4DisplayEvidence/requestMatching.ts',
      'src/test/c4DisplayFakePage.ts',
      'src/__tests__/c4DisplayLane.test.tsx',
      'src/__tests__/c4DisplayPublisher.test.ts',
      'src/__tests__/c4DisplayOwner.test.ts',
      'src/__tests__/c4LiveDisplayContract.test.ts',
      'src/__tests__/c4ReceiptBinderCore.test.ts',
      'src/__tests__/c4SchemaNegative.test.ts',
      'src/__tests__/c4Typecheck.test.ts',
      'src/lib/c4DisplayEvidence/__tests__/config.test.ts',
      'src/lib/c4DisplayEvidence/__tests__/receipt.test.ts',
      'scripts/c4-receipt-binder.mjs',
      'scripts/c4-receipt-binder-core.mjs',
      'scripts/c4-receipt-binder-core.d.mts',
      'scripts/playwright-live-c4-display.sh',
    ]
    for (const relative of files) {
      const full = path.resolve(__dirname, '../../', relative)
      const content = readFileSync(full, 'utf8')
      const lines = content.split('\n').length
      expect(lines, `${relative} is ${lines} lines`).toBeLessThanOrEqual(1000)
    }
  })

  it('runs C4 AJV negatives when only the C4 evidence schema changes', () => {
    const ci = readFileSync(path.join(repoRoot, '.github/workflows/ci.yml'), 'utf8')
    const frontend = ci.slice(ci.indexOf('            frontend:\n'), ci.indexOf('            docs:\n'))
    expect(frontend).toContain("'schemas/frontend_c4_live_evidence.schema.json'")
    expect(frontend).toContain("'schemas/examples/frontend_c4_live_evidence*.json'")
    expect(frontend).not.toContain("'schemas/**'")
  })

  it('does not add a runtime dependency to the production bundle', () => {
    const pkg = JSON.parse(readFileSync(path.resolve(__dirname, '../../package.json'), 'utf8'))
    expect(pkg.dependencies.ajv).toBeUndefined()
    expect(Object.keys(pkg.dependencies)).not.toContain('playwright')
    const ci = readFileSync(path.join(repoRoot, '.github/workflows/ci.yml'), 'utf8')
    expect(ci).toContain('check:types')
  })
})
