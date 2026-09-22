## ADDED Requirements

### Requirement: Completed-type terminal skips SHALL yield to a newer failure truth

The candidate decision SHALL classify a candidate as `terminal_pipeline_success` or `terminal_completed_cycle` only when the success truth backing that classification is at least as new as the candidate's latest failure truth. This is the same rule `terminal_hydro_success` already applies. Latest failure truth is the newest failed pipeline job or failure event in the candidate's decision state, excluding repaired-stage evidence and manual-retry markers, exactly as the hydro leg computes it. When the latest failure truth is strictly newer, the candidate SHALL NOT receive either completed-type skip. It SHALL instead reach the existing failure path (`retry_failed_candidate`, subject to the missing-forcing block and the stage-scoped retry budget). As a result, no skip→forced-retry rewrite (journal-predecessor identity quarantine, `terminal_run_manifest_missing`, strict warm-start mismatch) is fed by a success that a newer failure has superseded. Equal timestamps SHALL keep the terminal classification. A success truth without a timestamp SHALL keep the pre-change classification.

#### Scenario: A failed quarantine rerun is not re-read as terminal
- **GIVEN** a completed cycle with a stale recorded init-state token, no operator confirmation, and no completed quarantine-stamped master
- **WHEN** the scheduler emits `retry_journal_predecessor_identity_mismatch`, the rerun fails at `forecast`, and the scheduler runs more passes than the retry limit
- **THEN** the pass after the failure decides `retry_failed_candidate` rather than a completed-type skip rewritten into a quarantine retry
- **AND** the total number of forecast submissions does not exceed the retry budget, and the candidate ends blocked by budget rather than still submitting

#### Scenario: An older failure does not demote a newer success
- **WHEN** a candidate's newest terminal-success completion-stage row is newer than or equal in time to its latest failure row
- **THEN** the decision remains `terminal_pipeline_success` (or `terminal_completed_cycle`) exactly as before this change

#### Scenario: Sibling rewrite legs stop re-firing on a newer failure
- **WHEN** a failure truth is newer than the success that would have produced `terminal_run_manifest_missing`
- **THEN** no `terminal_run_manifest_missing` forced retry is emitted and the candidate follows the failure path

### Requirement: §8.7 identity authority SHALL prefer the newer of the completed hydro_run and the terminal-success candidate master

The file-journal completed-pipeline init-state identity SHALL resolve as follows. When a terminal-success accepted-submit candidate master carrying a self-bound identity is strictly newer than the matching completed `hydro_run` row, the master's identity SHALL win. Otherwise the completed `hydro_run` row SHALL keep priority. The `hydro_run` side of the comparison SHALL use a time that a same-run_id rerun does not refresh. The `hydro_run` write path SHALL remain retriable-only. The breaker occurrence count SHALL keep counting completed quarantine-stamped masters only. Every consumer of the accessor — the journal-predecessor identity quarantine and the discovery-side §8.7 scoring — SHALL observe the same resolved identity.

#### Scenario: A same-run_id corrective rerun converges
- **GIVEN** a run completed through the real hydro-run and pipeline terminal write path recording token X
- **WHEN** a same-run_id rerun with quarantine provenance completes and records the correct token Y
- **THEN** the resolved identity is Y, the journal-predecessor identity quarantine returns no decision, and the breaker does not engage

#### Scenario: A rerun that records the stale token still fail-stops
- **WHEN** the same-run_id rerun completes and records the stale token X again
- **THEN** the breaker engages exactly as before this change

#### Scenario: Legacy hydro_run-only identity is unchanged
- **WHEN** no accepted-submit candidate master exists and only the completed `hydro_run` row carries an identity
- **THEN** the resolved identity equals that row's identity
