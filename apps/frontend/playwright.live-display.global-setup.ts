/**
 * Real Playwright globalSetup for the dedicated river-click profile (#1970).
 *
 * Runs BEFORE any browser fixture or worker is launched. It invokes the real
 * config-before-browser owner (runRiverClickLiveEvidenceOwner) with the REAL
 * publisher, so a missing/unsafe receipt path or a BLOCKED/FAIL config
 * classification is decided and (where required) ONE receipt is published
 * before any page/navigation work. Global setup failure aborts the whole run
 * before a single browser is created.
 *
 * Classification points (owner):
 * - missing/blank/unsafe receipt path -> bounded diagnostic, NO file.
 * - safe path + missing/invalid pin / malformed URL / forbidden override ->
 *   exactly one CONFIG_INVALID FAIL receipt (valid normalized origins preserved),
 *   then nonzero.
 * - safe path + missing frontend/API URL -> exactly one REQUIRED_ENV_MISSING
 *   BLOCKED receipt (all-null claims), then nonzero.
 * - fully valid config -> no receipt, setup succeeds (the lane runs later).
 */

import { runRiverClickLiveEvidenceOwner } from './playwright.river-click-evidence-owner'
import { publishRiverClickEvidence } from './playwright.river-click-evidence'

export default async function globalSetup(): Promise<void> {
  const startedAt = new Date().toISOString()
  const result = await runRiverClickLiveEvidenceOwner(process.env, {
    publish: (receiptPath, receipt) => publishRiverClickEvidence(receiptPath, receipt),
  }, startedAt)
  if (!result.ok) {
    // Bounded diagnostic to stderr with the FROZEN status prefix
    // (BLOCKED/FAIL) then the closed bounded code + message. Nonzero abort =
    // no browser work. The owner already published the required receipt when a
    // safe path existed; a publisher failure is terminal (no retry).
    process.stderr.write(
      `${result.classification}: ${result.code}: ${result.message}${result.receiptWritten ? ' (receipt published)' : ''}\n`,
    )
    throw new Error(`${result.classification}: ${result.code}: ${result.message}`)
  }
}
