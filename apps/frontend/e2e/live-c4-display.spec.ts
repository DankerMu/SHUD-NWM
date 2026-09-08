import { lstatSync, statSync, realpathSync, openSync, fstatSync, fchmodSync, writeSync, fsyncSync, closeSync, linkSync, unlinkSync, readSync } from 'node:fs'
import { test } from '@playwright/test'

import { parseC4DisplayConfig } from '../src/lib/c4DisplayEvidence/config'
import { C4_PLAYWRIGHT_TIMEOUT_MS, C4_WHOLE_RUN_DEADLINE_MS } from '../src/lib/c4DisplayEvidence/constants'
import { createRiverClickDeadline } from '../src/lib/riverClickEvidence/deadline'
import { runC4DisplayLane, type C4LanePageSurface, type C4LaneTerminal } from '../playwright.c4-display-lane'
import { publishC4Pass, publishC4Terminal } from '../playwright.c4-display-terminal'
import {
  publishC4Evidence,
  validateC4ReceiptPath,
} from '../playwright.c4-display-evidence'

test.describe('live C4 display_readonly evidence', () => {
  test('visits / and strict-identity /ops for GFS and IFS without role spoofing @live-c4-display', async ({ page }) => {
    test.setTimeout(C4_PLAYWRIGHT_TIMEOUT_MS)
    const startedAt = new Date().toISOString()
    const parsed = parseC4DisplayConfig(process.env)
    if (!parsed.ok) {
      throw new Error(`${parsed.classification}: ${parsed.message}`)
    }
    const receiptPreflight = validateC4ReceiptPath(parsed.config.receiptPath, {
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
      process.stderr.write(`BLOCKED: C4 evidence receipt path unsafe: ${receiptPreflight.message}\n`)
      throw new Error(`BLOCKED: C4 evidence receipt path unsafe: ${receiptPreflight.message}`)
    }

    const wholeDeadline = createRiverClickDeadline(C4_WHOLE_RUN_DEADLINE_MS)
    let publicationAttempted = false
    let accumulatedTerminal: C4LaneTerminal | null = null
    try {
      const fetchImpl = (url: string, init: RequestInit): Promise<Response> => fetch(url, init)
      const result = await runC4DisplayLane(
        { config: parsed.config, page: page as unknown as C4LanePageSurface },
        fetchImpl,
        { deadline: wholeDeadline },
      )
      const endedAt = new Date().toISOString()
      if (!result.ok) {
        accumulatedTerminal = result.terminal
        if (result.terminal.failure === null) {
          throw new Error('C4 lane terminal has no failure classification')
        }
        const terminalOutcome = publishC4Terminal(result.terminal, {
          startedAt,
          endedAt,
          frontendOrigin: parsed.config.frontendOrigin,
          apiOrigin: parsed.config.apiOrigin,
          receiptPath: parsed.config.receiptPath,
        }, {
          publish: (receiptPath, receipt) => {
            publicationAttempted = true
            return publishC4Evidence(receiptPath, receipt)
          },
        })
        if (!terminalOutcome.ok) {
          throw new Error(`terminal receipt publish failed: ${terminalOutcome.code}: ${terminalOutcome.message}`)
        }
        throw new Error(`C4 lane terminal: ${result.terminal.failure.code} at ${result.terminal.failure.stage}`)
      }

      const terminal = result.terminal
      if (
        terminal.requestedPins === null ||
        terminal.gfs === null ||
        terminal.ifs === null ||
        terminal.home === null ||
        terminal.opsGfs === null ||
        terminal.opsIfs === null ||
        terminal.noControl === null
      ) {
        throw new Error('C4 PASS terminal is missing a non-null identity')
      }
      const passOutcome = publishC4Pass(terminal, {
        startedAt,
        endedAt,
        frontendOrigin: parsed.config.frontendOrigin,
        apiOrigin: parsed.config.apiOrigin,
        receiptPath: parsed.config.receiptPath,
        requestedPins: terminal.requestedPins,
        gfs: terminal.gfs,
        ifs: terminal.ifs,
        home: terminal.home,
        opsGfs: terminal.opsGfs,
        opsIfs: terminal.opsIfs,
        noControl: terminal.noControl,
      }, {
        publish: (receiptPath, receipt) => {
          publicationAttempted = true
          return publishC4Evidence(receiptPath, receipt)
        },
      }, wholeDeadline)
      if (!passOutcome.ok) {
        throw new Error(`C4 PASS publication failed: ${passOutcome.code}: ${passOutcome.message}`)
      }
    } catch (error) {
      if (!publicationAttempted && receiptPreflight.ok) {
        publicationAttempted = true
        const endedAt = new Date().toISOString()
        const failureMessage = 'C4 lane internal error'
        try {
          const terminalOutcome = publishC4Terminal({
            requestedPins: accumulatedTerminal?.requestedPins ?? null,
            gfs: accumulatedTerminal?.gfs ?? null,
            ifs: accumulatedTerminal?.ifs ?? null,
            home: accumulatedTerminal?.home ?? null,
            opsGfs: accumulatedTerminal?.opsGfs ?? null,
            opsIfs: accumulatedTerminal?.opsIfs ?? null,
            noControl: accumulatedTerminal?.noControl ?? null,
            failure: accumulatedTerminal?.failure ?? {
              code: 'INTERNAL_ERROR',
              stage: 'runtime',
              message: failureMessage,
            },
          }, {
            startedAt,
            endedAt,
            frontendOrigin: parsed.config.frontendOrigin,
            apiOrigin: parsed.config.apiOrigin,
            receiptPath: parsed.config.receiptPath,
          }, {
            publish: (receiptPath, receipt) => publishC4Evidence(receiptPath, receipt),
          })
          if (!terminalOutcome.ok) {
            throw new Error(`fallback terminal receipt publish failed: ${terminalOutcome.code}`)
          }
        } catch (fallbackError) {
          if (fallbackError instanceof Error && fallbackError.message.startsWith('fallback terminal receipt publish failed:')) {
            throw fallbackError
          }
          throw new Error('fallback terminal receipt publication threw')
        }
      }
      throw error
    }
  })
})
