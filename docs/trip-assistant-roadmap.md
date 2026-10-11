# Agent-First Delivery Roadmap

Updated 2026-10-10. Durations are estimates for a small pre-seed team. A phase starts only after its entry gate and does not pass because documentation or code exists.

| Phase | Estimate | Deliverable | Exit gate |
|---|---:|---|---|
| 0. Agent concierge | 3–5 weeks | Instrumented mobile web Trip Workspace; vague and named same-day local outing; Maps-grounded discovery; shortlist; route/feasibility tools; Trip Board | 30+ goals across vague/specific/recovery; ≥90% useful proposal; zero invented material facts; ≥50% action; ≥30% second goal within 14 days |
| 1. Android agent MVP | 6–10 weeks | Chat plus Trip Board, Maps share/handoff, foreground location, saved places, reminders, refresh, recovery, outcome capture | ≥60% activation; ≥90% supported-goal completion; ≥30% second-day use; 100% material claims grounded; ≥99.5% crash-free |
| 2. Calendar and routines | 5–8 weeks | Selected Calendar context, consecutive-event reasoning, explicit routines, widget and App Actions | Calendar reduces turns/time; ≥25% repeat users save routine; ≥50% routine reuse; zero ambiguous event plans |
| 3. Proactive daily agent | 4–7 weeks | Opt-in evidence refresh, conflict detection, material-change alerts and recovery across active goals | ≥20% useful alert action; <10% mute; context-wrong alerts <1%; no silent plan mutation |
| 4. Multi-stop day | 5–8 weeks | Required/optional stops, fixed/flexible order, time windows, group needs and remaining-trip replanning | 100% hard constraints on oracle fixtures; no dropped required stop; conflict explained; locked/completed stops preserved |
| 5. Multi-day road trips | 6–10 weeks after separate validation | Web itinerary canvas, stays, day partitioning, bookings, rest, time zones, charging/fuel and Android day execution | 10 interviews/10 real plans first; 100% curated timezone/daily-limit checks; future commitments preserved |
| 6. Deeper execution | Gated | Richer voice, Android Auto, shared trips, additional modes/providers, transactions and iOS | Retention, economics, provider rights, permission, safety and recovery gates pass |

## Phase 0 scope

Support same-day driving in Toronto/GTA, one vague or named destination and optional return. Accept natural language and show an editable Trip Board. Use official Maps grounding for discovery, one recommendation plus up to two alternatives, structured place/route tools for finalists, optional deadline/visit validation, manual refresh, reminder proposal, Maps handoff and lightweight feedback.

The prototype must exercise the real agent loop and tools. Founder assistance may observe and recover failures but cannot secretly make recommendations.

Start with a silent anonymous Firebase identity and no sign-up screen. Implement the configurable 50-user-turn/10-activated-trip guest allowance, benefit-led notices, free Google/email-link upgrade, local-first guest storage, seven-day server workspace expiry, atomic quota reservation, read/export/delete after exhaustion and resumable account linking. Registered-free allowances reset every 30 days; no paid tier or billing system is built in this phase. Phase 0 cannot exit until anonymous isolation, quota races, account linking and interrupted merge recovery pass the gates in [access-tiers.md](access-tiers.md).

## Foundation sequence

1. Freeze `TripWorkspace`, fact/provenance, evidence, plan-version, permission, `PendingAction`, identity, entitlement, usage-reservation and cost-ledger contracts.
2. Build silent guest bootstrap, provider-neutral model gateway, strict patch validator and typed tool registry.
3. Implement Maps-grounded discovery with compliant rendering and retention controls.
4. Implement structured Places lookup, route estimate, optional feasibility validator and Maps handoff.
5. Build chat plus Trip Board with editable assumptions and recovery states.
6. Add free-account linking and resumable merge, evaluation fixtures, simulated tools, redacted traces, atomic quotas and kill switches before live pilot traffic.

## Expansion rules

Each capability requires empty, loading, partial, stale, permission-denied, quota, provider-error, conflict, unsupported and recovery states. Every phase reports actual agent quality, turns, latency, tool use, provider/model cost, correction, abandonment and retention by initial intent class.

Pause expansion when the agent invents a material fact, uses an unconfirmed hard constraint, produces a schedule violation, fails to ground a recommendation, users do not repeat the core job, proactive notifications are frequently muted, or provider cost exceeds the frozen per-goal cap.

## Platform strategy

Web is the cheapest way to validate the agent loop and recommendation value. Android becomes the first product after this gate because daily assistance depends on sharing, reminders, widgets, offline state and voice entry. Multi-day composition remains web-first even after Android launch.

Use one scale-to-zero Cloud Run service, Firestore, Cloud Tasks and FCM. Avoid GPU hosting, Kubernetes, vector storage, SMS, continuous background location, embedded navigation and autonomous transactions during early validation.


