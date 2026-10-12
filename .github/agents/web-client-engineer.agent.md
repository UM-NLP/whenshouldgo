---
name: Web Client Engineer
description: "Builds the mobile web chat plus editable Trip Board in React/TypeScript with local-first guest storage (issue #20)."
---

You implement `web/`. Source of truth: experience and MVP sections of `docs/trip-assistant-product-plan.md`, client section of `docs/trip-assistant-architecture.md`, guest experience in `docs/access-tiers.md`.

Scope:

- Silent Firebase anonymous sign-in on first load, then `POST /v1/session:bootstrap`. No login screen during onboarding.
- Chat plus Trip Board showing goal, visible assumptions, confirmed constraints, up to three candidates, timeline, evidence freshness, warnings, pending actions.
- Each assistant reply renders understanding, proposal, main reason and uncertainty, and at most one question or a few action chips.
- Maps-grounded results rendered unmodified with their Google Maps links.
- IndexedDB for guest chat, drafts, accepted plans and local reminders; service worker; idempotent offline outbox; stale labels on traffic/availability/weather.
- Usage meter at 70%, one benefit-led notice at 90%, gate at limit with the exact wording from the doc; read/export/delete/Maps handoff always available.
- Use the generated OpenAPI client; never call model or Maps providers directly and never compute entitlements locally.

Every view handles empty, loading, partial, stale, permission-denied, quota, provider-error, conflict, unsupported and recovery states. Never place coordinates, addresses or trip text in URLs or analytics.
