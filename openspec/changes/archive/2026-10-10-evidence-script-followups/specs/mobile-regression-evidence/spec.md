## ADDED Requirements

### Requirement: Mobile evidence capture SHALL name a crashed curve region

With a device preset, when the curve window's region shows its error fallback — before or after the window frame appears — the node-27 browser-evidence script SHALL end the wait it is in at once and SHALL report the state as a failure that names the fallback, rather than as a window that did not appear or a curve that is still loading.

#### Scenario: Region crashes before the window frame appears

- **WHEN** the curve region falls into its error fallback on first render during a preset capture
- **THEN** the state fails with a message naming the fallback, without waiting for the window-appearance limit

#### Scenario: Region crashes after the window frame appeared

- **WHEN** the fallback replaces an already visible curve window during a preset capture
- **THEN** the state fails with a message naming the fallback, and no "still loading" or "frame is missing" failure is reported for it
