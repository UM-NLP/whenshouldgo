# Agent-First Delivery Roadmap

Updated 2026-10-14. Durations are estimates for a small pre-seed team. A phase starts only after its entry gate and does not pass because documentation or code exists. Phases 1–3 deliver the product's long-term features (history, pattern recognition, routine reminders) on web before any Android or Calendar work. Each later phase reuses the stores and contracts of the earlier ones without migration; the invariants below hold in every phase.

| Phase | Estimate | Deliverable | Exit gate |
|---|---:|---|---|
| 0. Agent concierge (web) | 3–5 weeks, to be re-baselined after the contract freeze | Instrumented mobile web Trip Workspace; vague and named same-day local outing; Grounding Lite discovery shown verbatim with sources; server-rendered place and route checks; Trip Board; in-app reminder; local outing log and consent ledger (schemas only, no server history) | 30+ goals across vague/specific/recovery; ≥90% useful proposal; zero invented material facts; ≥50% action; ≥30% second goal within 14 days; structural compliance gates below |
| 1. Trip history (web) | Set at phase entry | Opt-in outing history for registered users (guests keep it on the device); history view rebuilt from place IDs; edit, delete, export; saved places by ID plus user label; 180-day retention ceiling the user can shorten | History view renders 100% via place-ID re-fetch with attribution; zero Maps content at rest outside the display-cache limit; account deletion removes all history; Places cost per view within the frozen cap |
| 2. Patterns and confirmed routines (web, in-app) | Set at phase entry | Deterministic pattern derivation over stored history with separate opt-in; in-app proposal “You often go out Saturday mornings. Save as a routine?”; routine create, edit, delete; per-pattern “never suggest”; routines used as soft preferences | ≥50% of proposals accepted or declined; zero routines created without a confirmed `PendingAction`; zero inferred hard constraints; declined patterns never re-proposed |
| 3. Routine reminder notifications (web push) | Set at phase entry | Opt-in web push for confirmed routines of registered users; quiet hours, caps and one-tap stop; generic text; in-app inbox fallback; server task per routine occurrence | ≥20% useful action; <10% mute; context-wrong <1%; zero sends in quiet hours or after opt-out; zero duplicate sends; zero model calls in the trigger path |
| 4. Android agent MVP | 6–10 weeks | Chat plus Trip Board, Maps share/handoff, foreground location, saved places, native reminders, refresh, recovery, outcome capture; reuses Phase 1–3 stores | ≥60% activation; ≥90% supported-goal completion; ≥30% second-day use; 100% material claims grounded; ≥99.5% crash-free |
| 5. Calendar and calendar-aware routines | 5–8 weeks | Selected Calendar context, consecutive-event reasoning, calendar-aware routines, widget and App Actions | Calendar reduces turns/time; ≥25% repeat users save routine; ≥50% routine reuse; zero ambiguous event plans |
| 6. Proactive daily agent | 4–7 weeks | Opt-in refresh of non-Maps evidence, conflict detection, material-change alerts and recovery across active goals, built on the Phase 3 notification path | ≥20% useful alert action; <10% mute; context-wrong alerts <1%; no silent plan mutation |
| 7. Multi-stop day | 5–8 weeks | Required/optional stops, fixed/flexible order, time windows, group needs and remaining-trip replanning | 100% hard constraints on oracle fixtures; no dropped required stop; conflict explained; locked/completed stops preserved |
| 8. Multi-day road trips | 6–10 weeks after separate validation | Web itinerary canvas, stays, day partitioning, bookings, rest, time zones, charging/fuel and Android day execution | 10 interviews/10 real plans first; 100% curated timezone/daily-limit checks; future commitments preserved |
| 9. Deeper execution | Gated | Richer voice, Android Auto, shared trips, additional modes/providers, transactions and iOS | Retention, economics, provider rights, permission, safety and recovery gates pass |

## Entry gates for Phases 0–3

| Phase | Entry gate |
|---|---|
| 0 | Foundation issues #14, #22 and #23 are green. Grounding Lite's reported GA status and Canadian eligibility are confirmed for the owner's billing account (an owner-supplied analysis reports GA with no Canadian exclusion; a Google-confirmed record is still wanted). The OpenRouter model provider's written zero-retention and no-training terms are on file, and reusable prompt caching is verified off, or accepted by Google in writing, for every allowlisted upstream endpoint. A synthetic Maps simulator exists. The legal checklist in [docs/legal/open-questions.md](legal/open-questions.md) is signed. |
| 1 | Phase 0 exit. Consent ledger live. Deletion and export tested. Retention ceiling (180 days, user can shorten) shown in the notice. |
| 2 | Phase 1 exit. Enough real history from consenting users to compute patterns. Separate pattern-suggestion consent live. Google's written answers on place-ID analysis recorded, or patterns restricted to counts over time, the user's tag and the user's label. AGENTS.md rule narrowed as in decision 20. Quebec profiling requirements and sensitive-inference handling settled with counsel. |
| 3 | Phase 2 exit. Counsel's answer on CASL for push notifications. Notification consent flow, unsubscribe and consent logging live. Security release gate “before proactive beta” met. iOS web-push requirements and FCM terms verified. |

## Phase 0 scope

Support same-day driving in Toronto/GTA, one vague or named destination and optional return. Accept natural language and show an editable Trip Board. Use Google Maps Grounding Lite for discovery with the pinned OpenRouter model: one recommendation plus up to two alternatives, shown as grounded output with sources. Use server-side Places Details for a selected place ID and a direct Routes check for finalists, both rendered by the server. Offer optional deadline/visit validation, manual refresh, an in-app reminder shown when the user next opens the app (a closed web tab cannot fire one), Maps handoff and lightweight feedback.

Phase 0 may define persisted schemas and the consent ledger for later phases and may record the user's explicit saves locally in the outing log. It does not implement server-side history, pattern computation, routines or notifications.

Voice entry in the Android phase starts with an in-app press-and-hold button and a 60-second maximum capture; wake words, background microphone services and hardware-key interception stay deferred. Before web or Android pilot traffic, enforce the travel-only domain gate, fixed out-of-scope response, server-owned tool allowlist and versioned token budgets in [voice-and-domain-controls.md](voice-and-domain-controls.md).

The prototype must exercise the real agent loop and tools. Founder assistance may observe and recover failures but cannot secretly make recommendations.

Start with a silent anonymous Firebase identity and no sign-up screen. Implement the configurable 50-user-turn/10-activated-trip guest allowance, benefit-led notices, free Google/email-link upgrade, local-first guest storage, seven-day server workspace expiry, atomic quota reservation, read/export/delete after exhaustion and resumable account linking. Registered-free allowances reset every 30 days; no paid tier or billing system is built in this phase. Phase 0 cannot exit until anonymous isolation, quota races, account linking and interrupted merge recovery pass the gates in [access-tiers.md](access-tiers.md).

Structural compliance gates for Phase 0: 100% of grounded blocks are displayed verbatim with sources immediately after; zero Maps content in logs, traces, evaluation data or history; display-cache purge verified; every model request carries the pinned-provider and zero-retention parameters.

## Invariants across phases

These hold in every phase and are the reason the phases do not contradict each other.

1. Maps content is never stored beyond the limits in decision 18. History stores place IDs and user data only.
2. Nothing inferred is stored or treated as a hard fact without the user's confirmation. Before Phase 2 nothing inferred is persisted at all.
3. Every stored kind of data has its own consent, retention, deletion and export rule, and every persisted document carries a `schema_version`.
4. A side effect, including creating a routine or enabling notifications, uses a pending action with a preview, nonce, expiry, idempotency key and explicit confirmation.
5. Notifications carry no place names or Maps content, and the trigger path makes no model call.
6. Guests keep their data on the device and receive no server monitoring or notifications.
7. No background location or microphone at any phase listed above.
8. Evaluation fixtures use synthetic place data.

## Foundation sequence

1. Freeze `TripWorkspace`, fact/provenance, evidence, plan-version, permission, `PendingAction`, identity, entitlement, usage-reservation, cost-ledger, `RuntimeConfig`, typed response and error contracts, each with `schema_version`.
2. Build silent guest bootstrap, travel-domain gate, provider-neutral OpenRouter model gateway that fails closed without a pinned provider and zero retention, token accounting, strict patch validator and typed tool registry.
3. Add the telemetry allowlist and a log-canary test before the first model call. Add the cost ledger with a versioned price table and kill switches.
4. Implement Grounding Lite discovery through a server-side MCP client with compliant rendering and retention controls, using a synthetic Maps simulator for tests.
5. Implement server-rendered Places Details lookup, route estimate, optional feasibility validator and Maps handoff.
6. Build chat plus Trip Board with editable assumptions and recovery states.
7. Add free-account linking and resumable merge, evaluation fixtures, simulated tools, redacted traces, atomic quotas and kill switches before live pilot traffic.

## Expansion rules

Each capability requires empty, loading, partial, stale, permission-denied, quota, provider-error, conflict, unsupported and recovery states. Every phase reports actual agent quality, turns, latency, tool use, provider/model cost, correction, abandonment and retention by initial intent class.

Pause expansion when the agent invents a material fact, uses an unconfirmed hard constraint, produces a schedule violation, fails to ground a recommendation, users do not repeat the core job, notifications are frequently muted, or provider cost exceeds the frozen per-goal cap.

## Platform strategy

Web is the cheapest way to validate the agent loop, recommendation value, history and routine reminders. Android becomes the first native product after the web phases because daily assistance depends on sharing, native reminders, widgets, offline state and voice entry, and only if the pilot shows repeat value and a native-specific need. Multi-day composition remains web-first even after Android launch.

Use one scale-to-zero Cloud Run service, Firestore, Cloud Tasks and web push (FCM for Android later). Avoid GPU hosting, Kubernetes, vector storage, SMS, continuous background location, embedded navigation and autonomous transactions during early validation.
