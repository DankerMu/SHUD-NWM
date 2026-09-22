## ADDED Requirements

### Requirement: Completed-type terminal skips SHALL yield to a newer failure truth

The candidate decision SHALL classify a candidate as `terminal_pipeline_success` or `terminal_completed_cycle` only when the success truth backing that classification is at least as new as the candidate's latest failure truth. This is the same rule `terminal_hydro_success` already applies. Latest failure truth is the newest failed pipeline job or failure event in the candidate's decision state, excluding repaired-stage evidence and manual-retry markers as the hydro leg does. A permanence re-label SHALL NOT count as a new failure: the `permanently_failed` mark event is ignored, and a `permanently_failed` job row contributes only its submission-side time (`submitted_at`, then `created_at`), because the mark rewrites both `updated_at` and `finished_at`. When the latest failure truth is strictly newer, the candidate SHALL NOT receive either completed-type skip. It SHALL instead reach the existing failure path (`retry_failed_candidate`, subject to the missing-forcing block and the stage-scoped retry budget). As a result, no skip→forced-retry rewrite (journal-predecessor identity quarantine, `terminal_run_manifest_missing`, strict warm-start mismatch) is fed by a success that a newer failure has superseded. Equal timestamps SHALL keep the terminal classification. A success truth without a timestamp SHALL keep the pre-change classification. For the same reason, a durable-SHUD downstream resume that would restart `forecast` itself SHALL NOT be emitted when the `hydro_run` truth it would reuse is strictly older than the latest failure truth; the candidate SHALL take the failure path instead.

#### Scenario: A failed quarantine rerun is not re-read as terminal
- **GIVEN** a completed cycle with a stale recorded init-state token, no operator confirmation, and no completed quarantine-stamped master
- **WHEN** the scheduler emits `retry_journal_predecessor_identity_mismatch`, the rerun fails at `forecast`, and the scheduler runs more passes than the retry limit
- **THEN** no later pass emits a quarantine retry derived from a completed-type skip; the candidate state decides the budgeted failure path (`retry_failed_candidate`, or `permanent_failure` once the inline retry service has declined at the retry limit)
- **AND** the total number of forecast submissions does not exceed one plus the retry limit; with the production inline retry service wired, the candidate ends blocked rather than still submitting

#### Scenario: An older failure does not demote a newer success
- **WHEN** a candidate's newest terminal-success completion-stage row is newer than or equal in time to its latest failure row
- **THEN** the decision remains `terminal_pipeline_success` (or `terminal_completed_cycle`) exactly as before this change

#### Scenario: Sibling rewrite legs stop re-firing on a newer failure
- **WHEN** a failure truth is newer than the success that would have produced `terminal_run_manifest_missing` or a strict warm-start terminal mismatch retry
- **THEN** neither forced retry is emitted from the completed skip and the candidate follows the failure path

#### Scenario: A permanence re-label of an older failure does not demote a newer success
- **WHEN** a failure older than the candidate's success is later marked `permanently_failed` by a declined retry
- **THEN** the candidate remains terminal

### Requirement: §8.7 identity authority SHALL prefer the newer of the completed hydro_run and the terminal-success candidate master

The file-journal completed-pipeline init-state identity SHALL resolve as follows. Among the accepted-submit cohort MASTER rows whose identity map names the model, the scheduler SHALL consider only those that are terminal-success for that model and carry exactly one self-bound identity, and SHALL take the one with the newest `created_at`. When that master's `created_at` is strictly newer than the matching completed `hydro_run` row's `created_at`, the master's identity SHALL win. Otherwise the completed `hydro_run` row SHALL keep priority. `created_at` is the comparison key on both sides because no post-hoc write (status updates, reconciliation, permanence marks) refreshes it, while `updated_at` is rewritten by `update_hydro_run_status`. A newer running or failed rerun SHALL NOT hide an older converged success. The `hydro_run` write path SHALL remain retriable-only. The breaker occurrence count SHALL keep counting completed quarantine-stamped masters only. Every consumer of the accessor — the journal-predecessor identity quarantine and the discovery-side §8.7 scoring — SHALL observe the same resolved identity.

#### Scenario: A same-run_id corrective rerun converges
- **GIVEN** a run completed through the real hydro-run and pipeline terminal write path recording token X
- **WHEN** a same-run_id rerun with quarantine provenance completes and records the correct token Y
- **THEN** the resolved identity is Y, the journal-predecessor identity quarantine returns no decision, and the breaker does not engage

#### Scenario: A rerun that records the stale token still fail-stops
- **WHEN** the same-run_id rerun completes and records the stale token X again
- **THEN** the breaker engages exactly as before this change

#### Scenario: A later failed rerun does not undo a converged identity
- **WHEN** a corrective rerun records Y and completes, and a later rerun for the same model fails
- **THEN** the resolved identity remains Y

#### Scenario: Legacy hydro_run-only identity is unchanged
- **WHEN** no accepted-submit candidate master exists and only the completed `hydro_run` row carries an identity
- **THEN** the resolved identity equals that row's identity
