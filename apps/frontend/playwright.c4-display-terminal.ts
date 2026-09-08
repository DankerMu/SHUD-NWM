/**
 * Lane-result -> receipt -> publish decision for the C4 live lane (#1895).
 * Exactly one publication attempt on every path.
 */

import {
  buildC4PassEvidence,
  buildC4TerminalEvidence,
  type C4BuildResult,
  type C4Evidence,
  type C4PassInput,
} from './src/lib/c4DisplayEvidence/receipt'
import type { RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import type { C4LaneTerminal } from './playwright.c4-display-lane'

export interface C4TerminalPublication {
  publish(path: string, receipt: C4Evidence): { path: string }
}

export type C4TerminalResult =
  | { ok: true; receipt: C4Evidence }
  | { ok: false; code: string; message: string }

export type C4PassResult =
  | { ok: true; receipt: C4Evidence }
  | { ok: false; code: string; message: string; receipt: C4Evidence | null }

const BLOCKED_CODES = ['REQUIRED_ENV_MISSING', 'RUNTIME_UNAVAILABLE'] as const

export function publishC4Terminal(
  terminal: C4LaneTerminal,
  input: {
    startedAt: string
    endedAt: string
    frontendOrigin: string
    apiOrigin: string
    receiptPath: string
  },
  publication: C4TerminalPublication,
): C4TerminalResult {
  const failure = terminal.failure
  if (failure === null) {
    return { ok: false, code: 'NO_FAILURE', message: 'terminal has no failure classification' }
  }
  const blocked = (BLOCKED_CODES as readonly string[]).includes(failure.code)
  const receipt = buildC4TerminalEvidence({
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: blocked ? null : input.frontendOrigin,
    apiOrigin: blocked ? null : input.apiOrigin,
    requestedPins: blocked ? null : terminal.requestedPins,
    gfs: blocked ? null : terminal.gfs,
    ifs: blocked ? null : terminal.ifs,
    home: blocked ? null : terminal.home,
    opsGfs: blocked ? null : terminal.opsGfs,
    opsIfs: blocked ? null : terminal.opsIfs,
    noControl: blocked ? null : terminal.noControl,
    failure,
  })
  if (!receipt.ok) {
    return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: receipt.reason }
  }
  try {
    publication.publish(input.receiptPath, receipt.receipt)
  } catch (error) {
    return {
      ok: false,
      code: error instanceof Error && 'code' in error ? String((error as { code: unknown }).code) : 'PUBLICATION_FAILED',
      message: 'C4 terminal publication failed',
    }
  }
  return { ok: true, receipt: receipt.receipt }
}

export interface C4PassPublicationInput {
  startedAt: string
  endedAt: string
  frontendOrigin: string
  apiOrigin: string
  receiptPath: string
  requestedPins: NonNullable<C4LaneTerminal['requestedPins']>
  gfs: NonNullable<C4LaneTerminal['gfs']>
  ifs: NonNullable<C4LaneTerminal['ifs']>
  home: NonNullable<C4LaneTerminal['home']>
  opsGfs: NonNullable<C4LaneTerminal['opsGfs']>
  opsIfs: NonNullable<C4LaneTerminal['opsIfs']>
  noControl: NonNullable<C4LaneTerminal['noControl']>
}

export interface C4PassBuildHooks {
  build?: (input: C4PassInput) => C4BuildResult
}

export function publishC4Pass(
  terminal: C4LaneTerminal,
  input: C4PassPublicationInput,
  publication: C4TerminalPublication,
  wholeDeadline: RiverClickDeadline | null = null,
  hooks: C4PassBuildHooks = {},
): C4PassResult {
  if (wholeDeadline !== null && wholeDeadline.expired()) {
    const receipt = buildC4TerminalEvidence({
      startedAt: input.startedAt,
      endedAt: input.endedAt,
      frontendOrigin: input.frontendOrigin,
      apiOrigin: input.apiOrigin,
      requestedPins: input.requestedPins,
      gfs: input.gfs,
      ifs: input.ifs,
      home: input.home,
      opsGfs: input.opsGfs,
      opsIfs: input.opsIfs,
      noControl: input.noControl,
      failure: { code: 'WHOLE_RUN_TIMEOUT', stage: 'timeout', message: 'whole-run deadline exceeded before PASS construction' },
    })
    if (!receipt.ok) return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: receipt.reason, receipt: null }
    try {
      publication.publish(input.receiptPath, receipt.receipt)
    } catch {
      return { ok: false, code: 'PUBLICATION_FAILED', message: 'C4 terminal publication failed', receipt: null }
    }
    return { ok: false, code: 'WHOLE_RUN_TIMEOUT', message: receipt.receipt.failure?.message ?? 'whole-run deadline exceeded before PASS construction', receipt: receipt.receipt }
  }
  const build = hooks.build ?? buildC4PassEvidence
  const passInput: C4PassInput = {
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: input.frontendOrigin,
    apiOrigin: input.apiOrigin,
    requestedPins: input.requestedPins,
    gfs: input.gfs,
    ifs: input.ifs,
    home: input.home,
    opsGfs: input.opsGfs,
    opsIfs: input.opsIfs,
    noControl: input.noControl,
  }
  const built = build(passInput)
  if (!built.ok) return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: built.reason, receipt: null }
  if (wholeDeadline !== null && wholeDeadline.expired()) {
    const receipt = buildC4TerminalEvidence({
      startedAt: input.startedAt,
      endedAt: input.endedAt,
      frontendOrigin: input.frontendOrigin,
      apiOrigin: input.apiOrigin,
      requestedPins: input.requestedPins,
      gfs: input.gfs,
      ifs: input.ifs,
      home: input.home,
      opsGfs: input.opsGfs,
      opsIfs: input.opsIfs,
      noControl: input.noControl,
      failure: { code: 'WHOLE_RUN_TIMEOUT', stage: 'timeout', message: 'whole-run deadline exceeded after PASS construction' },
    })
    if (!receipt.ok) return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: receipt.reason, receipt: null }
    try {
      publication.publish(input.receiptPath, receipt.receipt)
    } catch {
      return { ok: false, code: 'PUBLICATION_FAILED', message: 'C4 terminal publication failed', receipt: null }
    }
    return { ok: false, code: 'WHOLE_RUN_TIMEOUT', message: receipt.receipt.failure?.message ?? 'whole-run deadline exceeded after PASS construction', receipt: receipt.receipt }
  }
  try {
    publication.publish(input.receiptPath, built.receipt)
  } catch {
    return { ok: false, code: 'PUBLICATION_FAILED', message: 'C4 PASS publication failed', receipt: null }
  }
  return { ok: true, receipt: built.receipt }
}
