import { lstatSync, statSync, realpathSync, openSync, fstatSync, fchmodSync, writeSync, fsyncSync, closeSync, linkSync, unlinkSync, readSync } from 'node:fs'
import { test } from '@playwright/test'

import { parseRiverClickConfig } from '../src/lib/riverClickEvidence/config'
import { createRiverClickDeadline } from '../src/lib/riverClickEvidence/deadline'
import { RIVER_CLICK_WHOLE_RUN_DEADLINE_MS } from '../src/lib/riverClickEvidence/constants'
import { buildRiverClickTerminalEvidence } from '../src/lib/riverClickEvidence/receipt'
import { runRiverClickLane, type RiverClickLanePageSurface, type RiverClickLaneTerminal } from '../playwright.river-click-lane'
import { publishRiverClickPass, publishRiverClickTerminal } from '../playwright.river-click-terminal'
import {
  publishRiverClickEvidence,
  RiverClickPublicationError,
  validateRiverClickReceiptPath,
} from '../playwright.river-click-evidence'

test.describe('live display_readonly evidence', () => {
  test('river-click one warmup plus 20 serial GFS+IFS clicks stay below the P95 gate @live-river-click', async ({ page }) => {
    test.setTimeout(390_000)
    const startedAt = new Date().toISOString()
    // The pre-browser owner/globalSetup already ran (before any browser
    // fixture) and either aborted with a published BLOCKED/FAIL receipt or
    // validated the full config. Here we parse/vet the already-vetted config
    // again for the lane + terminal publication path (no second owner call).
    const parsed = parseRiverClickConfig(process.env)
    if (!parsed.ok) {
      throw new Error(`${parsed.classification}: ${parsed.message}`)
    }
    const receiptPreflight = validateRiverClickReceiptPath(parsed.config.receiptPath, {
      lstatSync,
      statSync,
      realpathSync,
      openSync,
      fstatSync,
      fchmodSync,
      writeSync,
      fsyncSync,
      closeSync,
      linkSync,
      unlinkSync,
      readSync,
    })
    if (!receiptPreflight.ok) {
      process.stderr.write(`BLOCKED: river-click evidence receipt path unsafe: ${receiptPreflight.message}\n`)
      throw new Error(`BLOCKED: river-click evidence receipt path unsafe: ${receiptPreflight.message}`)
    }

    // ONE absolute whole-run deadline created immediately before preflight; it
    // is threaded through the lane and enforced here through receipt
    // construction. Playwright's own test timeout adds the publication margin.
    const wholeDeadline = createRiverClickDeadline(RIVER_CLICK_WHOLE_RUN_DEADLINE_MS)

    // Exactly-one-publication discipline: `publicationAttempted` flips BEFORE
    // the first publish call and the catch NEVER publishes again; a failed
    // publication is terminal (no retry, no INTERNAL_ERROR second write).
    let publicationAttempted = false
    let accumulatedTerminal: RiverClickLaneTerminal | null = null
    try {
      // Node-side preflight fetch for the identity endpoints; it runs before any
      // page work on the monotonic whole-deadline (no redirects, credentials
      // omitted, bounded by the owner lane).
      const fetchImpl = (url: string, init: RequestInit): Promise<Response> => fetch(url, init)
      const result = await runRiverClickLane(
        { config: parsed.config, page: page as unknown as RiverClickLanePageSurface },
        fetchImpl,
        { deadline: wholeDeadline },
      )
      const endedAt = new Date().toISOString()
      if (!result.ok) {
        accumulatedTerminal = result.terminal
        if (result.terminal.failure === null) {
          throw new Error('river-click lane terminal has no failure classification')
        }
        // One tested executable terminal owner: builds + publishes; BLOCKED
        // normalized to all-null claims, FAIL preserves known origins/evidence.
        const terminalOutcome = publishRiverClickTerminal(result.terminal, {
          startedAt,
          endedAt,
          frontendOrigin: parsed.config.frontendOrigin,
          apiOrigin: parsed.config.apiOrigin,
          receiptPath: parsed.config.receiptPath,
        }, {
          publish: (receiptPath, receipt) => {
            publicationAttempted = true
            return publishRiverClickEvidence(receiptPath, receipt)
          },
        })
        if (!terminalOutcome.ok) {
          // The single publication attempt failed: never publish again.
          throw new Error(`terminal receipt publish failed: ${terminalOutcome.code}: ${terminalOutcome.message}`)
        }
        throw new Error(`river-click lane terminal: ${result.terminal.failure.code} at ${result.terminal.failure.stage}`)
      }

      // Narrow the terminal non-null fields before building a PASS receipt:
      // no casts. A PASS terminal guarantees non-null identities by the lane
      // contract; validate them here so a violated contract fails closed.
      const terminal = result.terminal
      if (
        terminal.requestedFeature === null ||
        terminal.renderedFeature === null ||
        terminal.gfs === null ||
        terminal.ifs === null
      ) {
        throw new Error('river-click PASS terminal is missing a non-null identity')
      }
      // One tested PASS publisher enforces the SAME absolute deadline BEFORE and
      // AFTER PASS construction: expiry during construction is exactly one
      // WHOLE_RUN_TIMEOUT FAIL with the completed 1+20 evidence, never a stale
      // PASS, and publication stays outside the 360s budget (Playwright test
      // timeout). Exactly one publication attempt.
      const passOutcome = publishRiverClickPass(terminal, {
        startedAt,
        endedAt,
        frontendOrigin: parsed.config.frontendOrigin,
        apiOrigin: parsed.config.apiOrigin,
        receiptPath: parsed.config.receiptPath,
        requestedFeature: terminal.requestedFeature,
        renderedFeature: terminal.renderedFeature,
        gfs: terminal.gfs,
        ifs: terminal.ifs,
        warmup: terminal.warmup,
        samples: terminal.samples,
      }, {
        publish: (receiptPath, receipt) => {
          publicationAttempted = true
          return publishRiverClickEvidence(receiptPath, receipt)
        },
      }, wholeDeadline)
      if (!passOutcome.ok) {
        // ok:true means ONLY a PASS receipt was published: a published
        // WHOLE_RUN_TIMEOUT FAIL (or a build/publication failure) is ok:false
        // and MUST abort nonzero. publicationAttempted is already true, so the
        // catch below never republishes.
        throw new Error(`river-click PASS publisher failed: ${passOutcome.code}: ${passOutcome.message}`)
      }
    } catch (error) {
      // Any unexpected lane/build failure with a safe receipt path may publish
      // at most ONE honest terminal before the throw — but NEVER after a
      // publication attempt already started (publisher failures get no retry).
      if (!publicationAttempted && receiptPreflight.ok) {
        try {
          if (accumulatedTerminal !== null && accumulatedTerminal.failure !== null) {
            // An owned terminal exists and NO publication has started: publish it
            // through the SAME terminal owner (BLOCKED normalized to all-null,
            // FAIL preserves evidence), never relabel as INTERNAL_ERROR.
            const outcome = publishRiverClickTerminal(accumulatedTerminal, {
              startedAt,
              endedAt: new Date().toISOString(),
              frontendOrigin: parsed.config.frontendOrigin,
              apiOrigin: parsed.config.apiOrigin,
              receiptPath: parsed.config.receiptPath,
            }, {
              publish: (receiptPath, receipt) => publishRiverClickEvidence(receiptPath, receipt),
            })
            if (!outcome.ok) {
              process.stderr.write(`BLOCKED: river-click evidence publication failed: ${outcome.code}: ${outcome.message}\n`)
            }
          } else {
            // A build failure BEFORE any publication attempt may build exactly
            // one fixed INTERNAL_ERROR fallback receipt (no owned terminal, no
            // retry after it starts).
            const failure = {
              code: 'INTERNAL_ERROR' as const,
              stage: 'sample' as const,
              sampleIndex: null,
              gfsStatus: null,
              ifsStatus: null,
              message: 'river-click lane failed before an owned terminal was produced',
            }
            const terminalDoc = buildRiverClickTerminalEvidence({
              startedAt,
              endedAt: new Date().toISOString(),
              frontendOrigin: null,
              apiOrigin: null,
              failure,
            })
            if (terminalDoc.ok) {
              publishRiverClickEvidence(parsed.config.receiptPath, terminalDoc.receipt)
            }
          }
        } catch (publicationError) {
          process.stderr.write(`BLOCKED: river-click evidence publication failed: ${publicationError instanceof RiverClickPublicationError ? publicationError.message : 'unexpected'}\n`)
        }
      }
      throw error
    }
  })
})
