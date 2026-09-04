/**
 * Lane-result -> receipt -> publish decision for the river-click live lane
 * (#1970). The page-fixture test calls this instead of inlining the builder/
 * publisher; it is directly unit-testable with the real builder + real
 * publisher.
 *
 * Classification:
 * - BLOCKED failure code (REQUIRED_ENV_MISSING / RUNTIME_UNAVAILABLE /
 *   HOOK_PREREQUISITE_MISSING): normalized to all-null claims — origins null,
 *   identities null, warmup null, samples empty, p95 null. Never carries a
 *   known origin (schema requires BLOCKED origins null; a BLOCKED receipt with
 *   origins would fail validation and get relabeled).
 * - FAIL failure code: preserves known normalized origins and the actual
 *   partial terminal evidence (rendered identity on drift, warmup, samples).
 * - THRESHOLD_EXCEEDED: the completed shape (1+20 + p95>=2000) with the same
 *   non-null identity/warmup a PASS would carry.
 * - Publication error is terminal; exactly one publication attempt.
 */

import {
  buildRiverClickPassEvidence,
  buildRiverClickTerminalEvidence,
  type RiverClickBuildResult,
  type RiverClickEvidence,
  type RiverClickFeatureIdentity,
  type RiverClickPassInput,
  type RiverClickProductIdentity,
  type RiverClickSample,
} from './src/lib/riverClickEvidence/receipt'
import type { RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import type { RiverClickLaneTerminal } from './playwright.river-click-lane'

export interface RiverClickTerminalPublication {
  publish(path: string, receipt: RiverClickEvidence): { path: string }
}

export type RiverClickTerminalResult =
  | { ok: true; receipt: RiverClickEvidence }
  | { ok: false; code: string; message: string }

/**
 * PASS publication result. SEMANTIC DISTINCTION (documented at both helpers):
 * - `publishRiverClickTerminal` returns ok:true to mean the TERMINAL receipt
 *   was successfully published — the caller ALWAYS throws afterward because a
 *   lane terminal is a failure; ok:true there never means "the run passed".
 * - `publishRiverClickPass` returns ok:true ONLY when a PASS receipt was
 *   successfully published. A published WHOLE_RUN_TIMEOUT FAIL receipt (expiry
 *   before/during PASS construction) is ok:false with code WHOLE_RUN_TIMEOUT
 *   and carries the published receipt; a build/publication failure is
 *   ok:false with receipt null. Exactly one publication attempt on every path.
 */
export type RiverClickPassResult =
  | { ok: true; receipt: RiverClickEvidence }
  | {
      ok: false
      code: string
      message: string
      /** The published FAIL receipt (e.g. WHOLE_RUN_TIMEOUT with the completed
       *  1+20 evidence); null when nothing was published (build/publication
       *  failure). */
      receipt: RiverClickEvidence | null
    }

/** Narrowed PASS publication input: the spec proves non-null identities first. */
export interface RiverClickPassPublicationInput {
  startedAt: string
  endedAt: string
  frontendOrigin: string
  apiOrigin: string
  receiptPath: string
  requestedFeature: RiverClickFeatureIdentity
  renderedFeature: RiverClickFeatureIdentity
  gfs: RiverClickProductIdentity
  ifs: RiverClickProductIdentity
  warmup: { index: 0; durationMs: number; gfsStatus: number; ifsStatus: number } | null
  samples: RiverClickSample[]
}

export interface RiverClickPassBuildHooks {
  /** Override the PASS builder (test seam: the injected clock advances during
   *  construction so the AFTER check expires). */
  build?: (input: RiverClickPassInput) => RiverClickBuildResult
}

const BLOCKED_CODES = ['REQUIRED_ENV_MISSING', 'RUNTIME_UNAVAILABLE', 'HOOK_PREREQUISITE_MISSING'] as const

/**
 * Build AND publish exactly one terminal receipt for a lane result. Returns the
 * built receipt (for test inspection) or a closed failure code. Never relabels
 * a known terminal as INTERNAL_ERROR.
 */
export function publishRiverClickTerminal(
  terminal: RiverClickLaneTerminal,
  input: {
    startedAt: string
    endedAt: string
    frontendOrigin: string
    apiOrigin: string
    receiptPath: string
  },
  publication: RiverClickTerminalPublication,
): RiverClickTerminalResult {
  const failure = terminal.failure
  if (failure === null) {
    return { ok: false, code: 'NO_FAILURE', message: 'terminal has no failure classification' }
  }
  const blocked = (BLOCKED_CODES as readonly string[]).includes(failure.code)
  const receipt = buildRiverClickTerminalEvidence({
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    // BLOCKED normalized to all-null claims; FAIL preserves known origins.
    frontendOrigin: blocked ? null : input.frontendOrigin,
    apiOrigin: blocked ? null : input.apiOrigin,
    requestedFeature: blocked ? null : terminal.requestedFeature,
    renderedFeature: blocked ? null : terminal.renderedFeature,
    gfs: blocked ? null : terminal.gfs,
    ifs: blocked ? null : terminal.ifs,
    warmup: blocked ? null : terminal.warmup,
    samples: blocked ? [] : terminal.samples,
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
      message: 'river-click terminal publication failed',
    }
  }
  return { ok: true, receipt: receipt.receipt }
}

/**
 * Publish exactly one PASS receipt with the whole-deadline enforced BEFORE and
 * AFTER PASS construction. SEMANTICS: ok:true means a PASS receipt was
 * successfully published, NOTHING ELSE.
 * - expiry before construction -> published WHOLE_RUN_TIMEOUT FAIL receipt,
 *   RETURNED AS ok:false with code WHOLE_RUN_TIMEOUT + the receipt (the caller
 *   must throw nonzero — a FAIL receipt is never a successful run);
 * - expiry AFTER the builder returns but BEFORE publication -> the same
 *   ok:false WHOLE_RUN_TIMEOUT with the completed 1+20 evidence, never a stale
 *   PASS;
 * - otherwise exactly one PASS (ok:true).
 * A build or publication failure is ok:false with receipt null and NO retry.
 * The publication attempt is exactly one on every path.
 * `wholeDeadline` may be undefined in unit tests (no expiry enforced).
 */
export function publishRiverClickPass(
  terminal: RiverClickLaneTerminal,
  input: RiverClickPassPublicationInput,
  publication: RiverClickTerminalPublication,
  wholeDeadline: RiverClickDeadline | null = null,
  hooks: RiverClickPassBuildHooks = {},
): RiverClickPassResult {
  // Enforce the SAME absolute deadline BEFORE PASS construction.
  if (wholeDeadline !== null && wholeDeadline.expired()) {
    const receipt = buildRiverClickTerminalEvidence({
      startedAt: input.startedAt,
      endedAt: input.endedAt,
      frontendOrigin: input.frontendOrigin,
      apiOrigin: input.apiOrigin,
      requestedFeature: input.requestedFeature,
      renderedFeature: input.renderedFeature,
      gfs: input.gfs,
      ifs: input.ifs,
      warmup: input.warmup,
      samples: input.samples,
      failure: {
        code: 'WHOLE_RUN_TIMEOUT',
        stage: 'sample',
        sampleIndex: null,
        gfsStatus: null,
        ifsStatus: null,
        message: 'whole-run deadline exceeded before PASS construction',
      },
    })
    if (!receipt.ok) return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: receipt.reason, receipt: null }
    try {
      publication.publish(input.receiptPath, receipt.receipt)
    } catch {
      return { ok: false, code: 'PUBLICATION_FAILED', message: 'river-click terminal publication failed', receipt: null }
    }
    // A FAIL receipt was published: ok:false, closed WHOLE_RUN_TIMEOUT. The
    // caller (live spec) throws nonzero — NO receipt ever means a PASS.
    return { ok: false, code: 'WHOLE_RUN_TIMEOUT', message: receipt.receipt.failure?.message ?? 'whole-run deadline exceeded before PASS construction', receipt: receipt.receipt }
  }
  const build = hooks.build ?? buildRiverClickPassEvidence
  const passInput: RiverClickPassInput = {
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: input.frontendOrigin,
    apiOrigin: input.apiOrigin,
    requestedFeature: input.requestedFeature,
    renderedFeature: input.renderedFeature,
    gfs: input.gfs,
    ifs: input.ifs,
    warmup: input.warmup,
    samples: input.samples,
  }
  const built = build(passInput)
  if (!built.ok) return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: built.reason, receipt: null }
  // Enforce the SAME absolute deadline AFTER construction and BEFORE the PASS
  // publication: expiry during construction is never a stale PASS.
  if (wholeDeadline !== null && wholeDeadline.expired()) {
    const receipt = buildRiverClickTerminalEvidence({
      startedAt: input.startedAt,
      endedAt: input.endedAt,
      frontendOrigin: input.frontendOrigin,
      apiOrigin: input.apiOrigin,
      requestedFeature: input.requestedFeature,
      renderedFeature: input.renderedFeature,
      gfs: input.gfs,
      ifs: input.ifs,
      warmup: input.warmup,
      samples: input.samples,
      failure: {
        code: 'WHOLE_RUN_TIMEOUT',
        stage: 'sample',
        sampleIndex: null,
        gfsStatus: null,
        ifsStatus: null,
        message: 'whole-run deadline exceeded during PASS construction',
      },
    })
    if (!receipt.ok) return { ok: false, code: 'RECEIPT_BUILD_FAILED', message: receipt.reason, receipt: null }
    try {
      publication.publish(input.receiptPath, receipt.receipt)
    } catch {
      return { ok: false, code: 'PUBLICATION_FAILED', message: 'river-click terminal publication failed', receipt: null }
    }
    // A FAIL receipt was published: ok:false, closed WHOLE_RUN_TIMEOUT; never
    // a stale PASS and never a successful exit after a FAIL receipt.
    return { ok: false, code: 'WHOLE_RUN_TIMEOUT', message: receipt.receipt.failure?.message ?? 'whole-run deadline exceeded during PASS construction', receipt: receipt.receipt }
  }
  try {
    publication.publish(input.receiptPath, built.receipt)
  } catch {
    return { ok: false, code: 'PUBLICATION_FAILED', message: 'river-click terminal publication failed', receipt: null }
  }
  return { ok: true, receipt: built.receipt }
}
