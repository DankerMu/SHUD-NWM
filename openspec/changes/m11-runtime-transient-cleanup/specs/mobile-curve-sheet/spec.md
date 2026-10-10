## ADDED Requirements

### Requirement: A curve panel that never rendered SHALL NOT pause playback

In mobile form, selecting a feature whose curve panel crashes on its first render SHALL leave timeline playback running: the sheet never appeared, so the pause that accompanies an open sheet SHALL NOT be applied. If the panel later renders (for example after a retry), the sheet is open and playback SHALL be paused as for any open sheet.

#### Scenario: Panel crashes on first render while playing

- **WHEN** the timeline is playing in mobile form and the user selects a river segment whose curve panel throws on its first render
- **THEN** the curve region shows its fallback, the timeline is still playing, and the valid time keeps advancing

#### Scenario: Retry after the crash opens the sheet

- **WHEN** the fallback from the previous scenario is retried and the panel renders
- **THEN** the sheet is open and playback is paused

### Requirement: The sheet auto-pan SHALL move the map once per settled trigger

When the auto-pan trigger goes away and comes back to the same value before the scheduled pan has run, the map SHALL be moved exactly once.

#### Scenario: Trigger flickers before the pan runs

- **WHEN** the auto-pan trigger changes from a value to empty and back to the same value before the scheduled pan has run
- **THEN** the map receives exactly one camera move
