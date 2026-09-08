/**
 * Real Playwright globalSetup for the dedicated C4 profile (#1895).
 *
 * Runs BEFORE any browser fixture or worker is launched. It is NOT the river
 * globalSetup; this owner uses C4 env keys and the C4 publisher only.
 */

import { runC4LiveEvidenceOwner } from './playwright.c4-display-evidence-owner'
import { publishC4Evidence } from './playwright.c4-display-evidence'

export default async function globalSetup(): Promise<void> {
  const startedAt = new Date().toISOString()
  const result = await runC4LiveEvidenceOwner(process.env, {
    publish: (receiptPath, receipt) => publishC4Evidence(receiptPath, receipt),
  }, startedAt)
  if (!result.ok) {
    process.stderr.write(
      `${result.classification}: ${result.code}: ${result.message}${result.receiptWritten ? ' (receipt published)' : ''}\n`,
    )
    throw new Error(`${result.classification}: ${result.code}: ${result.message}`)
  }
}
