import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import Ajv2020 from 'ajv/dist/2020'

const repoRoot = path.resolve(__dirname, '../../../../')
const schemaPath = path.join(repoRoot, 'schemas/frontend_c4_live_evidence.schema.json')
const passExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_c4_live_evidence.example.json'), 'utf8'),
)
const blockedExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_c4_live_evidence.partial.example.json'), 'utf8'),
)
const failExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_c4_live_evidence.error.example.json'), 'utf8'),
)

const schema = JSON.parse(readFileSync(schemaPath, 'utf8'))
const ajv = new Ajv2020({ strict: false, allErrors: true, formats: {
  'date-time': /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/,
} })
const validate = ajv.compile(schema)

function cloneBaseline(document: unknown): Record<string, unknown> {
  return JSON.parse(JSON.stringify(document)) as Record<string, unknown>
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

describe('C4 evidence schema branch discrimination (AJV Draft-2020-12)', () => {
  const PASS_BASE = cloneBaseline(passExample)
  const BLOCKED_BASE = cloneBaseline(blockedExample)
  const FAIL_BASE = cloneBaseline(failExample)

  it('accepts the three checked-in examples', () => {
    expectValid(PASS_BASE)
    expectValid(BLOCKED_BASE)
    expectValid(FAIL_BASE)
  })

  it('documents that equal PASS job IDs are a semantic-validator and binder invariant, not a Draft 2020-12 sibling comparison', () => {
    const doc = cloneBaseline(PASS_BASE)
    const ops = doc.ops as Record<string, Record<string, unknown>>
    ops.ifs.job_id = ops.gfs.job_id

    expectValid(doc)
  })

  it('rejects a river-click receipt against the C4 schema', () => {
    const river = JSON.parse(
      readFileSync(path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json'), 'utf8'),
    )
    expectInvalid(river, 'river-click artifact', 'FOREIGN')
  })

  describe('PASS single-field negatives', () => {
    it('rejects a non-null failure', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.failure = { code: 'INTERNAL_ERROR', stage: 'ops', message: 'x' }
      expectInvalid(doc, 'non-null failure', 'PASS')
    })

    it('rejects null home', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.home = null
      expectInvalid(doc, 'null home', 'PASS')
    })

    it.each([
      ['ops.gfs', (doc: Record<string, unknown>) => { (doc.ops as Record<string, unknown>).gfs = null }],
      ['ops.ifs', (doc: Record<string, unknown>) => { (doc.ops as Record<string, unknown>).ifs = null }],
      ['no_control', (doc: Record<string, unknown>) => { doc.no_control = null }],
      ['origins.frontend', (doc: Record<string, unknown>) => { (doc.origins as Record<string, unknown>).frontend = null }],
      ['origins.api', (doc: Record<string, unknown>) => { (doc.origins as Record<string, unknown>).api = null }],
    ])('rejects null %s on PASS', (_slot, mutate) => {
      const doc = cloneBaseline(PASS_BASE)
      mutate(doc)
      expectInvalid(doc, `null ${_slot}`, 'PASS')
    })

    it('rejects a non-RFC3339 product cycle time', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.gfs as Record<string, unknown>).cycle_time = 'not-a-timestamp'
      expectInvalid(doc, 'invalid product cycle_time', 'PASS')
    })

    it('rejects gfs.source_id IFS', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.gfs as Record<string, unknown>).source_id = 'IFS'
      expectInvalid(doc, 'gfs source_id IFS', 'PASS')
    })

    it('rejects ops.gfs.source_id IFS', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;((doc.ops as Record<string, unknown>).gfs as Record<string, unknown>).source_id = 'IFS'
      expectInvalid(doc, 'ops.gfs source_id IFS', 'PASS')
    })

    it('rejects home.path /ops', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.home as Record<string, unknown>).path = '/ops'
      expectInvalid(doc, 'home.path /ops', 'PASS')
    })

    it('rejects an unobserved current-read path', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;(doc.home as Record<string, unknown>).current_read_path = '/api/v1/models'
      expectInvalid(doc, 'unknown home current_read_path', 'PASS')
    })

    it('rejects file, websocket, and userinfo-shaped identities', () => {
      for (const value of ['file:///private/path', 'file:private/path', 'ws://socket.example.test', 'wss:socket.example.test', 'user@example.test']) {
        const doc = cloneBaseline(PASS_BASE)
        ;(doc.gfs as Record<string, unknown>).run_id = value
        expectInvalid(doc, `unsafe run_id ${value}`, 'PASS')
      }
    })

    it('rejects nonzero observed control counts on PASS', () => {
      const doc = cloneBaseline(PASS_BASE)
      ;((doc.ops as Record<string, unknown>).gfs as Record<string, unknown>).non_get_control_count = 1
      expectInvalid(doc, 'nonzero ops control count', 'PASS')
    })

    it('rejects a missing or extra nested home key', () => {
      const missing = cloneBaseline(PASS_BASE)
      delete (missing.home as Record<string, unknown>).current_read_path
      expectInvalid(missing, 'missing home current_read_path', 'PASS')
      const extra = cloneBaseline(PASS_BASE)
      ;(extra.home as Record<string, unknown>).extra = true
      expectInvalid(extra, 'extra home field', 'PASS')
    })

    it('rejects an extra top-level field', () => {
      const doc = cloneBaseline(PASS_BASE)
      doc.p95_ms = 1
      expectInvalid(doc, 'extra p95_ms', 'PASS')
    })
  })

  describe('BLOCKED single-field negatives', () => {
    it('rejects a FAIL code on BLOCKED', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      ;(doc.failure as Record<string, unknown>).code = 'PERMISSION_DENIED'
      expectInvalid(doc, 'FAIL code on BLOCKED', 'BLOCKED')
    })

    it('rejects non-null home on BLOCKED', () => {
      const doc = cloneBaseline(BLOCKED_BASE)
      doc.home = (PASS_BASE.home as Record<string, unknown>)
      expectInvalid(doc, 'non-null home', 'BLOCKED')
    })
  })

  describe('FAIL single-field negatives', () => {
    it('accepts a FAIL receipt that records an observed forbidden request count', () => {
      const doc = cloneBaseline(FAIL_BASE)
      ;(doc.no_control as Record<string, unknown>).non_get_control_count = 1
      expectValid(doc)
    })

    it('rejects file, websocket, and userinfo-shaped failure messages', () => {
      for (const message of ['file:///private/path', 'file:private/path', 'wss://socket.example.test', 'wss:socket.example.test', 'user@example.test']) {
        const doc = cloneBaseline(FAIL_BASE)
        ;(doc.failure as Record<string, unknown>).message = message
        expectInvalid(doc, `unsafe failure message ${message}`, 'FAIL')
      }
    })

    it('rejects a BLOCKED code on FAIL', () => {
      const doc = cloneBaseline(FAIL_BASE)
      ;(doc.failure as Record<string, unknown>).code = 'REQUIRED_ENV_MISSING'
      expectInvalid(doc, 'BLOCKED code on FAIL', 'FAIL')
    })

    it('rejects null failure on FAIL', () => {
      const doc = cloneBaseline(FAIL_BASE)
      doc.failure = null
      expectInvalid(doc, 'null failure', 'FAIL')
    })
  })
})
