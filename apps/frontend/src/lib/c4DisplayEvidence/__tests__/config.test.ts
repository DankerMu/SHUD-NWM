import { describe, expect, it } from 'vitest'

import {
  buildC4OpsHref,
  c4RejectedOverrideKeys,
  c4RejectedRoleKeys,
  parseC4DisplayConfig,
  type C4DisplayConfig,
  type C4ConfigParse,
} from '../config'

const VALID = {
  PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
  PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
  PLAYWRIGHT_LIVE_C4_BASIN_ID: 'basins_qhh',
  PLAYWRIGHT_LIVE_C4_SEGMENT_ID: 'basins_qhh_shud_reach_000001',
  PLAYWRIGHT_LIVE_C4_RECEIPT_PATH: '/private/evidence/nhms-frontend-c4-live-evidence-20260904T000000Z.json',
}

function parse(overrides: Partial<typeof VALID> = {}): C4ConfigParse {
  return parseC4DisplayConfig({ ...VALID, ...overrides })
}

function expectOk(parsed: C4ConfigParse): C4DisplayConfig {
  if (!parsed.ok) throw new Error(`fixture config must parse: ${parsed.message}`)
  return parsed.config
}

function expectFailure(parsed: C4ConfigParse): Extract<C4ConfigParse, { ok: false }> {
  if (parsed.ok) throw new Error('fixture config must fail to parse')
  return parsed
}

function failParse(overrides: Partial<typeof VALID>): Extract<C4ConfigParse, { ok: false }> {
  return expectFailure(parse(overrides))
}

describe('C4 live config parser', () => {
  it('accepts the full exact five-value configuration and normalizes URL origins', () => {
    const config = expectOk(parse())
    expect(config.frontendOrigin).toBe('https://display.example.test')
    expect(config.apiOrigin).toBe('https://api.example.test')
    expect(config.basinId).toBe('basins_qhh')
    expect(config.segmentId).toBe('basins_qhh_shud_reach_000001')
    expect(config.receiptPath).toBe('/private/evidence/nhms-frontend-c4-live-evidence-20260904T000000Z.json')
  })

  it('classifies absent URL prerequisites as BLOCKED and absent pins as FAIL before browser work', () => {
    for (const key of ['PLAYWRIGHT_LIVE_BASE_URL', 'PLAYWRIGHT_LIVE_API_BASE_URL'] as const) {
      const env = { ...VALID }
      delete env[key]
      const parsed = parseC4DisplayConfig(env)
      expect(parsed.ok, key).toBe(false)
      if (!parsed.ok) {
        expect(parsed.classification).toBe('BLOCKED')
        expect(parsed.code).toBe('REQUIRED_ENV_MISSING')
      }
    }
    for (const key of ['PLAYWRIGHT_LIVE_C4_BASIN_ID', 'PLAYWRIGHT_LIVE_C4_SEGMENT_ID'] as const) {
      const env = { ...VALID }
      delete env[key]
      const parsed = parseC4DisplayConfig(env)
      expect(parsed.ok, key).toBe(false)
      if (!parsed.ok) {
        expect(parsed.classification).toBe('FAIL')
        expect(parsed.code).toBe('CONFIG_INVALID')
      }
    }
  })

  it('rejects every supplied river run/model/version/cycle/scenario override even when empty', () => {
    for (const key of c4RejectedOverrideKeys) {
      expect(parseC4DisplayConfig({ ...VALID, [key]: 'anything' }).ok).toBe(false)
      expect(parseC4DisplayConfig({ ...VALID, [key]: '' }).ok).toBe(false)
    }
  })

  it('rejects VITE_AUTH_ROLE and VITE_ENABLE_ROLE_OVERRIDE even when empty', () => {
    for (const key of c4RejectedRoleKeys) {
      expect(parseC4DisplayConfig({ ...VALID, [key]: 'operator' }).ok).toBe(false)
      expect(parseC4DisplayConfig({ ...VALID, [key]: '' }).ok).toBe(false)
    }
  })

  it('requires bare http(s) origins and the C4 receipt basename grammar', () => {
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'https://user:pass@display.example.test' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test/app' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_C4_RECEIPT_PATH: '/tmp/other-name.json' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_C4_RECEIPT_PATH: '/tmp/nhms-frontend-river-click-live-evidence-1.json' }).code).toBe('CONFIG_INVALID')
  })

  it('builds the strict /ops href with cycle_time rather than the loose cycle param', () => {
    const href = buildC4OpsHref({
      sourceId: 'GFS',
      cycleTime: '2026-09-04T00:00:00.000Z',
      runId: 'run-gfs',
      modelId: 'model-gfs',
    })
    expect(href).toBe('/ops?source=GFS&cycle_time=2026-09-04T00%3A00%3A00.000Z&run_id=run-gfs&model_id=model-gfs')
    expect(href).not.toContain('cycle=')
  })
})
