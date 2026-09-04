import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import Ajv2020 from 'ajv/dist/2020'

import {
  RIVER_CLICK_FAIL_CODES,
  RIVER_CLICK_BLOCKED_CODES,
  RIVER_CLICK_THRESHOLD_MS,
} from '../lib/riverClickEvidence/constants'

/**
 * Negative branch discrimination for the Draft-2020-12 river-click evidence
 * schema, run in frontend CI with the dev-only AJV validator (no runtime
 * dependency). The same schema is also shipped to backend CI via
 * check-jsonschema; these tests MUST NOT skip when that CLI is unavailable.
 *
 * One valid baseline per status shape (PASS / BLOCKED / ordinary FAIL /
 * THRESHOLD_EXCEEDED FAIL) is built ONCE and schema-validated first. Every
 * negative case clones exactly one baseline and mutates exactly ONE
 * load-bearing field; a test that changes two fields would be false-red when
 * either condition disappears, so it is forbidden here. Cross-field relations
 * that Draft 2020-12 cannot express (requested==rendered equality, consecutive
 * indices, canonical cycle equality) are enforced by the semantic validator
 * (validateRiverClickEvidenceDocument) and asserted in receipt.test.ts.
 */
const repoRoot = path.resolve(__dirname, '../../../../')
const schemaPath = path.join(repoRoot, 'schemas/frontend_river_click_live_evidence.schema.json')
const passExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json'), 'utf8'),
)
const blockedExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.partial.example.json'), 'utf8'),
)
const failExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.error.example.json'), 'utf8'),
)

const schema = JSON.parse(readFileSync(schemaPath, 'utf8'))
const ajv = new Ajv2020({ strict: false, allErrors: true })
const validate = ajv.compile(schema)

/** Deep clone of a baseline so one mutation can never leak into another case. */
function cloneBaseline(document: unknown): Record<string, unknown> {
  return JSON.parse(JSON.stringify(document)) as Record<string, unknown>
}

function emptyFailure(): Record<string, unknown> {
  return { code: 'INTERNAL_ERROR', stage: 'sample', sample_index: null, gfs_status: null, ifs_status: null, message: 'x' }
}

function expectValid(document: unknown) {
  const ok = validate(document)
  expect(validate.errors ?? [], 'expected schema acceptance').toEqual([])
  expect(ok).toBe(true)
}

function expectInvalid(document: unknown, mutation: string, baselineName: string) {
  const ok = validate(document)
  expect(ok, `expected schema rejection for ${baselineName} + ${mutation}`).toBe(false)
}

describe('river-click evidence schema branch discrimination (AJV Draft-2020-12)', () => {
  // One validated baseline per status shape; every negative case below clones
  // exactly one of these and changes EXACTLY one field.
  const PASS_BASE = cloneBaseline(passExample)
  const BLOCKED_BASE = cloneBaseline(blockedExample)
  const FAIL_BASE = cloneBaseline(failExample)
  const THRESHOLD_BASE = cloneBaseline(passExample)
  THRESHOLD_BASE.status = 'FAIL'
  THRESHOLD_BASE.failure = {
    code: 'THRESHOLD_EXCEEDED',
    stage: 'threshold',
    sample_index: null,
    gfs_status: null,
    ifs_status: null,
    message: 'x',
  }
  THRESHOLD_BASE.p95_ms = RIVER_CLICK_THRESHOLD_MS

  it('accepts the three checked-in examples and the built threshold baseline', () => {
    expectValid(PASS_BASE)
    expectValid(BLOCKED_BASE)
    expectValid(FAIL_BASE)
    expectValid(THRESHOLD_BASE)
  })

  describe('PASS single-field negatives', () => {
    it('rejects accepted_count 19 (PASS requires exactly 20)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.accepted_count = 19
      expectInvalid(doc, 'accepted_count 19', 'PASS')
    })

    it('rejects p95_ms equal to the threshold (must be strictly below)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.p95_ms = RIVER_CLICK_THRESHOLD_MS
      expectInvalid(doc, 'p95_ms == threshold', 'PASS')
    })

    it('rejects a non-null failure', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.failure = emptyFailure()
      expectInvalid(doc, 'non-null failure', 'PASS')
    })

    it('rejects a 21st sample (maxItems 20, not 64)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.samples = [...(doc.samples as unknown[]), { index: 21, duration_ms: 21, gfs_status: 200, ifs_status: 200 }]
      expectInvalid(doc, '21 samples', 'PASS')
    })

    it('rejects 19 samples with accepted_count unchanged (minItems 20)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.samples = (doc.samples as unknown[]).slice(0, 19)
      expectInvalid(doc, '19 samples', 'PASS')
    })

    it('rejects gfs.source_id IFS (const GFS)', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.gfs as Record<string, unknown>).source_id = 'IFS'
      expectInvalid(doc, 'gfs source_id IFS', 'PASS')
    })

    it('rejects ifs.source_id GFS (const IFS)', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.ifs as Record<string, unknown>).source_id = 'GFS'
      expectInvalid(doc, 'ifs source_id GFS', 'PASS')
    })

    it('rejects gfs.scenario set to the IFS scenario (const GFS scenario)', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.gfs as Record<string, unknown>).scenario = 'forecast_ifs_deterministic'
      expectInvalid(doc, 'gfs scenario IFS', 'PASS')
    })

    it('rejects warmup_count 0 while the warmup object is present (const 1)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.warmup_count = 0
      expectInvalid(doc, 'warmup_count 0', 'PASS')
    })

    it('rejects null rendered_feature (PASS requires non-null)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.rendered_feature = null
      expectInvalid(doc, 'null rendered_feature', 'PASS')
    })

    it('rejects null gfs (PASS requires non-null)', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.gfs = null
      expectInvalid(doc, 'null gfs', 'PASS')
    })
  })

  describe('BLOCKED single-field negatives', () => {
    it('rejects accepted_count 1 (must be 0)', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      doc.accepted_count = 1
      expectInvalid(doc, 'accepted_count 1', 'BLOCKED')
    })

    it('rejects any sample claim (maxItems 0)', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      doc.samples = [{ index: 1, duration_ms: 1, gfs_status: 200, ifs_status: 200 }]
      expectInvalid(doc, 'one sample', 'BLOCKED')
    })

    it('rejects a non-null origins.frontend (origin constraint is load-bearing)', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      ;(doc.origins as Record<string, unknown>).frontend = 'https://display.example.test'
      expectInvalid(doc, 'origins.frontend non-null', 'BLOCKED')
    })

    it('rejects a FAIL failure code (BLOCKED closed set)', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      ;(doc.failure as Record<string, unknown>).code = 'INTERNAL_ERROR'
      expectInvalid(doc, 'failure code INTERNAL_ERROR', 'BLOCKED')
    })

    it('rejects a failure carrying a sample_index', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      ;(doc.failure as Record<string, unknown>).sample_index = 1
      expectInvalid(doc, 'failure sample_index 1', 'BLOCKED')
    })

    it('rejects a non-null requested_feature', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      doc.requested_feature = PASS_BASE.requested_feature
      expectInvalid(doc, 'non-null requested_feature', 'BLOCKED')
    })

    it('rejects a non-null gfs', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      doc.gfs = PASS_BASE.gfs
      expectInvalid(doc, 'non-null gfs', 'BLOCKED')
    })

    it('rejects failure null (BLOCKED requires a failure)', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      doc.failure = null
      expectInvalid(doc, 'null failure', 'BLOCKED')
    })
  })

  describe('ordinary FAIL single-field negatives', () => {
    it('rejects failure null', () => {
      const doc = cloneBaseline(FAIL_BASE)
      doc.failure = null
      expectInvalid(doc, 'null failure', 'FAIL')
    })

    it('rejects a BLOCKED failure code', () => {
      const doc = cloneBaseline(FAIL_BASE)
      ;(doc.failure as Record<string, unknown>).code = 'REQUIRED_ENV_MISSING'
      expectInvalid(doc, 'failure code REQUIRED_ENV_MISSING', 'FAIL')
    })

    it('rejects a failure code outside the closed sets', () => {
      const doc = cloneBaseline(FAIL_BASE)
      ;(doc.failure as Record<string, unknown>).code = 'NOT_A_CODE'
      expectInvalid(doc, 'failure code NOT_A_CODE', 'FAIL')
    })

    it('rejects an unknown status', () => {
      const doc = cloneBaseline(FAIL_BASE)
      doc.status = 'NOPE'
      expectInvalid(doc, 'status NOPE', 'FAIL')
    })

    it('rejects an empty failure message', () => {
      const doc = cloneBaseline(FAIL_BASE)
      ;(doc.failure as Record<string, unknown>).message = ''
      expectInvalid(doc, 'empty failure message', 'FAIL')
    })

    it('rejects p95_ms non-null on a non-THRESHOLD_FAIL (must stay null)', () => {
      const doc = cloneBaseline(FAIL_BASE)
      doc.p95_ms = 99
      expectInvalid(doc, 'p95_ms 99', 'FAIL')
    })
  })

  describe('THRESHOLD_EXCEEDED FAIL single-field negatives', () => {
    it('rejects p95_ms below the threshold', () => {
      const doc = cloneBaseline(THRESHOLD_BASE)
      doc.p95_ms = 99
      expectInvalid(doc, 'p95_ms 99', 'THRESHOLD_EXCEEDED')
    })

    it('rejects fewer than 20 samples', () => {
      const doc = cloneBaseline(THRESHOLD_BASE)
      doc.samples = (doc.samples as unknown[]).slice(0, 19)
      expectInvalid(doc, '19 samples', 'THRESHOLD_EXCEEDED')
    })

    it('rejects accepted_count 19 (must be 20)', () => {
      const doc = cloneBaseline(THRESHOLD_BASE)
      doc.accepted_count = 19
      expectInvalid(doc, 'accepted_count 19', 'THRESHOLD_EXCEEDED')
    })

    it('rejects failure stage sample (must be threshold)', () => {
      const doc = cloneBaseline(THRESHOLD_BASE)
      ;(doc.failure as Record<string, unknown>).stage = 'sample'
      expectInvalid(doc, 'stage sample', 'THRESHOLD_EXCEEDED')
    })

    it('rejects a non-null failure sample_index', () => {
      const doc = cloneBaseline(THRESHOLD_BASE)
      ;(doc.failure as Record<string, unknown>).sample_index = 7
      expectInvalid(doc, 'sample_index 7', 'THRESHOLD_EXCEEDED')
    })

    it('rejects null rendered_feature', () => {
      const doc = cloneBaseline(THRESHOLD_BASE)
      doc.rendered_feature = null
      expectInvalid(doc, 'null rendered_feature', 'THRESHOLD_EXCEEDED')
    })
  })

  it('rejects a schema-version/artifact mismatch', () => {
    const doc = cloneBaseline(PASS_BASE)
    doc.schema_version = '2.0'
    expectInvalid(doc, 'schema_version 2.0', 'PASS')
  })
})
