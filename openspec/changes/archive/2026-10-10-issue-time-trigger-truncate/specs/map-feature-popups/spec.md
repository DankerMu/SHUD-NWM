## ADDED Requirements

### Requirement: The issue-time trigger SHALL keep its value on one line inside its box

The issue-time trigger in a curve window (river and station) SHALL render the selected issue time on a single line that never extends beyond the trigger's box, in desktop and mobile form. When the label does not fit, it SHALL be truncated with an ellipsis, the date-time part (`MM-DD HH:MM UTC`) SHALL remain fully visible at every supported sheet width, and the full label SHALL remain available as the trigger's `title`. The trigger's height SHALL NOT depend on the selected label.

In the river window's issue-time bar, the trigger SHALL NOT be narrowed by the caption next to it: when both do not fit on one line, the caption SHALL be the one that is not shown, and the bar SHALL stay a single row of unchanged height.

#### Scenario: Retained issue time in a portrait sheet

- **WHEN** a curve sheet at 390×664 shows an issue time that is no longer in the available list, labelled with the retention-unavailable suffix
- **THEN** the trigger is 44px high, its content does not overflow it vertically or horizontally, the date-time part is not elided, and the trigger's `title` is the full label

#### Scenario: Retained issue time in a desktop curve window

- **WHEN** the river curve window at 1280×800 shows a retained issue time
- **THEN** the trigger is 28px high, its content does not overflow it, and the trigger's `title` is the full label

#### Scenario: Narrow river sheet

- **WHEN** the river sheet is shown at 568×320 or 320×568 with an ordinary issue time
- **THEN** the issue-time bar is a single row, the trigger shows the whole label without elision, and the caption is either on one line beside the trigger or not shown

#### Scenario: Caption stays where it fits

- **WHEN** the river sheet is shown at 750×342 with an ordinary issue time
- **THEN** the caption is visible on one line beside the trigger
