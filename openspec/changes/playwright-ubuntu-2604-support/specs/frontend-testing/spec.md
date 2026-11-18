## ADDED Requirements

### Requirement: The Playwright version SHALL support the CI runner image

The pinned `@playwright/test` version SHALL ship a chromium build for the operating system of the runner image that the
mocked-regression lane uses. The lane SHALL run on `ubuntu-latest`, not on a pinned older image kept only because
Playwright lacks support. A workflow that replays a historical commit's lockfile (the M15 visual-evidence dispatch) MAY
pin an older image, and its comment SHALL state that reason.

#### Scenario: The mocked lane installs chromium on the current runner image

- **WHEN** the `Frontend E2E (mocked)` job runs on `ubuntu-latest`
- **THEN** `playwright install --with-deps chromium` succeeds and every shard executes its specs

#### Scenario: The historical visual-evidence replay keeps a supported image

- **WHEN** `m15-visual-evidence` is dispatched with an evidence SHA from before the upgrade
- **THEN** it runs on an image that SHA's Playwright version supports, and the pin comment names that reason
