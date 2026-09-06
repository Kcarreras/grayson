# Console design and interaction guide

The console uses server-rendered Jinja templates, local CSS and JavaScript, system
fonts, and the existing guarded application APIs. It needs no frontend build step
or external asset service.

## Shared interface

- `ui/templates/base.html` owns navigation, the theme control, the quick-navigation
  dialog, accessible landmarks, and shared feedback.
- `ui/static/console.css` defines both themes, typography, controls, tables, and
  responsive layouts. `ui/templates/_icons.html` contains local SVG icons.
- `ui/static/console.js` enhances the server-rendered content: list filtering and
  sorting, clipboard feedback, keyboard navigation, form protection, and live updates.
- Shared assets revalidate with ETags. Unchanged assets return 304; changed assets
  load without waiting for a version bump.

Use the existing core APIs and form endpoints for mutations. Approval, evidence,
scope, and budget enforcement remain server responsibilities.

## Navigation and keyboard controls

- Persistent sidebar on desktop; an expandable Menu on narrow screens.
- **Ctrl/Cmd K** opens quick navigation to workspace pages and current-page sections.
  Arrow keys select a destination; Enter opens it; Escape closes the dialog.
- **/** focuses the first visible search field outside an editor.
- Section links open collapsed ancestors before moving focus to their content.
- Sortable table headers support Enter and Space. Session action tabs support
  arrow keys, Home, and End. Selecting an action only opens its existing form.
- Charts support Enter/Space to enlarge, arrow keys to move between charts, and
  Escape to close. Inline help also dismisses with Escape.

## Live updates and forms

Only pages with `data-live` poll for changes. Active sessions and the dashboard
check every ten seconds; closed sessions and decision forms do not poll.

An unchanged response causes no reload. Updates pause while a form has unsaved
values, a field is focused, a dialog is open, text is selected, the tab is hidden,
or a submission is in progress. A user can also pause and resume explicitly.
Refresh failures keep the current page visible and show a retry status.

Unsaved POST-form values remain protected after focus leaves the field. Navigation
away prompts through the browser's standard unsaved-change warning. Values are
not stored in browser storage. Successful submission retains the existing form
payload and endpoint; pending submissions show feedback and block duplicate submits.

Search, sort, filter, disclosure, and session-action choices are remembered.
An automatic refresh restores the document's scroll position.

## Verification of the design pass

Validated on 6 September 2026:

- Full Python suite: **1,171 passed, 1 skipped**. Ruff lint and format checks passed.
- Shared JavaScript passed `node --check`; the Git diff passed whitespace checks.
- Browser review in light and dark themes at desktop, tablet, and phone sizes,
  including 320, 390, 900, and 1280 pixel widths.
- Reviewed sessions, charts, queries, interventions, workflow catalog/detail/editor,
  schemas, knowledge and relationship maps, checks, records, settings, change impact,
  and comparison setup/results using an isolated sandbox workspace.
- Exercised search and reset, quick navigation, keyboard charts and session actions,
  mobile navigation, mapping add/remove, comparison preview and execution, draft
  protection across refresh intervals, and dismissible mobile help. Saved, reviewed,
  activated, and replayed a regression check, producing fresh passing evidence.
- Checked page overflow, accessible field names, and core text-token contrast in
  both themes. Wide data tables retain their own horizontal scrolling.

Browser checks used the local sandbox warehouse. The automated suite covers the
underlying guard, evidence, approval, and assurance workflows.
