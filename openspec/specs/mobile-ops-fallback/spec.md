# mobile-ops-fallback Specification

## Purpose
定义内部诊断页（`/ops`）与模型资产页在移动形态下的兜底要求：页面不横向溢出、可纵向滚到底、表格在自己的容器内滚动、控件满足字号与触控下限；这些页面不做完整的移动端重设计。

## Requirements

### Requirement: Operational pages are readable and scrollable in mobile form

In mobile form `/ops` and `/monitoring` SHALL lay their content out in a single column inside a page scroll container with a horizontal padding of at least 12 CSS px. When the content is taller than the container, the container SHALL scroll vertically to its end while the window does not scroll. The page scroll container SHALL have no horizontal overflow of its own; wide tables SHALL scroll horizontally inside their own container. The log dialog SHALL lie inside the viewport with its log body scrollable in both directions. No mobile-specific interaction (accordion, tab, drawer) is required.

#### Scenario: Page padding and single column on a phone

- **WHEN** an authorized user opens `/ops` or `/monitoring` at 390×664
- **THEN** no card's left or right edge is closer than 12px to the viewport edge, and no two cards sit side by side

#### Scenario: Page scrolls vertically inside its container

- **WHEN** an authorized user opens `/ops` or `/monitoring` at 390×664 or 750×342 with content taller than the viewport
- **THEN** scrolling the page scroll container brings its last card's bottom edge inside the viewport, and the window scroll offset stays 0

#### Scenario: Jobs table scrolls inside its container

- **WHEN** the jobs table is wider than the viewport at 390×664
- **THEN** the table's own container has a scroll width greater than its client width, and the page scroll container's scroll width equals its client width

#### Scenario: Log dialog fits

- **WHEN** the log dialog is opened at 390×664 with a log wider and taller than the dialog
- **THEN** the dialog's bounding box and its close control's bounding box lie inside the viewport, and the log body's scroll width and scroll height both exceed its client size

### Requirement: The model-assets page does not overflow in mobile form

In mobile form the `/system/model-assets` page scroll container SHALL have no horizontal overflow of its own; its product table, which is wider than a phone viewport, SHALL scroll inside its own container. The page is otherwise treated as desktop-only.

#### Scenario: No container-level overflow

- **WHEN** an authorized user opens `/system/model-assets` at 390×664 with a non-empty product table
- **THEN** the page scroll container's scroll width equals its client width, and the product table's container has a scroll width greater than its client width
