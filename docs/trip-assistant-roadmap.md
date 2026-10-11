# Full Trip Assistant Roadmap

Updated 2026-10-10. Durations are planning estimates for a very small pre-seed team and begin only after the preceding gate. No phase is considered complete because documentation exists.

| Phase | Estimated effort | Deliverable | Gate to continue |
|---|---:|---|---|
| 0. Validate the core method | 2â€“4 weeks | Mobile web adaptive one-stop round trip, Maps handoff, refresh and outcome check-in | 20+ real attempts, 5+ users, zero false-feasible critical fixtures, â‰¥50% recommendation adoption, â‰¥30% plan again within 14 days |
| 1. Android local-trip alpha | 6â€“10 weeks | Kotlin/Compose app, Maps share target, saved active plan, local/FCM reminders, foreground location, manual refresh | â‰¥60% invited activation, â‰¥30% second planning day, â‰¥90% followed-plan deadline success with chosen allowance, p95 <8s, â‰¥99.5% crash-free |
| 2. Conversational agent | 3â€“5 weeks | Cheap schema-only model over user-authored intent; deterministic form and result rendering | 100% critical hard-constraint preservation, â‰¥98% tool-selection/schema accuracy, zero unauthorized commits, Maps/provider AI-use decision complete, measured model cost within budget |
| 3. Calendar and explicit routines | 5â€“8 weeks | User-selected Google Calendar read-only import, conflict detection, favorite places, user-created routines, provenance | â‰¥40% of eligible retained users try it, â‰¥50% event-to-plan conversion, 30% faster repeat planning, revocation verified |
| 4. Proactive companion | 4â€“7 weeks | Calendar change handling, traffic refresh policy, FCM and local reminders, widget, explainable plan proposals | â‰¥20% useful action rate on material alerts, <10% mute rate, context/time-wrong alerts <1%, bounded provider cost |
| 5. Same-day multi-stop | 5â€“8 weeks | Up to five stops, time windows, priorities, optional stops, bounded optimization, remaining-trip replanning | 20 concierge plans first; 100% hard constraints, no dropped required stop, oracle regret within target, completed/locked stops preserved |
| 6. Multi-day road trips | 6â€“10 weeks | Web itinerary builder, day partitioning, lodging/break/charging constraints, time zones, offline Android day view | 10 interviews and 10 real plans first; 100% curated timezone/DST and daily-limit tests; future locked bookings preserved |
| 7. Voice and ecosystem | 8â€“12+ weeks, gated | App Actions, richer push-to-talk, Android Auto or embedded navigation if justified; assistant-role research | Retention and usage prove the surface is needed; separate privacy, store-policy, battery and safety gates pass |

## Phase 0 scope

The immediate build is deliberately smaller than the vision: driving, same day, one destination, same return location, exact visit duration, adaptive questions, preparation/parking/walking allowances, a deadline or time window, and an objective such as minimum forecast road time or latest safe departure. It has no account, calendar, LLM, inferred routine, or continuous monitoring.

Phase 0 reuses issues #4â€“#10 and #12. Native Android issue #11 remains gated by the Phase 0 pilot rather than abandoned.

## Foundation work before Phase 1

- Freeze the shared `TripPlan`, `PlanningProblem`, provenance, consent, pending-action, quota, and cost-ledger contracts.
- Move the historical one-way research scripts under an experiment boundary and keep their findings On Hold.
- Create the modular-monolith directories and provider interfaces without deploying unused services.
- Add Firebase Auth only when cross-device persistence is required.
- Establish evaluation fixtures for constraint extraction, planner feasibility, tool selection, prompt injection, time zones, and reminder versioning.
- Obtain a written route-provider use determination before any Maps-derived value reaches an LLM, TTS, history, or AI evaluation corpus; otherwise keep the terminal deterministic boundary or select a permitting provider.

## Release and cost rules

Every phase needs empty, loading, stale, provider-error, permission-denied, quota-exceeded, offline, conflict, and recovery states. Credentials remain server-side. Authorization, quotas, and side-effect confirmation are server controls. Each release records actual model quality, latency, Maps/provider usage, notification behavior, and cost; planning estimates do not count as evidence.

The current invite-session/no-history pilot architecture cannot simply be expanded into the persistent assistant. Before accounts and Calendar, add authenticated tenant isolation, OAuth token vaulting, purpose-specific consent, export/deletion, scheduler cancellation, and cross-tenant/adversarial tests as a separate trust-boundary milestone.

Use a single scale-to-zero Cloud Run service, Firestore free allowance where possible, FCM, Cloud Tasks, and one small reconciliation schedule. The agent model stays off until a user speaks or types or a scheduled plan event requires an explanation. Route refreshes exist only for active, opted-in plans and stop at expiry. Do not pay for a GPU, Kubernetes, vector database, always-on instance, SMS, or embedded navigation during validation.

## Stop or revise conditions

Pause expansion if the planner produces a false-feasible hard deadline, users do not repeat the core planning job, route refresh cost exceeds the per-plan cap, proactive notifications are frequently muted, OAuth verification blocks the intended calendar experience, or Maps handoff mismatch destroys plan trust. Fix the core job before adding another surface.


