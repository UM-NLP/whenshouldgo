# Product and web agent

Use this role for client behavior, API presentation, and Trip Board changes in
`web/` (React + TypeScript).

Read `AGENTS.md`, `docs/trip-assistant-product-plan.md`,
`docs/trip-assistant-roadmap.md`, `docs/data-and-history-design.md` and the guest experience in
`docs/access-tiers.md`. Build the same-day local-outing journey (anywhere, no geographic restriction) on
mobile web only: a vague category or named destination, same-day driving, one
recommendation plus up to two alternatives, an editable Trip Board, manual refresh,
reminder proposal, outcome check-in and Google Maps handoff.

- Each reply shows its understanding, a proposal or next action, the main reason and
  uncertainty, and at most one question or a few action chips. Provide value before
  asking; ask only when the answer changes feasibility, ranking, safety,
  permissions or an action.
- Expose assumptions, evidence freshness, uncertainty, alternatives and pending
  actions. Hard needs (accessibility, dietary, fixed return) filter candidates; they
  are never averaged away.
- First load signs in anonymously with Firebase, then calls
  `POST /v1/session:bootstrap`. Use only the client generated from the backend
  OpenAPI contract; never call model or Maps providers directly.
- The Trip Board shows the goal, visible assumptions, confirmed constraints, up to
  three candidates, timeline, evidence freshness, warnings and pending actions.
- IndexedDB holds guest chat, drafts, accepted plans and local reminders; a service
  worker and an idempotent offline outbox handle connectivity loss. Traffic,
  availability and weather carry stale labels.
- No login screen during onboarding. Guest data is local-first. Show a quiet meter
  at 70%, one benefit-led notice at 90%, and the exact gate wording at the limit;
  read, export, delete and Maps handoff stay available. Never compute entitlements
  on the client.
- Render Maps-grounded results unmodified with their Maps links. No coordinates,
  addresses or trip text in URLs or analytics.
- Every view handles empty, loading, partial, stale, permission-denied, quota,
  provider-error, conflict, unsupported and recovery states.
