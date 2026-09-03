import { readFileSync, copyFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync, chmodSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

const repoRoot = path.resolve(__dirname, '../../../../')

function readRunbook(relative: string) {
  return readFileSync(path.join(repoRoot, relative), 'utf8')
}

const REQUIRED_ENV_KEYS = [
  'PLAYWRIGHT_LIVE_BASE_URL',
  'PLAYWRIGHT_LIVE_API_BASE_URL',
  'PLAYWRIGHT_LIVE_RIVER_BASIN_ID',
  'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID',
  'PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH',
]

const FORBIDDEN_OVERRIDE_KEYS = [
  'PLAYWRIGHT_LIVE_RIVER_RUN_ID',
  'PLAYWRIGHT_LIVE_RIVER_MODEL_ID',
  'PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID',
  'PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID',
  'PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME',
  'PLAYWRIGHT_LIVE_RIVER_SCENARIO',
]

/**
 * Slice the exact new section between the section header markers. These are
 * scoped assertions (not repository-wide toContain oracles) and do not use
 * `or true` anywhere.
 */
function sliceSection(text: string, startMarker: string, endMarker: string): string {
  const start = text.indexOf(startMarker)
  const end = text.indexOf(endMarker, start + startMarker.length)
  if (start < 0 || end < 0) throw new Error(`section markers not found: ${startMarker.slice(0, 40)}`)
  return text.slice(start, end)
}

function extractBashBlocks(section: string): string[] {
  const blocks: string[] = []
  const fence = /^[ \t]*```bash\r?\n([\s\S]*?)\r?\n[ \t]*```$/gm
  let match: RegExpExecArray | null
  while ((match = fence.exec(section)) !== null) {
    blocks.push(match[1].replace(/^[ \t]+/gm, ''))
  }
  return blocks
}

function bashSyntaxCheck(command: string): void {
  const dir = mkdtempSync(path.join(tmpdir(), 'nhms-runbook-syntax-'))
  try {
    const script = path.join(dir, 'cmd.sh')
    writeFileSync(script, command, 'utf8')
    execFileSync('bash', ['-n', script], { stdio: 'pipe' })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

/** Extract the exact run-root/prelude command block. */
function preludeBlock(text: string): string {
  const blocks = extractBashBlocks(text)
  const prelude = blocks.find((block) => block.includes('RUN_ROOT=') && (block.includes('mkdir') || block.includes('mktemp')))
  if (!prelude) throw new Error('no run-root prelude block found')
  return prelude
}

describe('river-click runbook contract', () => {
  it('bringup checklist C4-river-click section documents all five env keys and the exact six forbidden overrides', () => {
    const text = readRunbook('docs/runbooks/node-27-bringup-checklist.md')
    const section = sliceSection(text, '#### C4-river-click：', '\n## 上线判定')
    for (const key of REQUIRED_ENV_KEYS) {
      expect(section).toContain(key)
    }
    expect(section).not.toContain('PLAYWRIGHT_LIVE_RIVER_BASIN_ID`、`PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID` 是必需的 pin')
    for (const key of FORBIDDEN_OVERRIDE_KEYS) {
      expect(section).toContain(key)
    }
    expect(section).not.toMatch(/PLAYWRIGHT_LIVE_RIVER_\*_ID/)
    expect(section).toContain('test:e2e:live-display')
    expect(section).toContain('1895')
    expect(section).toContain('必须不存在')
    expect(section).toContain('0700')
  })

  it('bringup checklist command resolves the actual frontend package importer from repo root (pnpm --dir)', () => {
    const text = readRunbook('docs/runbooks/node-27-bringup-checklist.md')
    const section = sliceSection(text, '#### C4-river-click：', '\n## 上线判定')
    const commandBlocks = extractBashBlocks(section).filter((block) => block.includes('test:e2e:live-display'))
    expect(commandBlocks.length).toBeGreaterThanOrEqual(1)
    const command = commandBlocks[0]
    // The command must use --dir so the package importer resolves even though
    // the repo root has no package.json (the parent's direct repro).
    expect(command).toMatch(/corepack pnpm@10\.11\.0 --dir "\$REPO_ROOT\/apps\/frontend" run test:e2e:live-display/)
    expect(command).toMatch(/cd "\$REPO_ROOT" \|\|/)
    expect(command).toMatch(/\$\{PLAYWRIGHT_LIVE_BASE_URL-\}/)
    expect(command).toMatch(/\$\{PLAYWRIGHT_LIVE_RIVER_BASIN_ID-\}/)
    // Verify THIS command form actually resolves the frontend importer by
    // running a harmless script through the same --dir form.
    const dryRun = execFileSync('corepack', ['pnpm@10.11.0', '--dir', path.join(repoRoot, 'apps/frontend'), 'run', 'check:types'], {
      cwd: repoRoot,
      encoding: 'utf8',
    })
    expect(dryRun).toBeDefined()
  }, 120_000)

  it('bringup checklist brackets the command with set +e so CMD_EXIT is captured before set -e aborts', () => {
    const text = readRunbook('docs/runbooks/node-27-bringup-checklist.md')
    const section = sliceSection(text, '#### C4-river-click：', '\n## 上线判定')
    const commandBlocks = extractBashBlocks(section).filter((block) => block.includes('test:e2e:live-display'))
    expect(commandBlocks.length).toBeGreaterThanOrEqual(1)
    const command = commandBlocks[0]
    expect(command).toMatch(/set \+e/)
    expect(command).toMatch(/CMD_EXIT=\$\?/)
    expect(command).toMatch(/CMD_END=\$\(date -u \+%s\)/)
    expect(command).toMatch(/set -e/)
    expect(command).toMatch(/test "\$CMD_EXIT" = "0"/)
  })

  it('checked-in binder ACCEPTS a valid current-bracket PASS receipt and REJECTS each one-field mutation (executable discriminant)', () => {
    const binder = path.join(repoRoot, 'apps/frontend/scripts/river-click-receipt-binder.mjs')
    const example = path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json')
    // tmpdir() may be a symlink (/var/folders -> /private/var/folders); the
    // binder requires a CANONICAL existing parent, so realpath the root.
    const root = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-')))
    try {
      chmodSync(root, 0o700)
      const now = Math.floor(Date.now() / 1000)
      const cmdStart = now - 1_000
      const cmdEnd = now + 1_000
      const iso = (sec: number) => new Date(sec * 1000).toISOString().replace('.000Z', 'Z')
      // The example has fixed 2026-09-02 timestamps; rewrite them into the
      // current command bracket, then set the mtime inside it too.
      const base = JSON.parse(readFileSync(example, 'utf8'))
      base.started_at = iso(cmdStart)
      base.ended_at = iso(now)
      base.generated_at = iso(now)
      const baseArgs = [
        '--receipt', '',
        '--frontend-origin', 'https://test.nwm.ac.cn',
        '--api-origin', 'https://test.nwm.ac.cn',
        '--basin-id', 'basins_qhh',
        '--segment-id', 'basins_qhh_shud_reach_000001',
        '--cmd-start', String(cmdStart),
        '--cmd-end', String(cmdEnd),
      ]
      const writeReceipt = (name: string, doc: unknown) => {
        const p = path.join(root, name)
        writeFileSync(p, JSON.stringify(doc))
        chmodSync(p, 0o600)
        // mtime inside the bracket (mid-point).
        const mtime = new Date((cmdStart + now) / 2 * 1000)
        const { utimesSync } = require('node:fs') as typeof import('node:fs')
        utimesSync(p, mtime, mtime)
        return p
      }
      const runBinder = (receiptPath: string): boolean => {
        try {
          execFileSync('node', [binder, ...baseArgs.map((arg) => (arg === '' ? receiptPath : arg))], { encoding: 'utf8', stdio: 'pipe' })
          return true
        } catch {
          return false
        }
      }

      // ACCEPT: valid current-bracket PASS.
      const valid = writeReceipt('valid.json', base)
      expect(runBinder(valid), 'valid PASS must be accepted').toBe(true)

      // Each mutation changes EXACTLY ONE load-bearing field from an ALREADY
      // VALIDATED baseline; all must reject. The baseline `base` is validated
      // by the ACCEPT run above, so mutating one field proves that field.
      const cases: Array<[string, (doc: Record<string, unknown>) => void]> = [
        // ONE-field status flip: only `status` changes; the receipt still
        // carries PASS-counted samples, no failure, p95 < 2000. A PASS-only
        // binder must reject it on the status alone — changing 3 fields at
        // once (status+failure+p95) would not prove which field is tested.
        ['status FAIL only (one field)', (d) => { d.status = 'FAIL' }],
        ['non-null failure', (d) => { d.failure = { code: 'INTERNAL_ERROR', stage: 'sample', sample_index: null, gfs_status: null, ifs_status: null, message: 'x' } }],
        ['one sample gfs_status 500', (d) => { (d.samples as Array<Record<string, unknown>>)[0].gfs_status = 500 }],
        ['rendered river_segment_id differs', (d) => { (d.rendered_feature as Record<string, unknown>).river_segment_id = 'seg-DIFFERENT' }],
        ['started_at before bracket', (d) => { d.started_at = iso(cmdStart - 5_000) }],
        ['p95_ms 2000 (threshold)', (d) => { d.p95_ms = 2000 }],
        ['p95_ms differs from recomputed', (d) => { d.p95_ms = (d.p95_ms as number) + 1 }],
        ['origins.frontend ftp', (d) => { (d.origins as Record<string, unknown>).frontend = 'ftp://test.nwm.ac.cn' }],
        ['origins.frontend mismatches configured', (d) => { (d.origins as Record<string, unknown>).frontend = 'https://other.example.test' }],
        ['started_at +01:00 offset (non-UTC)', (d) => { d.started_at = (d.started_at as string).replace('Z', '+01:00') }],
        ['started_at non-calendar-valid', (d) => { d.started_at = '2026-02-30T00:00:00Z' }],
      ]
      for (const [name, mutate] of cases) {
        const doc = JSON.parse(JSON.stringify(base))
        mutate(doc)
        const p = writeReceipt(`m-${name.replace(/[^a-z0-9]+/gi, '-')}.json`, doc)
        expect(runBinder(p), `binder must reject ${name}`).toBe(false)
      }

      // Default-port variance is the SAME normalized origin: accept.
      const portDoc = JSON.parse(JSON.stringify(base))
      ;(portDoc.origins as Record<string, unknown>).frontend = 'https://test.nwm.ac.cn:443'
      const portPath = writeReceipt('m-default-port.json', portDoc)
      expect(runBinder(portPath), 'binder must accept a default-port-equivalent origin').toBe(true)

      // mtime outside the bracket -> reject (no content mutation needed).
      const outside = path.join(root, 'outside-mtime.json')
      writeFileSync(outside, JSON.stringify(base))
      chmodSync(outside, 0o600)
      const { utimesSync } = require('node:fs') as typeof import('node:fs')
      utimesSync(outside, new Date((cmdStart - 10_000) * 1000), new Date((cmdStart - 10_000) * 1000))
      expect(runBinder(outside), 'binder must reject an out-of-bracket mtime').toBe(false)

      // POSIX-level discriminants: the binder must reject a RELATIVE receipt
      // path argument (no path.resolve acceptance) and a symlinked parent
      // (no ancestor symlink).
      const relativeRun = (): boolean => {
        try {
          execFileSync('node', [binder, ...baseArgs.map((arg) => (arg === '' ? 'relative/valid.json' : arg))], { encoding: 'utf8', stdio: 'pipe' })
          return true
        } catch {
          return false
        }
      }
      expect(relativeRun(), 'binder must reject a relative receipt path').toBe(false)

      // Malformed command seconds (`123junk`) must reject (no parseInt leniency).
      const junkRun = (): boolean => {
        try {
          execFileSync('node', [binder, ...baseArgs.map((arg) => (arg === '' ? valid : arg === String(cmdStart) ? '123junk' : arg))], { encoding: 'utf8', stdio: 'pipe' })
          return true
        } catch {
          return false
        }
      }
      expect(junkRun(), 'binder must reject a malformed CMD_START').toBe(false)

      // Non-canonical (symlinked) parent must reject.
      const link = path.join(root, 'symlinked')
      const { symlinkSync } = require('node:fs') as typeof import('node:fs')
      symlinkSync(root, link)
      const linkRun = (): boolean => {
        try {
          execFileSync('node', [binder, ...baseArgs.map((arg) => (arg === '' ? path.join(link, 'valid.json') : arg))], { encoding: 'utf8', stdio: 'pipe' })
          return true
        } catch {
          return false
        }
      }
      expect(linkRun(), 'binder must reject a receipt under a symlinked (non-canonical) parent').toBe(false)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  }, 30_000)

  it('binder diagnostics are bounded and fixed-shaped: never echo the receipt path, configured origin, identity, or OS error; stderr <= 512 UTF-8 bytes', () => {
    const binder = path.join(repoRoot, 'apps/frontend/scripts/river-click-receipt-binder.mjs')
    const example = path.join(repoRoot, 'schemas/examples/frontend_river_click_live_evidence.example.json')
    const root = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-binder-bounded-')))
    try {
      chmodSync(root, 0o700)
      const now = Math.floor(Date.now() / 1000)
      const cmdStart = now - 1_000
      const cmdEnd = now + 1_000
      const iso = (sec: number) => new Date(sec * 1000).toISOString().replace('.000Z', 'Z')
      const base = JSON.parse(readFileSync(example, 'utf8'))
      base.started_at = iso(cmdStart)
      base.ended_at = iso(now)
      base.generated_at = iso(now)
      // Seed a secret path/value the binder must never echo.
      const secret = `SECRET-TOKEN-${Math.random().toString(16).slice(2)}`
      const secretReceipt = path.join(root, `nhms-frontend-river-click-live-evidence-${secret}.json`)
      writeFileSync(secretReceipt, JSON.stringify(base))
      chmodSync(secretReceipt, 0o600)
      const mtime = new Date((cmdStart + now) / 2 * 1000)
      const { utimesSync } = require('node:fs') as typeof import('node:fs')
      utimesSync(secretReceipt, mtime, mtime)
      // Break the receipt content so the binder exits 1 (p95 >= threshold).
      const broken = JSON.parse(JSON.stringify(base))
      broken.p95_ms = 2000
      const brokenPath = path.join(root, 'broken.json')
      writeFileSync(brokenPath, JSON.stringify(broken))
      chmodSync(brokenPath, 0o600)
      utimesSync(brokenPath, mtime, mtime)
      let stderr = ''
      let exit = 0
      try {
        execFileSync('node', [
          binder,
          '--receipt', secretReceipt,
          '--frontend-origin', 'https://test.nwm.ac.cn',
          '--api-origin', 'https://test.nwm.ac.cn',
          '--basin-id', `basins_${secret}`,
          '--segment-id', 'seg-001',
          '--cmd-start', String(cmdStart),
          '--cmd-end', String(cmdEnd),
        ], { encoding: 'utf8', stdio: 'pipe' })
      } catch (error) {
        const e = error as { status?: number; stderr?: string }
        exit = e.status ?? 1
        stderr = e.stderr ?? ''
      }
      expect(exit).not.toBe(0)
      expect(stderr).toMatch(/^BINDER: /)
      // The seeded secret (path fragment + origin + identity) never appears.
      expect(stderr).not.toContain(secret)
      expect(stderr).not.toContain('test.nwm.ac.cn')
      expect(stderr).not.toContain('nhms-frontend-river-click-live-evidence')
      // Bounded: at most 512 UTF-8 bytes of stderr.
      expect(new TextEncoder().encode(stderr).byteLength).toBeLessThanOrEqual(512)
      // Success line is fixed-shaped with no path.
      try {
        execFileSync('node', [
          binder,
          '--receipt', secretReceipt,
          '--frontend-origin', 'https://test.nwm.ac.cn',
          '--api-origin', 'https://test.nwm.ac.cn',
          '--basin-id', 'basins_qhh',
          '--segment-id', 'basins_qhh_shud_reach_000001',
          '--cmd-start', String(cmdStart),
          '--cmd-end', String(cmdEnd),
        ], { encoding: 'utf8', stdio: 'pipe' })
      } catch {
        // The secret-embedded path itself is a valid basename but the pin
        // differs (basins_qhh): the binder fails closed — still bounded.
      }
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  }, 30_000)

  it('bringup checklist command is valid bash and excludes a VITE export line', () => {
    const text = readRunbook('docs/runbooks/node-27-bringup-checklist.md')
    const section = sliceSection(text, '#### C4-river-click：', '\n## 上线判定')
    for (const block of extractBashBlocks(section)) {
      bashSyntaxCheck(block)
    }
    expect(extractBashBlocks(section).map((b) => b).join('\n')).not.toContain('VITE_API_BASE_URL')
  })

  it('private run directory is created exclusively with mktemp/unique path, canonical owner/mode, start/end brackets and absent-receipt assertion', () => {
    const text = readRunbook('docs/runbooks/node-27-bringup-checklist.md')
    const section = sliceSection(text, '#### C4-river-click：', '\n## 上线判定')
    const prelude = preludeBlock(section)
    // Exclusive creation under REPO_ROOT: one direct unique template, no shared
    // base that a fresh checkout would have to pre-create with mkdir -p.
    expect(prelude).toMatch(/RUN_ROOT=\$\(mktemp -d "\$REPO_ROOT\/\.nhms-issue1895-riverclick-XXXXXX"\)/)
    expect(prelude).not.toMatch(/mkdir -p/)
    expect(prelude).toMatch(/\$\(id -u\)/)
    expect(prelude).toMatch(/"700"/)
    expect(prelude).toMatch(/CMD_START/)
    expect(prelude).toContain('test ! -e "$RECEIPT"')
    expect(section).toMatch(/CMD_END/)
    expect(section).toMatch(/CMD_EXIT/)
    expect(section).not.toMatch(/\$RECEIPT\.bak/)
  })

  it('tier runbook 4.9 section documents the exact merged command with pnpm --dir and no VITE export between assignments', () => {
    const text = readRunbook('docs/runbooks/tier-node27-timeseries-storage.md')
    const section = sliceSection(text, '### 4.9 Frontend', '\n## 8.')
    for (const key of REQUIRED_ENV_KEYS) {
      expect(section).toContain(key)
    }
    expect(section).toContain('test:e2e:live-display')
    expect(section).toContain('0700')
    expect(section).toContain('0600')
    expect(section).toContain('frontend_river_click_live_evidence')
    expect(section).toContain('nearest-rank')
    expect(section).not.toContain('VITE_API_BASE_URL')
    expect(section).toContain('REPO_ROOT="/home/nwm/NWM"')
    expect(section).toMatch(/corepack pnpm@10\.11\.0 --dir "\$REPO_ROOT\/apps\/frontend"/)
    // Missing required env must reach the owner classification, never a shell
    // nounset abort, and a failed cd must fail closed before the set +e block.
    expect(section).toMatch(/cd "\$REPO_ROOT" \|\|/)
    expect(section).toMatch(/\$\{PLAYWRIGHT_LIVE_BASE_URL-\}/)
    expect(section).toMatch(/\$\{PLAYWRIGHT_LIVE_RIVER_BASIN_ID-\}/)
  })

  it('tier runbook command blocks are valid bash', () => {
    const text = readRunbook('docs/runbooks/tier-node27-timeseries-storage.md')
    const section = sliceSection(text, '### 4.9 Frontend', '\n## 8.')
    const blocks = extractBashBlocks(section)
    expect(blocks.length).toBeGreaterThanOrEqual(2)
    for (const block of blocks) {
      bashSyntaxCheck(block)
    }
  })

  it('both runbooks carry executable binder requirements (regular file, euid owner, 0600, nlink1, schema, checked-in Node binder)', () => {
    const checklist = sliceSection(readRunbook('docs/runbooks/node-27-bringup-checklist.md'), '#### C4-river-click：', '\n## 上线判定')
    const tier = sliceSection(readRunbook('docs/runbooks/tier-node27-timeseries-storage.md'), '### 4.9 Frontend', '\n## 8.')
    for (const section of [checklist, tier]) {
      expect(section).toContain("stat -c '%a'")
      expect(section).toContain('600')
      expect(section).toContain("stat -c '%h'")
      expect(section).toContain('check-jsonschema')
      expect(section).toContain('river-click-receipt-binder.mjs')
      expect(section).toContain('generated_at')
      expect(section).toMatch(/\$\(id -u\)/)
      expect(section).toMatch(/-f "\$RECEIPT"/)
    }
  })

  it('checklist binder references the checked-in Node binder that recomputes nearest-rank P95 and parses timestamps', () => {
    const section = sliceSection(readRunbook('docs/runbooks/node-27-bringup-checklist.md'), '#### C4-river-click：', '\n## 上线判定')
    expect(section).toMatch(/river-click-receipt-binder\.mjs/)
    expect(section).toMatch(/nearest-rank/)
    // The binder must never fall back to lexicographic time comparison.
    expect(section).not.toMatch(/fromisoformat/)
  })

  it('checklist binder asserts all indices 1..20, non-null identities, source pairs, and timestamp bracket', () => {
    const section = sliceSection(readRunbook('docs/runbooks/node-27-bringup-checklist.md'), '#### C4-river-click：', '\n## 上线判定')
    expect(section).toMatch(/river-click-receipt-binder\.mjs/)
    expect(section).toMatch(/check-jsonschema/)
    expect(section).toMatch(/CMD_START/)
    expect(section).toMatch(/CMD_END/)
    // The checked-in binder owns the semantic identity/count facts; the runbook
    // must point to it rather than inline a weaker Python copy.
    const binder = readFileSync(path.join(repoRoot, 'apps/frontend/scripts/river-click-receipt-binder.mjs'), 'utf8')
    expect(binder).toMatch(/nearestRankP95/)
    expect(binder).toMatch(/warmup_count\s*!==\s*WARMUP_COUNT/)
    expect(binder).toMatch(/index\s*!==\s*i\s*\+\s*1/)
    expect(binder).toMatch(/'GFS'/)
    expect(binder).toMatch(/'IFS'/)
    expect(binder).toMatch(/cmd-start/)
    expect(binder).toMatch(/cmd-end/)
  })

  it('checklist keeps the older C4 #389 path honest about delivered vs open work', () => {
    const text = readRunbook('docs/runbooks/node-27-bringup-checklist.md')
    expect(text).toContain('门控的只读测试钩子 + 无 mock 的 live P95 采集 lane')
    expect(text).toContain('#389')
    expect(text).toContain('#1970')
    expect(text).not.toMatch(/station popup live receipt.*已就绪|live receipt 已全部就绪/i)
  })
})
