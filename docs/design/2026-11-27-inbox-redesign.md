# Inbox redesign

Status: approved 2026-11-27, ships in 1.0.0.

## Intent

A new layout and visual style for the web interface, from a design handoff: an inbox with the queue
grouped by channel beside a panel that classifies, previews and files; History; a read-only Settings
& health page; and the Nocturne stylesheet. Filing behaviour does not change.

## Decisions

- Selection lives in the address (`?item=` or `?ids=`), so it needs no JavaScript.
- The preview is built from the same plan that filing runs, so it cannot drift.
- Filing and undo use the existing POST routes. A successful filing redirects to the next item and
  shows a message built from the database, so no session or secret key is needed.
- Where the handoff and the app differed, the app's behaviour stayed and was restyled: editable bulk
  episode numbers and titles, show description, season field, "Use the full title", Reclassify, the
  type-to-filter fields, and one default type per download.
- The handoff's login screen and Sign out are left out: the app uses the browser's basic login.
- Settings & health shows the values the app receives, not the Compose settings file's names.
- Inter is bundled, so browsers contact no outside server.

## Changes from the handoff

Where the handoff conflicted with WCAG 2.2 AA or with use, these take precedence:

- The panel takes half the page, and a video's fixed folder prefix wraps instead of being cut off.
- The message after filing stays until dismissed, because it can offer Undo.
- Queue titles wrap instead of being truncated.
- Field and control borders and the faintest text are brighter, to reach the contrast minimums.
- The queue says that ticking a box reloads the panel, and focus returns to that box.
- Skip links, one main heading per page and labelled controls were added.

The stylesheet's tokens are unchanged; the adjustments are in `app.css`.

## Approaches

| Approach | Trade-off |
|---|---|
| **Server-rendered pages with htmx for the preview (chosen)** | Works without JavaScript for selection and filing; a full page load per selection. |
| A client-side app | Smoother selection, but a second copy of the filing rules and a build step. |
