## ADDED Requirements

### Requirement: The gateway proof cancel stage requires an observed RUNNING state

The gateway live-proof emitter SHALL record the `submit_cancel` stage as PASS only if the long smoke job was observed in `running` before it was cancelled. If the job never reached `running` within the bounded wait, the emitter SHALL still cancel the job for cleanup, and SHALL then record the stage and the receipt as BLOCKED with `live_proof_accepted=false` and a blocker that names the last observed status.

#### Scenario: A job that stays pending is cancelled for cleanup and the stage is BLOCKED

- **WHEN** every bounded poll of the long job returns `pending`
- **THEN** a cancel request is still sent for the job
- **AND** the stage and the receipt are BLOCKED, `live_proof_accepted` is false, and the blocker contains `pending`
