# Product and web agent

Use this role for client behavior, API presentation, and Trip Board changes.

Read `AGENTS.md`, `docs/trip-assistant-product-plan.md`,
`docs/trip-assistant-roadmap.md` and the guest experience in
`docs/access-tiers.md`. Build the Toronto/GTA same-day local-outing journey on
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
- No login screen during onboarding. Guest data is local-first. Show a quiet meter
  at 70%, one benefit-led notice at 90%, and the exact gate wording at the limit;
  read, export, delete and Maps handoff stay available. Never compute entitlements
  on the client.
- Render Maps-grounded results unmodified with their Maps links. No coordinates,
  addresses or trip text in URLs or analytics.
- Every view handles empty, loading, partial, stale, permission-denied, quota,
  provider-error, conflict, unsupported and recovery states.
