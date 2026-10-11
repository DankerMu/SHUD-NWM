## ADDED Requirements

### Requirement: The issue-time selector SHALL mark only a retained selected issue time as unavailable

The curve-window issue-time selector SHALL NOT mark any issue time other than a selected, no-longer-listed one as retained-disk unavailable; every listed issue time SHALL stay selectable, including one that returned a retained-disk miss earlier in the session.

#### Scenario: A listed issue time missed on disk earlier in the session

- **WHEN** an issue time that is still among the available issue times returned a retained-disk miss earlier in the session
- **THEN** its option is shown without the unavailable label and can be selected again

#### Scenario: The selected issue time is no longer listed

- **WHEN** the selected issue time is not among the available issue times
- **THEN** it is kept as the first option, disabled and labelled as retained-disk unavailable

## MODIFIED Requirements

### Requirement: Forecast issue-time selectors use dark popup surfaces

M11 river forecast and station forcing popups SHALL render forecast issue-time
choices in a controlled dark popup surface whose background, text, hover,
focus, selected, and disabled states remain legible in the dark glass popup
theme across supported browsers.

#### Scenario: River cycle menu is dark and legible

- **WHEN** a user opens the river q_down forecast panel and opens the issue-time
  selector
- **THEN** the selector content MUST use a dark surface consistent with the
  curve popup theme
- **AND** the active, hover, focus, and selected option states MUST remain
  readable without a white native option backdrop.

#### Scenario: Station cycle menu is dark and legible

- **WHEN** a user opens the station forcing popup and opens the issue-time
  selector
- **THEN** the selector content MUST use the same dark issue-time selector
  behavior as the river panel
- **AND** changing the issue time MUST continue to reload both GFS and IFS
  station series for the selected variable.

#### Scenario: Disabled retained-window options remain honest

- **WHEN** the selected issue time is no longer among the available issue times
  and is shown as retained-disk unavailable
- **THEN** the option MUST be disabled or otherwise non-selectable
- **AND** the unavailable reason MUST remain visible in the dark selector
  without reducing contrast below readable levels.
