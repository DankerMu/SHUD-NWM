import { describe, expect, it } from 'vitest'
import {
  chmodSync,
  copyFileSync,
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

import { acceptRiverClickReceipt, MAX_RECEIPT_BYTES, realBinderFs, type BinderResult } from '../../scripts/river-click-receipt-binder-core.mjs'
import { ownedPosixFdCount } from '../test/posixFdTable'

function refusedMessage(result: BinderResult): string {
  if (result.ok) throw new Error('expected a binder refusal')
  return result.message
}

const repoRoot = path.resolve(__dirname, '../../../../')
const examplePath = path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json')

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

describe('river-click receipt binder core (in-process descriptor acceptance)', () => {
  it('accepts a bounded valid PASS and keeps this-process fd count stable on every terminal', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-core-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-core.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const accepted = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd))
      expect(accepted).toEqual({ ok: true, p95: doc.p95_ms })
      expect(ownedPosixFdCount()).toBe(before)

      const refused = acceptRiverClickReceipt(argsFor(receiptPath + '-missing', cmdStart, cmdEnd))
      expect(refused.ok).toBe(false)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a real same-euid/mode pathname replacement of the parent directory after initial facts', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-parent-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-parent.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
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
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
      rmSync(`${parent}.old`, { recursive: true, force: true })
      rmSync(`${parent}.replacement`, { recursive: true, force: true })
    }
  })

  it('refuses when only the post-read parent lstat identity changes, with receipt facts and PASS bytes unchanged', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-parent-ino-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-parent-ino.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const accepted = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd))
      expect(accepted).toEqual({ ok: true, p95: doc.p95_ms })
      const real = realBinderFs()
      let parentLstats = 0
      let closeCalls = 0
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          lstatSync: (target: string) => {
            const info = real.lstatSync(target)
            if (target !== parent) return info
            parentLstats += 1
            if (parentLstats === 1) return info
            return {
              mode: info.mode,
              uid: info.uid,
              nlink: info.nlink,
              size: info.size,
              dev: info.dev,
              ino: info.ino + 1,
              mtimeMs: info.mtimeMs,
              ctimeMs: info.ctimeMs,
              isFile: info.isFile,
              isDirectory: info.isDirectory,
              isSymbolicLink: info.isSymbolicLink,
            }
          },
          closeSync: (fd: number) => {
            closeCalls += 1
            real.closeSync(fd)
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toBe('receipt parent identity changed during read')
      expect(parentLstats).toBeGreaterThan(1)
      expect(closeCalls).toBe(1)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a leaf swap between lstat and open (O_NOFOLLOW, never initial-lstat-only)', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-leaf-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-leaf.json', doc, cmdStart, now)
      const alt = writePass(parent, 'nhms-frontend-river-click-live-evidence-alt.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        hooks: {
          afterPathnameFacts: ({ receiptPath: current }) => {
            renameSync(current, `${current}.orig`)
            symlinkSync(alt, current)
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toMatch(/open failed|regular file|descriptor identity differs/)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a same-inode same-size write during read via mtime/ctime identity', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-mutate-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-mutate.json', doc, cmdStart, now)
      const mutated = JSON.parse(JSON.stringify(doc))
      mutated.p95_ms = (doc.p95_ms as number) + 1
      let mutatedBytes = Buffer.from(JSON.stringify(mutated))
      const original = readFileSync(receiptPath)
      if (mutatedBytes.length !== original.length) {
        mutatedBytes = Buffer.concat([mutatedBytes, Buffer.alloc(Math.max(0, original.length - mutatedBytes.length), 0x20)])
        mutatedBytes = mutatedBytes.subarray(0, original.length)
      }
      const before = ownedPosixFdCount()
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        hooks: {
          afterRead: ({ receiptPath: current }) => {
            writeFileSync(current, mutatedBytes)
            chmodSync(current, 0o600)
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toMatch(/identity changed/)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses short/read/fstat errors and closes the descriptor on each terminal', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-err-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-err.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const real = realBinderFs()

      const short = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          readSync: () => 0,
        },
      })
      expect(short.ok).toBe(false)
      expect(refusedMessage(short)).toBe('receipt read is incomplete')
      expect(ownedPosixFdCount()).toBe(before)

      const readBoom = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          readSync: () => {
            throw new Error('read boom')
          },
        },
      })
      expect(readBoom.ok).toBe(false)
      expect(refusedMessage(readBoom)).toBe('receipt read failed')
      expect(ownedPosixFdCount()).toBe(before)

      let fstatCalls = 0
      const fstatBoom = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          fstatSync: (fd: number) => {
            fstatCalls += 1
            if (fstatCalls === 1) throw new Error('fstat boom')
            return real.fstatSync(fd)
          },
        },
      })
      expect(fstatBoom.ok).toBe(false)
      expect(refusedMessage(fstatBoom)).toBe('receipt descriptor status is unreadable')
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('refuses a valid PASS when closeSync throws, never returns ok:true, and does not leak the real fd', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-close-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-close.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const real = realBinderFs()
      let closeCalls = 0
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          closeSync: (fd: number) => {
            closeCalls += 1
            real.closeSync(fd)
            throw new Error('close boom')
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toBe('receipt close failed')
      expect(closeCalls).toBe(1)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('calls close exactly once on a semantic/read failure and keeps the original closed refusal', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-close-fail-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-close-fail.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const real = realBinderFs()
      let closeCalls = 0
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          readSync: () => {
            throw new Error('read boom')
          },
          closeSync: (fd: number) => {
            closeCalls += 1
            real.closeSync(fd)
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toBe('receipt read failed')
      expect(closeCalls).toBe(1)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('calls close exactly once on success', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-close-ok-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-close-ok.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const real = realBinderFs()
      let closeCalls = 0
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          closeSync: (fd: number) => {
            closeCalls += 1
            real.closeSync(fd)
          },
        },
      })
      expect(result).toEqual({ ok: true, p95: doc.p95_ms })
      expect(closeCalls).toBe(1)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('returns non-PASS without a raw error when both the business refusal and closeSync fail', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-close-both-')))
    try {
      chmodSync(parent, 0o700)
      const { doc, cmdStart, cmdEnd, now } = bracketedDoc()
      const receiptPath = writePass(parent, 'nhms-frontend-river-click-live-evidence-close-both.json', doc, cmdStart, now)
      const before = ownedPosixFdCount()
      const real = realBinderFs()
      let closeCalls = 0
      const result = acceptRiverClickReceipt(argsFor(receiptPath, cmdStart, cmdEnd), {
        fs: {
          ...real,
          readSync: () => {
            throw new Error('read boom')
          },
          closeSync: (fd: number) => {
            closeCalls += 1
            real.closeSync(fd)
            throw new Error('close boom')
          },
        },
      })
      expect(result.ok).toBe(false)
      expect(refusedMessage(result)).toBe('receipt read failed')
      expect(refusedMessage(result)).not.toMatch(/close boom|read boom/)
      expect(closeCalls).toBe(1)
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('exposes the same 262144-byte ceiling the CLI binder uses', () => {
    expect(MAX_RECEIPT_BYTES).toBe(262144)
  })
})
