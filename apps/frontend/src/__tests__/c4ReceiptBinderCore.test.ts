import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import {
  chmodSync,
  copyFileSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { acceptC4Receipt, MAX_RECEIPT_BYTES, type BinderResult } from '../../scripts/c4-receipt-binder-core.mjs'
import { acceptRiverClickReceipt } from '../../scripts/river-click-receipt-binder-core.mjs'
import { ownedPosixFdCount } from '../test/posixFdTable'

function refusedMessage(result: BinderResult): string {
  if (result.ok) throw new Error('expected a binder refusal')
  return result.message
}

const repoRoot = path.resolve(__dirname, '../../../../')
const examplePath = path.join(repoRoot, 'schemas/examples/frontend_c4_live_evidence.example.json')
const riverExamplePath = path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json')

function bracketedDoc() {
  const now = Math.floor(Date.now() / 1000)
  const cmdStart = now - 1_000
  const cmdEnd = now + 1_000
  const iso = (sec: number) => new Date(sec * 1000).toISOString().replace('.000Z', 'Z')
  const doc = JSON.parse(readFileSync(examplePath, 'utf8'))
  doc.started_at = iso(cmdStart)
  doc.ended_at = iso(now)
  doc.generated_at = iso(now)
  return { doc, cmdStart, cmdEnd, now }
}

function argsFor(receiptPath: string, cmdStart: number, cmdEnd: number) {
  return {
    receipt: receiptPath,
    'frontend-origin': 'https://test.nwm.ac.cn',
    'api-origin': 'https://test.nwm.ac.cn',
    'basin-id': 'basins_qhh',
    'segment-id': 'basins_qhh_shud_reach_000001',
    'cmd-start': String(cmdStart),
    'cmd-end': String(cmdEnd),
  }
}

function writePass(parent: string, name: string, doc: unknown, cmdStart: number, now: number) {
  const receiptPath = path.join(parent, name)
  writeFileSync(receiptPath, JSON.stringify(doc))
  chmodSync(receiptPath, 0o600)
  const mtime = new Date(((cmdStart + now) / 2) * 1000)
  utimesSync(receiptPath, mtime, mtime)
  return receiptPath
}

describe('C4 receipt binder core', () => {
  it('accepts a bounded valid PASS and keeps this-process fd count stable', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-c4-live-evidence-core.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      expect(acceptC4Receipt(argsFor(receiptPath, cmdStart, cmdEnd))).toEqual({ ok: true })
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a river-click receipt, a FAIL receipt, a symlink, and an oversized file', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-neg-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const river = JSON.parse(readFileSync(riverExamplePath, 'utf8'))
      river.started_at = doc.started_at
      river.ended_at = doc.ended_at
      river.generated_at = doc.generated_at
      const riverPath = writePass(parent, 'nhms-frontend-c4-live-evidence-river.json', river, cmdStart, now)
      expect(refusedMessage(acceptC4Receipt(argsFor(riverPath, cmdStart, cmdEnd)))).toMatch(/artifact identity|top-level key/)

      const fail = { ...doc, status: 'FAIL', failure: { code: 'PERMISSION_DENIED', stage: 'ops', message: 'x' } }
      const failPath = writePass(parent, 'nhms-frontend-c4-live-evidence-fail.json', fail, cmdStart, now)
      expect(refusedMessage(acceptC4Receipt(argsFor(failPath, cmdStart, cmdEnd)))).toMatch(/status must be PASS/)

      const realPath = writePass(parent, 'nhms-frontend-c4-live-evidence-real.json', doc, cmdStart, now)
      const linkPath = path.join(parent, 'nhms-frontend-c4-live-evidence-link.json')
      symlinkSync(realPath, linkPath)
      expect(refusedMessage(acceptC4Receipt(argsFor(linkPath, cmdStart, cmdEnd)))).toMatch(/symlink/)

      const oversize = writePass(parent, 'nhms-frontend-c4-live-evidence-oversize.json', { pad: 'x'.repeat(MAX_RECEIPT_BYTES + 8) }, cmdStart, now)
      expect(refusedMessage(acceptC4Receipt(argsFor(oversize, cmdStart, cmdEnd)))).toMatch(/bounded ceiling|valid JSON|artifact/)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('keeps equal source job IDs as a second-stage semantic binder invariant after schema acceptance', () => {
    const schema = JSON.parse(readFileSync(path.join(repoRoot, 'schemas/frontend_c4_live_evidence.schema.json'), 'utf8'))
    expect(schema.description).toContain('semantic validation and the binder additionally require ops.gfs.job_id and ops.ifs.job_id to differ')
  })

  it('refuses nested key drift, unsafe identities, wrong read paths, and equal source job IDs', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-nested-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const mutations: Array<[string, (receipt: Record<string, unknown>) => void]> = [
        ['missing home field', (receipt) => delete (receipt.home as Record<string, unknown>).current_read_path],
        ['extra nested field', (receipt) => { (receipt.ops as Record<string, Record<string, unknown>>).gfs.extra = true }],
        ['wrong current read path', (receipt) => { (receipt.home as Record<string, unknown>).current_read_path = '/api/v1/models' }],
        ['file-shaped job id', (receipt) => { (receipt.ops as Record<string, Record<string, unknown>>).gfs.job_id = 'file:private/job' }],
        ['userinfo-shaped job id', (receipt) => { (receipt.ops as Record<string, Record<string, unknown>>).gfs.job_id = 'user@example.test' }],
        ['equal source job ids', (receipt) => {
          const ops = receipt.ops as Record<string, Record<string, unknown>>
          ops.ifs.job_id = ops.gfs.job_id
        }],
      ]
      for (const [name, mutate] of mutations) {
        const mutated = JSON.parse(JSON.stringify(doc)) as Record<string, unknown>
        mutate(mutated)
        const receipt = writePass(parent, `nhms-frontend-c4-live-evidence-${name.replaceAll(' ', '-')}.json`, mutated, cmdStart, now)
        expect(acceptC4Receipt(argsFor(receipt, cmdStart, cmdEnd)).ok, name).toBe(false)
      }
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses swapped origins, pins, and current identities, and river binder refuses a C4 receipt', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-swap-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-c4-live-evidence-swap.json', doc, cmdStart, now)
      const args = argsFor(receiptPath, cmdStart, cmdEnd)
      expect(refusedMessage(acceptC4Receipt({ ...args, 'frontend-origin': 'https://other.example.test' }))).toMatch(/origin/)
      expect(refusedMessage(acceptC4Receipt({ ...args, 'basin-id': 'other-basin' }))).toMatch(/basin_id/)
      const extra = { ...doc, extra: 'nope' }
      const extraPath = writePass(parent, 'nhms-frontend-c4-live-evidence-extra.json', extra, cmdStart, now)
      expect(refusedMessage(acceptC4Receipt(argsFor(extraPath, cmdStart, cmdEnd)))).toMatch(/top-level key/)
      expect(acceptRiverClickReceipt(args).ok).toBe(false)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it.each([
    'receipt',
    'frontend-origin',
    'api-origin',
    'basin-id',
    'segment-id',
    'cmd-start',
    'cmd-end',
  ] as const)('refuses a missing %s binder argument', (key) => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-missing-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-c4-live-evidence-missing.json', doc, cmdStart, now)
      const args = { ...argsFor(receiptPath, cmdStart, cmdEnd) }
      delete args[key]
      expect(refusedMessage(acceptC4Receipt(args))).toBe('missing required binder arguments')
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a malformed or reordered command bracket', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-bracket-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-c4-live-evidence-bracket.json', doc, cmdStart, now)
      const args = argsFor(receiptPath, cmdStart, cmdEnd)
      expect(refusedMessage(acceptC4Receipt({ ...args, 'cmd-start': '123junk' }))).toMatch(/CMD_START/)
      expect(refusedMessage(acceptC4Receipt({
        ...args,
        'cmd-start': String(cmdEnd),
        'cmd-end': String(cmdStart),
      }))).toMatch(/CMD_START\/CMD_END bracket/)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses receipt mtime, started_at, and ended_at outside the command bracket', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-window-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const iso = (sec: number) => new Date(sec * 1000).toISOString().replace('.000Z', 'Z')
      const mtimePath = writePass(parent, 'nhms-frontend-c4-live-evidence-mtime.json', doc, cmdStart, now)
      const earlyMtime = new Date((cmdStart - 5) * 1000)
      utimesSync(mtimePath, earlyMtime, earlyMtime)
      expect(refusedMessage(acceptC4Receipt(argsFor(mtimePath, cmdStart, cmdEnd)))).toMatch(/mtime is before CMD_START/)

      const started = { ...doc, started_at: iso(cmdStart - 1), ended_at: iso(now), generated_at: iso(now) }
      const startedPath = writePass(parent, 'nhms-frontend-c4-live-evidence-started.json', started, cmdStart, now)
      expect(refusedMessage(acceptC4Receipt(argsFor(startedPath, cmdStart, cmdEnd)))).toMatch(/started_at is before CMD_START/)

      const ended = { ...doc, started_at: iso(cmdStart), ended_at: iso(cmdEnd + 1), generated_at: iso(cmdEnd + 1) }
      const endedPath = writePass(parent, 'nhms-frontend-c4-live-evidence-ended.json', ended, cmdStart, now)
      expect(refusedMessage(acceptC4Receipt(argsFor(endedPath, cmdStart, cmdEnd)))).toMatch(/ended_at is after CMD_END/)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses the wrong receipt mode, nlink, and parent mode', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-posix-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const modePath = writePass(parent, 'nhms-frontend-c4-live-evidence-mode.json', doc, cmdStart, now)
      chmodSync(modePath, 0o644)
      expect(refusedMessage(acceptC4Receipt(argsFor(modePath, cmdStart, cmdEnd)))).toMatch(/mode is not 600/)

      const nlinkPath = writePass(parent, 'nhms-frontend-c4-live-evidence-nlink.json', doc, cmdStart, now)
      linkSync(nlinkPath, path.join(parent, 'nhms-frontend-c4-live-evidence-nlink-hard.json'))
      expect(refusedMessage(acceptC4Receipt(argsFor(nlinkPath, cmdStart, cmdEnd)))).toMatch(/nlink is not 1/)

      const parentModePath = writePass(parent, 'nhms-frontend-c4-live-evidence-parent-mode.json', doc, cmdStart, now)
      chmodSync(parent, 0o755)
      expect(refusedMessage(acceptC4Receipt(argsFor(parentModePath, cmdStart, cmdEnd)))).toMatch(/parent mode is not 700/)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a hook-driven parent identity change after pathname facts', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-parent-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-c4-live-evidence-parent.json', doc, cmdStart, now)
      const result = acceptC4Receipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        hooks: {
          afterPathnameFacts: ({ parentPath, receiptPath: current }) => {
            const replacement = `${parentPath}.replacement`
            mkdirSync(replacement, { mode: 0o700 })
            chmodSync(replacement, 0o700)
            copyFileSync(current, path.join(replacement, path.basename(current)))
            chmodSync(path.join(replacement, path.basename(current)), 0o600)
            renameSync(parentPath, `${parentPath}.old`)
            renameSync(replacement, parentPath)
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toMatch(/parent identity changed|descriptor identity differs|open failed/)
    } finally {
      rmSync(parent, { recursive: true, force: true })
      rmSync(`${parent}.old`, { recursive: true, force: true })
      rmSync(`${parent}.replacement`, { recursive: true, force: true })
    }
  })

  it('CLI binder refuses a missing required argument without inventing flags', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-binder-cli-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-c4-live-evidence-cli.json', doc, cmdStart, now)
      const binder = path.resolve(__dirname, '../../scripts/c4-receipt-binder.mjs')
      const argv = [
        binder,
        '--receipt', receiptPath,
        '--frontend-origin', 'https://test.nwm.ac.cn',
        '--api-origin', 'https://test.nwm.ac.cn',
        '--basin-id', 'basins_qhh',
        '--segment-id', 'basins_qhh_shud_reach_000001',
        '--cmd-start', String(cmdStart),
      ]
      expect(() => execFileSync('node', argv, { encoding: 'utf8', stdio: 'pipe' })).toThrow()
      try {
        execFileSync('node', argv, { encoding: 'utf8', stdio: 'pipe' })
      } catch (error) {
        const stderr = (error as { stderr?: string }).stderr ?? ''
        expect(stderr).toMatch(/missing required binder arguments/)
      }
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })
})
