# Design QA

## Comparison setup

- Approved reference: `design-assets/selected-design.png`
- Implementation capture: `design-assets/implementation-dashboard.png`
- Combined evidence: `design-assets/comparison.png`
- Focused header and metrics evidence: `design-assets/comparison-header-metrics.png`
- Reference pixels: 1487 × 1058, normalized to 1440 × 1024 for comparison
- Implementation pixels: 1440 × 1024
- Browser viewport: 1440 × 1024 CSS pixels at 1× capture density
- State: authenticated dashboard, all channels, last 14 days, date order

## Pass history

### Pass 1 findings and fixes

- P2 layout: the first implementation used a 138 px sidebar, which compressed the workspace compared with the approved 96 px rail. Fixed by matching the compact rail and 24 px workspace gutter.
- P2 behavior/fidelity: the approved Day / Week / Month chart control was missing. Fixed with a keyboard-accessible segmented control that genuinely regroups cumulative series by day, week, or month.
- P2 responsiveness: the initial table columns could crowd the action column. Fixed with an explicit column grid, fixed table layout on desktop, and a contained horizontal scroller on narrow screens.
- P2 accessibility: mobile header controls and post-detail targets were smaller than 44 px. Fixed to 44 px minimum targets; bottom navigation remains 51 px high.
- P2 icon fidelity: early generic-looking glyph choices did not match the approved expressive line system. Replaced with one consistent self-hosted MingCute regular icon family and verified every used class against the vendored font.
- P2 typography: chart labels initially fell back to the canvas default. Fixed by applying the self-hosted Manrope family to page and chart typography.

### Final comparison findings

- P0: none.
- P1: none.
- P2: none.
- P3: the live dashboard preserves ISO UTC date labels instead of the mock's abbreviated English labels; this is intentional for the analytics context and does not affect layout or readability.
- P3: the production icon font preserves the selected thin geometric character while using real library glyphs rather than attempting to trace the generated mock icons.

## Functional and accessibility checks

- History window verified for 7 days, 14 days, and all history; the empty channel value remains valid and no longer reaches the API as an invalid integer.
- Sort-by-views, post drill-down, and return-to-overview flows verified in the in-app browser.
- Day / Week / Month chart controls verified with visible state and `aria-pressed` updates.
- Post 1 renders two collected comments with stored author attribution and direct Telegram links; unavailable Telegram identity is explicitly shown as `Unknown / hidden`.
- Desktop and 390 × 844 mobile layouts have zero page-level horizontal overflow.
- Mobile tables scroll inside their panel, while filters and metric cards remain usable in a single-column layout.
- Manrope and MingCute fonts load successfully; focus-visible styles and reduced-motion behavior are present.
- Backend collector remained healthy during QA: 27 posts and 4 comments were read on repeated scheduled cycles with no collector errors.
- Python compilation, Jinja parsing, asset requests, icon-class validation, and `git diff --check` passed. No automated test suite exists in the repository.

final result: passed
