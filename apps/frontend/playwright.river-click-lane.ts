/**
 * Whole-lane orchestrator part of the browser-side no-mock river-click P95 lane
 * (#1970): install the pre-start flag, run the bounded preflight, wait for the
 * gated hook, run one discarded warmup then exactly 20 serial accepted samples,
 * and compute the nearest-rank P95. The bounded preflight and one-attempt
 * observer live in the sibling modules; this file re-exports the shared types.
 */

import {
  RIVER_CLICK_ACCEPTED_SAMPLES,
  RIVER_CLICK_PER_SAMPLE_DEADLINE_MS,
  RIVER_CLICK_WHOLE_RUN_DEADLINE_MS,
} from './src/lib/riverClickEvidence/constants'
import type { RiverClickConfig } from './src/lib/riverClickEvidence/config'
import { createRiverClickDeadline, withRiverClickDeadline, type RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import { nearestRankP95, validateRiverClickDurations } from './src/lib/riverClickEvidence/timing'
import type { RiverClickFeatureIdentity, RiverClickFailure, RiverClickSample } from './src/lib/riverClickEvidence/receipt'
import {
  failureOf,
  boundedMessage,
  resolveRiverClickIdentity,
  TIMEOUT_SENTINEL,
  type RiverClickFailureShape,
  type RiverClickLaneEnv,
  type RiverClickLaneIdentity,
} from './playwright.river-click-lane-preflight'
import { runRiverClickAttempt, type RiverClickAttemptOptions } from './playwright.river-click-lane-attempt'

export {
  failureOf,
  boundedMessage,
  readResponseBounded,
  resolveRiverClickIdentity,
  TIMEOUT_SENTINEL,
} from './playwright.river-click-lane-preflight'
export type {
  RiverClickLaneBrowserRequest,
  RiverClickLaneBrowserResponse,
  RiverClickLanePageSurface,
  RiverClickLaneEnv,
  RiverClickLaneIdentity,
  RiverClickFailureShape,
} from './playwright.river-click-lane-preflight'
export type { RiverClickAttemptOptions, RiverClickAttemptResult } from './playwright.river-click-lane-attempt'
export { runRiverClickAttempt } from './playwright.river-click-lane-attempt'

export interface RiverClickLaneTerminal {
  failure: RiverClickFailure | null
  requestedFeature: RiverClickFeatureIdentity | null
  renderedFeature: RiverClickFeatureIdentity | null
  gfs: RiverClickProductIdentityOrNull
  ifs: RiverClickProductIdentityOrNull
  warmup: { index: 0; durationMs: number; gfsStatus: number; ifsStatus: number } | null
  samples: RiverClickSample[]
  p95Ms: number | null
}

type RiverClickProductIdentityOrNull = import('./src/lib/riverClickEvidence/receipt').RiverClickProductIdentity | null

export type RiverClickLaneResult =
  | { ok: true; terminal: RiverClickLaneTerminal }
  | { ok: false; terminal: RiverClickLaneTerminal }

export interface RiverClickLaneOptions {
  deadlineMs?: number
  deadline?: RiverClickDeadline
  attemptDeadlineMs?: number
  mapDeadlineMs?: number
  pollMs?: number
  quietMs?: number
  now?: () => number
}

/** Terminal wrapper for a closed lane failure. */
export function laneFailure(
  code: RiverClickFailureShape['code'],
  stage: RiverClickFailureShape['stage'],
  message: string,
  sampleIndex: number | null = null,
  gfsStatus: number | null = null,
  ifsStatus: number | null = null,
): RiverClickLaneTerminal {
  return {
    failure: failureOf(code, stage, message, sampleIndex, gfsStatus, ifsStatus),
    requestedFeature: null,
    renderedFeature: null,
    gfs: null,
    ifs: null,
    warmup: null,
    samples: [],
    p95Ms: null,
  }
}

function boundedRace<T>(promise: Promise<T>, deadline: RiverClickDeadline): Promise<T> {
  return withRiverClickDeadline(
    promise,
    deadline,
    () => {
      throw new Error('river-click deadline exceeded')
    },
  )
}

/** Whole-run orchestrator. */
export async function runRiverClickLane(
  env: RiverClickLaneEnv,
  fetchImpl: (url: string, init: RequestInit) => Promise<Response>,
  options: RiverClickLaneOptions = {},
): Promise<RiverClickLaneResult> {
  const clock = options.now ?? (() => performance.now())
  const deadlineMs = options.deadlineMs ?? RIVER_CLICK_WHOLE_RUN_DEADLINE_MS
  const attemptDeadlineMs = options.attemptDeadlineMs ?? RIVER_CLICK_PER_SAMPLE_DEADLINE_MS
  const mapDeadlineMs = options.mapDeadlineMs ?? RIVER_CLICK_PER_SAMPLE_DEADLINE_MS
  // ONE absolute whole-run deadline created immediately before preflight; an
  // externally supplied deadline object is used verbatim so the caller and the
  // lane share the same monotonic instant through receipt construction.
  const wholeDeadline = options.deadline ?? createRiverClickDeadline(deadlineMs, clock)

  const samples: RiverClickSample[] = []
  const warmups: Array<{ index: 0; durationMs: number; gfsStatus: number; ifsStatus: number }> = []
  const terminalCarrier: { terminal: RiverClickLaneTerminal | null } = { terminal: null }

  const terminalForFailure = (
    failure: RiverClickFailureShape,
    renderedFeature: RiverClickFeatureIdentity | null,
    sampleIndex: number | null,
    stage: RiverClickFailureShape['stage'],
  ): RiverClickLaneTerminal => {
    const terminal = laneFailure(failure.code, stage, failure.message, sampleIndex, failure.gfsStatus, failure.ifsStatus)
    terminal.requestedFeature = identity?.requestedFeature ?? null
    terminal.renderedFeature = renderedFeature
    terminal.gfs = identity?.gfs ?? null
    terminal.ifs = identity?.ifs ?? null
    terminal.warmup = warmups[0] ?? null
    terminal.samples = [...samples]
    return terminal
  }

  let identity: RiverClickLaneIdentity | null = null
  let renderedAnchor: RiverClickFeatureIdentity | null = null

  try {
    const resolved = await resolveRiverClickIdentity(env.config, fetchImpl, wholeDeadline)
    if (!resolved.ok) {
      return {
        ok: false,
        terminal: laneFailure(
          resolved.failure.code,
          resolved.failure.stage,
          resolved.failure.message,
          resolved.failure.sampleIndex,
          resolved.failure.gfsStatus,
          resolved.failure.ifsStatus,
        ),
      }
    }
    identity = resolved.identity

    try {
      await boundedRace(env.page.addInitScript(`window.__NHMS_E2E_HOOKS__ = true`), wholeDeadline)
    } catch {
      return { ok: false, terminal: laneFailure('HOOK_PREREQUISITE_MISSING', 'runtime', 'unable to install the pre-start hook flag') }
    }
    try {
      await boundedRace(env.page.goto('/'), wholeDeadline)
    } catch {
      // A goto interrupted by whole-deadline expiry (or any navigation failure
      // after the deadline is spent) is a WHOLE_RUN_TIMEOUT, never a hook
      // selection failure: the hook never got a chance because the run's one
      // absolute budget ran out during navigation.
      if (wholeDeadline.expired()) {
        return {
          ok: false,
          terminal: terminalForFailure(
            failureOf('WHOLE_RUN_TIMEOUT', 'map', 'whole-run deadline exceeded during page navigation'),
            null,
            null,
            'map',
          ),
        }
      }
      return {
        ok: false,
        terminal: terminalForFailure(
          failureOf('HOOK_SELECTION_FAILED', 'map', 'page navigation failed before hook availability'),
          null,
          null,
          'map',
        ),
      }
    }

    // Hook wait is clipped to the earlier whole-run absolute deadline.
    const clippedBudget = Math.min(mapDeadlineMs, wholeDeadline.remaining())
    const hookDeadline = createRiverClickDeadline(clippedBudget, clock)
    let hookReady = false
    while (!hookReady) {
      const raw = await withRiverClickDeadline(
        env.page.evaluate<boolean>(
          `Boolean(window.__nhmsRiverClickEvidence && typeof window.__nhmsRiverClickEvidence.selectRenderedRiver === 'function')`,
        ),
        hookDeadline,
        () => false,
      ).catch(() => false)
      if (raw) {
        hookReady = true
        break
      }
      if (hookDeadline.expired() || wholeDeadline.expired()) {
        break
      }
      const remaining = Math.min(100, hookDeadline.remaining())
      await env.page.waitForTimeout(Math.max(0, remaining))
    }
    if (!hookReady) {
      if (wholeDeadline.expired()) {
        return {
          ok: false,
          terminal: terminalForFailure(failureOf('WHOLE_RUN_TIMEOUT', 'map', 'whole-run deadline exceeded before hook availability'), null, null, 'map'),
        }
      }
      return {
        ok: false,
        terminal: terminalForFailure(failureOf('HOOK_SELECTION_FAILED', 'map', 'gated hook never became available after page load'), null, null, 'map'),
      }
    }

    const attemptOptions: RiverClickAttemptOptions = {
      attemptDeadlineMs,
      pollMs: options.pollMs ?? 100,
      quietMs: options.quietMs ?? 250,
      wholeDeadline,
      now: clock,
    }

    const runOne = async (index: number, accept: boolean): Promise<boolean> => {
      const expected = renderedAnchor ?? identity!.requestedFeature
      const attempt = await runRiverClickAttempt(env, identity!, expected, attemptOptions)
      if (!attempt.ok) {
        // Warmup failures use stage warmup/sample_index 0; accepted failures use
        // the actual 1..20 index (never count an attempt that failed mid-way,
        // including during quiet). The hook-returned rendered identity (if
        // selection succeeded) is preserved on the terminal.
        const stage: RiverClickFailureShape['stage'] = accept ? 'sample' : 'warmup'
        const sampleIndex = accept ? index : 0
        const rendered = attempt.rendered ?? renderedAnchor
        terminalCarrier.terminal = terminalForFailure(attempt.failure, rendered, sampleIndex, stage)
        if (rendered !== null && renderedAnchor === null) {
          renderedAnchor = rendered
        }
        return false
      }
      const rendered = attempt.rendered
      // Compare to BOTH the requested identity and the first actual identity.
      if (
        rendered.basinId !== identity!.requestedFeature.basinId ||
        rendered.riverSegmentId !== identity!.requestedFeature.riverSegmentId ||
        rendered.basinVersionId !== identity!.requestedFeature.basinVersionId ||
        rendered.riverNetworkVersionId !== identity!.requestedFeature.riverNetworkVersionId
      ) {
        terminalCarrier.terminal = terminalForFailure(
          failureOf('IDENTITY_DRIFT', 'sample', 'rendered feature identity drifted from requested preflight identity'),
          renderedAnchor,
          index,
          'sample',
        )
        return false
      }
      if (renderedAnchor !== null && (
        rendered.basinId !== renderedAnchor.basinId ||
        rendered.riverSegmentId !== renderedAnchor.riverSegmentId ||
        rendered.basinVersionId !== renderedAnchor.basinVersionId ||
        rendered.riverNetworkVersionId !== renderedAnchor.riverNetworkVersionId
      )) {
        terminalCarrier.terminal = terminalForFailure(
          failureOf('IDENTITY_DRIFT', 'sample', 'rendered feature identity drifted across samples'),
          renderedAnchor,
          index,
          'sample',
        )
        return false
      }
      if (renderedAnchor === null) {
        renderedAnchor = rendered
      }
      const durationMs = attempt.t1Ms - attempt.t0Ms
      if (!Number.isFinite(durationMs) || durationMs < 0) {
        terminalCarrier.terminal = terminalForFailure(
          failureOf('TIMING_INVALID', 'sample', 't1 - t0 is not a finite non-negative duration'),
          renderedAnchor,
          index,
          'sample',
        )
        return false
      }
      if (accept) {
        samples.push({ index: samples.length + 1, durationMs, gfsStatus: attempt.gfsStatus, ifsStatus: attempt.ifsStatus })
      } else {
        warmups.push({ index: 0, durationMs, gfsStatus: attempt.gfsStatus, ifsStatus: attempt.ifsStatus })
      }
      return true
    }

    if (wholeDeadline.expired()) {
      return {
        ok: false,
        terminal: terminalForFailure(failureOf('WHOLE_RUN_TIMEOUT', 'warmup', 'whole-run deadline exceeded before warmup'), null, null, 'warmup'),
      }
    }
    const warmupOk = await runOne(0, false)
    if (!warmupOk) {
      return { ok: false, terminal: terminalCarrier.terminal as RiverClickLaneTerminal }
    }

    for (let index = 1; index <= RIVER_CLICK_ACCEPTED_SAMPLES; index += 1) {
      if (wholeDeadline.expired()) {
        return {
          ok: false,
          terminal: terminalForFailure(failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded between samples'), renderedAnchor, index, 'sample'),
        }
      }
      const ok = await runOne(index, true)
      if (!ok) {
        return { ok: false, terminal: terminalCarrier.terminal as RiverClickLaneTerminal }
      }
    }

    const durations = samples.map((sample) => sample.durationMs)
    const validation = validateRiverClickDurations(durations)
    if (!validation.ok) {
      return {
        ok: false,
        terminal: terminalForFailure(failureOf('TIMING_INVALID', 'threshold', validation.reason), renderedAnchor, null, 'threshold'),
      }
    }
    const p95 = nearestRankP95(durations) as number
    if (p95 >= 2000) {
      const terminal: RiverClickLaneTerminal = {
        failure: failureOf('THRESHOLD_EXCEEDED', 'threshold', `nearest-rank P95 ${p95} >= 2000 ms`),
        requestedFeature: identity.requestedFeature,
        renderedFeature: renderedAnchor,
        gfs: identity.gfs,
        ifs: identity.ifs,
        warmup: warmups[0] ?? null,
        samples,
        p95Ms: p95,
      }
      return { ok: false, terminal }
    }

    const terminalPass: RiverClickLaneTerminal = {
      failure: null,
      requestedFeature: identity.requestedFeature,
      renderedFeature: renderedAnchor,
      gfs: identity.gfs,
      ifs: identity.ifs,
      warmup: warmups[0] ?? null,
      samples,
      p95Ms: p95,
    }
    return { ok: true, terminal: terminalPass }
  } catch (error) {
    // Any unexpected lane exception becomes a bounded INTERNAL_ERROR terminal
    // carrying whatever completed evidence was accumulated (never raw text).
    const terminal = laneFailure(
      'INTERNAL_ERROR',
      'sample',
      'river-click lane failed internally',
    )
    if (identity !== null) {
      terminal.requestedFeature = identity.requestedFeature
      terminal.renderedFeature = renderedAnchor
      terminal.gfs = identity.gfs
      terminal.ifs = identity.ifs
      terminal.warmup = warmups[0] ?? null
      terminal.samples = [...samples]
    }
    return { ok: false, terminal }
  }
}

/** Re-export for callers that only need the preflight type. */
export type { RiverClickConfig }
