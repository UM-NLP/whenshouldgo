# Active Backlog

Updated 2026-10-17. Only work in the current agent-first product plan appears here. The new items were created as GitHub issues #24 to #35 on 2026-10-17 from [legal/github-issues-draft.md](legal/github-issues-draft.md).

| Work item | State | Completion evidence |
|---|---|---|
| [#13 VISION-01: Agent-first personal trip assistant](https://github.com/UM-NLP/whenshouldgo/issues/13) | Active epic | Current product boundary, principles, phases, and expansion gates |
| [#14 AGENT-01: Trip Workspace and agent runtime](https://github.com/UM-NLP/whenshouldgo/issues/14) | Next | Workspace schemas, model gateway, domain gate, typed tools, evidence validation, action protocol, event journal, and cost ledger |
| [#22 ACCESS-01: Anonymous guest and registered-free tiers](https://github.com/UM-NLP/whenshouldgo/issues/22) | Next with #14 | Silent guest bootstrap (usage and capabilities endpoints done; Firebase token issue pending), account linking, quota ledger, recovery, and migration tests |
| [#23 GUARD-01: Push-to-talk, travel-only scope, and token budgets](https://github.com/UM-NLP/whenshouldgo/issues/23) | Next with #14 | In-app hold-to-talk contract, server domain gate, tool allowlist, token reservation, safe truncation handling, and boundary evaluation |
| [#20 AGENT-MVP-01: Same-day local outings](https://github.com/UM-NLP/whenshouldgo/issues/20) | After foundation | Complete mobile-web journey from vague or named goal to grounded Trip Board, route check, Maps handoff, and outcome capture |
| [#21 AGENT-EVAL-01: Quality, safety, cost, and pilot](https://github.com/UM-NLP/whenshouldgo/issues/21) | After MVP | Reviewed offline corpus, adversarial and cost reports, then at least 30 supported real goals and an explicit continue/revise/stop decision |
| [#31 LLMOPS-01:](https://github.com/UM-NLP/whenshouldgo/issues/31)  Runtime config, telemetry allowlist, cost ledger, eval pipeline | Next with #14 | Versioned runtime config on every turn, typed traces with canary test, price table and dollar caps, CI eval suites with a statistical pass rule |
| [#32 HISTORY-01:](https://github.com/UM-NLP/whenshouldgo/issues/32)  Opt-in trip history (Phase 1) | After #21 Phase 0 exit | Server history for registered users, place-ID rendering, edit/delete/export, 180-day retention ceiling |
| [#33 ROUTINE-01:](https://github.com/UM-NLP/whenshouldgo/issues/33)  Patterns and confirmed routines (Phase 2) | After HISTORY-01 | Deterministic patterns, user-confirmed routines, sensitive-inference rule |
| [#34 NOTIFY-01:](https://github.com/UM-NLP/whenshouldgo/issues/34)  Routine reminder notifications over web push (Phase 3) | After ROUTINE-01 | Opt-in web push, quiet hours and caps, generic payload, no model call in the trigger path |
| LEGAL-01 to LEGAL-07 ([#24](https://github.com/UM-NLP/whenshouldgo/issues/24), [#25](https://github.com/UM-NLP/whenshouldgo/issues/25), [#26](https://github.com/UM-NLP/whenshouldgo/issues/26), [#27](https://github.com/UM-NLP/whenshouldgo/issues/27), [#28](https://github.com/UM-NLP/whenshouldgo/issues/28), [#29](https://github.com/UM-NLP/whenshouldgo/issues/29), [#30](https://github.com/UM-NLP/whenshouldgo/issues/30)): Legal and privacy gates | Parallel to #14 | Written processor terms (DeepInfra), privacy notice, counsel reviews; the Google questions (LEGAL-01, -03, -07) were closed on 2026-10-17 by owner decision; see [legal/open-questions.md](legal/open-questions.md) |
| Contract freeze and router split (decisions 25-27) | Done 2026-10-26 | `backend/app/routes/` package (one router per area); `SAVED_PLACE` consent purpose, `SAVE_PLACE` effect only, `SavedPlace`, `PlaceCard`, `RouteCheck`, `OutcomeCheckin`, `ExportBundle` contracts; `history_enabled` flag (dark, fails closed without an approved notice); typed telemetry events; every planned route is a typed 501 stub in the OpenAPI; web client types regenerated; CI drift check on `web/src/api/openapi.json` |
| [#35 ACCESS-02: Shared spend controls and abuse limits](https://github.com/UM-NLP/whenshouldgo/issues/35) | Gate before any pilot traffic | Shared (Firestore) cost ledger and kill switches, global spend cap, identity-creation and IP limits, App Check; also TTLs and IP buckets for the in-memory operation limiters and ledger (now only LRU-capped) |
| [#11 ANDROID-01: Android product gate and MVP](https://github.com/UM-NLP/whenshouldgo/issues/11) | Gated by #21 and the web phases | Android build begins only if the pilot demonstrates repeat value and a native-specific need |
| [#15 CALENDAR-01: Read-only calendar and calendar-aware routines](https://github.com/UM-NLP/whenshouldgo/issues/15) | Future | Consent, account, OAuth, privacy, and evaluation gates; not a prerequisite for routines or notifications |
| [#16 PROACTIVE-01: Event-driven monitoring and reminders](https://github.com/UM-NLP/whenshouldgo/issues/16) | Future | NOTIFY-01, evidence-refresh rules for non-Maps content, task idempotency, quiet hours, and measured cost |
| [#17 MULTISTOP-01: Same-day bounded optimizer](https://github.com/UM-NLP/whenshouldgo/issues/17) | Future | Demand evidence and bounded optimization evaluation |
| [#18 ROADTRIP-01: Multi-day trip builder](https://github.com/UM-NLP/whenshouldgo/issues/18) | Future | Separate customer validation and provider-cost model |
| [#19 VOICE-01: Android voice entry](https://github.com/UM-NLP/whenshouldgo/issues/19) | Future Android phase | Implement #23 push-to-talk baseline; gate assistant-role or hotword research separately |

## Current operating limits

- No paid campaign, custom domain, payment system, native app, or always-on server during the web validation phase.
- No visible login before value; every request still has a server-derived guest or registered principal.
- One scale-to-zero backend with server-side credentials, quotas, authorization, idempotency, and kill switches.
- At most six model calls, twelve tool calls, one escalation, 40,000 input tokens, and 4,000 output tokens per interactive run.
- Search broadly, fetch details for a shortlist, and route finalists only.
- Precise trip data stays out of Git, URLs, logs, and analytics.

Documentation completion is not product or market validation.