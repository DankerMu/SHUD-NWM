import { describe, expect, it } from 'vitest'
import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import {
  buildC4PassEvidence,
  buildC4TerminalEvidence,
  validateC4EvidenceDocument,
  type C4PassInput,
} from '../receipt'

const repoRoot = path.resolve(__dirname, '../../../../../../')
const passExample = JSON.parse(
  readFileSync(path.join(repoRoot, 'schemas/examples/frontend_c4_live_evidence.example.json'), 'utf8'),
)

function passInput(base: Partial<C4PassInput> = {}): C4PassInput {
  return {
    startedAt: '2026-09-04T01:00:00Z',
    endedAt: '2026-09-04T01:02:03Z',
    frontendOrigin: 'https://test.nwm.ac.cn',
    apiOrigin: 'https://test.nwm.ac.cn',
    requestedPins: { basinId: 'basins_qhh', riverSegmentId: 'basins_qhh_shud_reach_000001' },
    gfs: {
      sourceId: 'GFS',
      basinId: 'basins_qhh',
      basinVersionId: 'bv-2026-09',
      riverNetworkVersionId: 'rn-2026-09',
      runId: 'qhh_20260904_gfs',
      modelId: 'shud-gfs',
      cycleTime: '2026-09-04T00:00:00Z',
      scenario: 'forecast_gfs_deterministic',
    },
    ifs: {
      sourceId: 'IFS',
      basinId: 'basins_qhh',
      basinVersionId: 'bv-2026-09',
      riverNetworkVersionId: 'rn-2026-09',
      runId: 'qhh_20260904_ifs',
      modelId: 'shud-ifs',
      cycleTime: '2026-09-04T06:00:00Z',
      scenario: 'forecast_ifs_deterministic',
    },
    home: {
      path: '/',
      mapSurfaceVisible: true,
      runtimeConfigStatus: 200,
      runtimeServiceRole: 'display_readonly',
      currentReadObserved: true,
      currentReadPath: '/api/v1/basins',
    },
    opsGfs: {
      sourceId: 'GFS',
      path: '/ops',
      headingObserved: true,
      permissionDenied: false,
      runtimeUnavailable: false,
      statusStatus: 200,
      stagesStatus: 200,
      jobsStatus: 200,
      jobId: 'job-gfs-001',
      logsStatus: 200,
      roleSelectorCount: 0,
      retryCancelControlCount: 0,
      slurmRequestCount: 0,
      nonGetControlCount: 0,
      queueReadonlyVisible: true,
      operatorRecoveryVisible: true,
    },
    noControl: { slurmRequestCount: 0, nonGetControlCount: 0 },
    opsIfs: {
      sourceId: 'IFS',
      path: '/ops',
      headingObserved: true,
      permissionDenied: false,
      runtimeUnavailable: false,
      statusStatus: 200,
      stagesStatus: 200,
      jobsStatus: 200,
      jobId: 'job-ifs-001',
      logsStatus: 200,
      roleSelectorCount: 0,
      retryCancelControlCount: 0,
      slurmRequestCount: 0,
      nonGetControlCount: 0,
      queueReadonlyVisible: true,
      operatorRecoveryVisible: true,
    },
    ...base,
  }
}

describe('C4 evidence construction', () => {
  it('builds a schema-1.0 PASS document matching the checked-in example shape', () => {
    const result = buildC4PassEvidence(passInput())
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('PASS fixture must build')
    expect(result.receipt.artifact).toBe('nhms-frontend-c4-live-evidence')
    expect(result.receipt.status).toBe('PASS')
    expect(result.receipt.failure).toBeNull()
    expect(result.receipt.ops.gfs?.job_id).toBe('job-gfs-001')
    expect(result.receipt.ops.ifs?.job_id).toBe('job-ifs-001')
    // productWire uses the shipping river-click normalizer, whose wire spelling
    // is canonical UTC milliseconds rather than the DB's optional bare-Z form.
    expect(result.receipt.gfs?.cycle_time).toBe('2026-09-04T00:00:00.000Z')
    expect(result.receipt.ifs?.cycle_time).toBe('2026-09-04T06:00:00.000Z')
    expect(validateC4EvidenceDocument(result.receipt)).toEqual({ ok: true })
    const bridgeFixture = process.env.C4_ISSUE1895_BRIDGE_FIXTURE
    if (bridgeFixture) writeFileSync(bridgeFixture, JSON.stringify(result.receipt))
    expect(validateC4EvidenceDocument(passExample).ok).toBe(true)
  })

  it('refuses a river-click receipt as a C4 document', () => {
    const river = JSON.parse(
      readFileSync(path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json'), 'utf8'),
    )
    expect(validateC4EvidenceDocument(river).ok).toBe(false)
  })

  it('refuses raw URL/query material in identities and extra fields', () => {
    const result = buildC4PassEvidence(passInput())
    if (!result.ok) throw new Error('PASS fixture must build')
    const mutated = { ...result.receipt, extra: 'nope' }
    expect(validateC4EvidenceDocument(mutated).ok).toBe(false)
    const urlIdentity = buildC4PassEvidence(passInput({
      gfs: { ...passInput().gfs, runId: 'https://example.test/run' },
    }))
    expect(urlIdentity.ok).toBe(false)
  })

  it('rejects unsafe current-read paths, identity values, and failure messages before publication', () => {
    const pass = buildC4PassEvidence(passInput({
      home: { ...passInput().home, currentReadPath: '/api/v1/models' },
    }))
    expect(pass.ok).toBe(false)
    for (const runId of ['file:///private/run', 'ws://socket.example.test', 'user@example.test']) {
      const identity = buildC4PassEvidence(passInput({
        gfs: { ...passInput().gfs, runId },
      }))
      expect(identity.ok, runId).toBe(false)
    }
    const sameJob = buildC4PassEvidence(passInput({
      opsIfs: { ...passInput().opsIfs, jobId: passInput().opsGfs.jobId },
    }))
    expect(sameJob.ok).toBe(false)
    const observedControl = buildC4PassEvidence(passInput({
      noControl: { slurmRequestCount: 0, nonGetControlCount: 1 },
    }))
    expect(observedControl.ok).toBe(false)
    const nested = JSON.parse(JSON.stringify(passExample)) as Record<string, unknown>
    delete (nested.home as Record<string, unknown>).current_read_path
    expect(validateC4EvidenceDocument(nested).ok).toBe(false)
    for (const message of ['file:///private/path', 'ws://socket.example.test', 'user@example.test']) {
      const failure = buildC4TerminalEvidence({
        startedAt: '2026-09-04T01:00:00Z',
        endedAt: '2026-09-04T01:00:01Z',
        frontendOrigin: 'https://test.nwm.ac.cn',
        apiOrigin: 'https://test.nwm.ac.cn',
        failure: { code: 'INTERNAL_ERROR', stage: 'runtime', message },
      })
      expect(failure.ok, message).toBe(false)
    }
  })

  it.each([
    ['page error during home proof', 'HOME_NOT_READY', 'home'],
    ['home map source error contradicts readiness', 'HOME_NOT_READY', 'home'],
    ['C4 lane internal error', 'INTERNAL_ERROR', 'runtime'],
    ['whole-run deadline exceeded before preflight', 'WHOLE_RUN_TIMEOUT', 'timeout'],
  ] as const)('builds each C4 fixed terminal message as a publishable FAIL receipt', (message, code, stage) => {
    const failure = buildC4TerminalEvidence({
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:00:01Z',
      frontendOrigin: 'https://test.nwm.ac.cn',
      apiOrigin: 'https://test.nwm.ac.cn',
      failure: { code, stage, message },
    })

    expect(failure.ok, message).toBe(true)
    if (!failure.ok) throw new Error(`${message} must build`)
    expect(validateC4EvidenceDocument(failure.receipt)).toEqual({ ok: true })
  })

  it('builds BLOCKED all-null claims and FAIL with preserved origins', () => {
    const blocked = buildC4TerminalEvidence({
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:00:01Z',
      frontendOrigin: null,
      apiOrigin: null,
      failure: { code: 'REQUIRED_ENV_MISSING', stage: 'runtime', message: 'missing required C4 live evidence env' },
    })
    expect(blocked.ok).toBe(true)
    if (!blocked.ok) throw new Error('blocked must build')
    expect(blocked.receipt.status).toBe('BLOCKED')
    expect(blocked.receipt.origins).toEqual({ frontend: null, api: null })
    expect(blocked.receipt.home).toBeNull()

    const fail = buildC4TerminalEvidence({
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:01:03Z',
      frontendOrigin: 'https://test.nwm.ac.cn',
      apiOrigin: 'https://test.nwm.ac.cn',
      requestedPins: { basinId: 'basins_qhh', riverSegmentId: 'basins_qhh_shud_reach_000001' },
      failure: { code: 'PERMISSION_DENIED', stage: 'ops', message: 'production bundle role cannot access /ops' },
    })
    expect(fail.ok).toBe(true)
    if (!fail.ok) throw new Error('fail must build')
    expect(fail.receipt.status).toBe('FAIL')
    expect(fail.receipt.origins.frontend).toBe('https://test.nwm.ac.cn')
  })
})
