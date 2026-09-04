import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

/**
 * Static wiring gate for the focused Node/Playwright typecheck. It verifies
 * that check:types exists, is included in the frontend package scripts, AND is
 * invoked by CI; it never recursively invokes pnpm (that is what the CI build
 * job does). Catches Stats field bugs (dev/ino vs st_dev/st_ino) at compile
 * time through the real tsc run, which CI executes.
 */
const repoRoot = path.resolve(__dirname, '../../../..')

describe('river-click Node/Playwright typecheck gate (static wiring)', () => {
  it('exposes check:types as a frontend script', () => {
    const pkg = JSON.parse(readFileSync(path.resolve(__dirname, '../../package.json'), 'utf8'))
    expect(pkg.scripts['check:types']).toBe('tsc -p tsconfig.node-playwright.json')
  })

  it('is wired into the frontend CI job (build && test && check:types)', () => {
    const ci = readFileSync(path.join(repoRoot, '.github/workflows/ci.yml'), 'utf8')
    const start = ci.indexOf('frontend-build:')
    expect(start).toBeGreaterThanOrEqual(0)
    const frontendJob = ci.slice(start, start + 1200)
    expect(frontendJob).toContain('check:types')
    expect(frontendJob).toContain('pnpm build')
    expect(frontendJob).toContain('pnpm test')
    // The install line must run check:types in the same command so a type error
    // blocks the build/test gate.
    expect(frontendJob).toMatch(/pnpm build && pnpm run check:types && pnpm test|pnpm run check:types && pnpm build/)
  })

  it('does not recursively invoke pnpm from within a vitest test', () => {
    const source = readFileSync(path.resolve(__dirname, 'riverClickTypecheck.test.ts'), 'utf8')
    // Static check: the gate must not shell out (CI runs tsc directly). The
    // token is split so this assertion line itself does not self-match.
    expect(source).not.toContain(`${'child'}_process`)
  })

  it('lists the split lane modules and owner helper in the typecheck include', () => {
    const tsconfig = JSON.parse(readFileSync(path.resolve(__dirname, '../../tsconfig.node-playwright.json'), 'utf8'))
    const include = tsconfig.include as string[]
    expect(include).toContain('playwright.river-click-evidence.ts')
    expect(include).toContain('playwright.river-click-evidence-owner.ts')
    expect(include.some((entry) => entry.includes('playwright.river-click-lane'))).toBe(true)
    expect(include).toContain('playwright.river-click-terminal.ts')
    // global-setup is referenced only as a STRING in the config; without an
    // explicit include entry tsc would never typecheck it.
    expect(include).toContain('playwright.live-display.global-setup.ts')
    expect(include).toContain('e2e/live-display.spec.ts')
    expect(include).toContain('src/lib/riverClickEvidence/**/*.ts')
    expect(include).toContain('playwright.config.helpers.ts')
    expect(include).toContain('playwright.live-display.config.ts')
    expect(include).toContain('playwright.live-river-click.config.ts')
    expect(include).toContain('scripts/river-click-receipt-binder-core.d.mts')
    expect(tsconfig.compilerOptions.types).toContain('node')
  })

  it('enumerates every #1970-authored test/fixture path for the manual app-wide tsc diagnostic check', () => {
    // The app-wide `tsc -p tsconfig.app.json` (which also typechecks vitest
    // tests) reports pre-existing diagnostics in unrelated files. This contract
    // keeps the FULL changed-by-#1970 path set enumerated so a reviewer can run
    // the app-wide tsc and filter exactly these paths asserting ZERO
    // diagnostics, independent of unrelated existing errors. Adding a new
    // #1970 test/fixture file without listing it here is a FAIL.
    const paths = [
      'src/__tests__/riverClickLane.test.ts',
      'src/__tests__/riverClickEvidencePublisher.test.ts',
      'src/__tests__/riverClickEvidenceOwner.test.ts',
      'src/__tests__/riverClickPublisherFdLeak.test.ts',
      'src/__tests__/riverClickSchemaNegative.test.ts',
      'src/__tests__/riverClickTypecheck.test.ts',
      'src/__tests__/runbookContract.test.ts',
      'src/__tests__/riverClickReceiptBinderCore.test.ts',
      'src/__tests__/riverClickFakePage.test.ts',
      'src/__tests__/riverClickLiveDisplayContract.test.ts',
      'src/__tests__/riverClickPhase2Closure.test.ts',
      'src/__tests__/riverClickTerminalPublisher.test.ts',
      'src/components/map/__tests__/M11MapLibreSurfaceHook.test.tsx',
      'src/test/maplibreStub.tsx',
      'src/test/riverClickFakePage.ts',
      'src/test/posixFdTable.ts',
      'src/test/riverClickThresholdFixture.ts',
      'src/lib/riverClickEvidence/__tests__/config.test.ts',
      'src/lib/riverClickEvidence/__tests__/hook.test.ts',
      'src/lib/riverClickEvidence/__tests__/preflight.test.ts',
      'src/lib/riverClickEvidence/__tests__/receipt.test.ts',
      'src/lib/riverClickEvidence/__tests__/deadline.test.ts',
      'src/lib/riverClickEvidence/__tests__/requestMatching.test.ts',
    ]
    for (const relative of paths) {
      const full = path.resolve(__dirname, '../../', relative)
      expect(readFileSync(full, 'utf8').length).toBeGreaterThan(0)
    }
    // Every required #1970 test file IS present under src (the guard above
    // would throw ENOENT otherwise); no path in the set may be a duplicate.
    expect(new Set(paths).size).toBe(paths.length)
  })

  it('no production or test file authored by this change exceeds the 1000-line guard', () => {
    const files = [
      'playwright.river-click-evidence.ts',
      'playwright.river-click-evidence-owner.ts',
      'playwright.river-click-lane-preflight.ts',
      'playwright.river-click-lane-attempt.ts',
      'playwright.river-click-lane.ts',
      'playwright.river-click-terminal.ts',
      'playwright.live-display.global-setup.ts',
      'playwright.live-river-click.config.ts',
      'e2e/live-display.spec.ts',
      'src/lib/riverClickEvidence/deadline.ts',
      'src/lib/riverClickEvidence/hook.ts',
      'src/lib/riverClickEvidence/preflight.ts',
      'src/lib/riverClickEvidence/receipt.ts',
      'src/lib/riverClickEvidence/requestMatching.ts',
      'src/lib/riverClickEvidence/config.ts',
      'src/lib/riverClickEvidence/timing.ts',
      'src/__tests__/riverClickLane.test.ts',
      'src/__tests__/riverClickEvidencePublisher.test.ts',
      'src/__tests__/riverClickEvidenceOwner.test.ts',
      'src/__tests__/riverClickPublisherFdLeak.test.ts',
      'src/__tests__/riverClickSchemaNegative.test.ts',
      'src/__tests__/riverClickTypecheck.test.ts',
      'src/__tests__/runbookContract.test.ts',
      'src/__tests__/riverClickLiveDisplayContract.test.ts',
      'src/__tests__/riverClickPhase2Closure.test.ts',
      'src/__tests__/riverClickTerminalPublisher.test.ts',
      'src/components/map/__tests__/M11MapLibreSurfaceHook.test.tsx',
      'src/test/maplibreStub.tsx',
      'src/test/riverClickFakePage.ts',
      'src/test/posixFdTable.ts',
      'src/test/riverClickThresholdFixture.ts',
      'src/lib/riverClickEvidence/__tests__/hook.test.ts',
      'src/lib/riverClickEvidence/__tests__/preflight.test.ts',
      'src/lib/riverClickEvidence/__tests__/receipt.test.ts',
      'scripts/river-click-receipt-binder.mjs',
      'scripts/river-click-receipt-binder-core.mjs',
      'scripts/river-click-receipt-binder-core.d.mts',
      'src/__tests__/riverClickReceiptBinderCore.test.ts',
      'src/__tests__/riverClickFakePage.test.ts',
      'src/lib/riverClickEvidence/__tests__/deadline.test.ts',
      'src/lib/riverClickEvidence/__tests__/requestMatching.test.ts',
    ]
    for (const relative of files) {
      const full = path.resolve(__dirname, '../../', relative)
      // A required file that vanished from the guard set is a FAIL, never a
      // silent skip: the guard must stay complete or the line-count audit
      // loses its teeth.
      const content = readFileSync(full, 'utf8')
      const lines = content.split('\n').length
      expect(lines, `${relative} is ${lines} lines`).toBeLessThanOrEqual(1000)
    }
  })
})
