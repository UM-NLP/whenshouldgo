# Backlog

Updated 2026-10-10. The long-term direction is an Android-first personal trip assistant with calendar, explicit routine memory, proactive reminders, multi-stop planning, and multi-day road trips. The active validating release remains a low-cost mobile web application and assisted private pilot. Native Android and each sensitive or costly capability are gated by evidence. The coordinator maintains this backlog and the [Project 2 snapshot](project-board.md).

| Item | Status | Dependency / completion evidence |
|---|---|---|
| Product, architecture, quality and roadmap documentation | Done | [Product](web-product-plan.md), [architecture](web-architecture.md), [roadmap](web-roadmap.md), and [quality/operations](web-quality-operations.md) reviewed locally |
| Full assistant product, architecture, model, security and roadmap | Done | [Product](trip-assistant-product-plan.md), [architecture](trip-assistant-architecture.md), [model decision](model-hosting-decision.md), [security](trip-assistant-security.md), and [roadmap](trip-assistant-roadmap.md) reviewed locally; implementation and validation remain Todo |
| [#4 WEB-01: Adaptive product/schema/acceptance/cost contract](https://github.com/UM-NLP/whenshouldgo/issues/4) | Todo | Freeze constraint semantics, interview stopping rule, exact-second objective, search completeness, pilot denominators and cost contract |
| [#12 INTAKE-01: Adaptive trip interview engine](https://github.com/UM-NLP/whenshouldgo/issues/12) | Blocked by #4 | Typed state, deterministic question policy, rule parser, gold dialogues and minimum-safe-question oracle |
| [#5 CORE-01: Generalized planner and route oracle](https://github.com/UM-NLP/whenshouldgo/issues/5) | Blocked by #4/#12 request contract | Outbound/round-trip constraints, independent oracle, zero false-feasible results, p95 regret <=5 road minutes, <=24 estimates |
| [#6 API-01: Interview/provider secure API](https://github.com/UM-NLP/whenshouldgo/issues/6) | Blocked by #4/#12/#5 interface | Zero route calls during intake, workload identity, atomic reservations/idempotency, redaction and deletion verified |
| [#7 WEB-02: Responsive adaptive journey](https://github.com/UM-NLP/whenshouldgo/issues/7) | Blocked by #4/#12/fake API | Mobile adaptive interview, editable timeline and all recovery states |
| [#8 TEST-01: Verification](https://github.com/UM-NLP/whenshouldgo/issues/8) | Blocked by #12/#5/#6/#7 | Interview/route oracles, multi-process races, browser/device/accessibility matrix and release report |
| [#9 OPS-01: Cheap invite-only deployment](https://github.com/UM-NLP/whenshouldgo/issues/9) | Blocked by #6/#7/#8 | Reproducible pilot project, digest release, hard quotas, kill switches, alerts and rollback evidence |
| [#10 PILOT-01: Adaptive intake and plan validation](https://github.com/UM-NLP/whenshouldgo/issues/10) | Blocked by #9 | 5â€“8 users, 20+ real attempts, constraint/question metrics, sanitized evidence and stop/revise/continue decision |
| [#11 EXPAND-01: PWA/Android gate](https://github.com/UM-NLP/whenshouldgo/issues/11) | On hold | Starts only after #10 demonstrates repeat use and a surface-specific need |
| [#13 VISION-01: Full personal trip assistant](https://github.com/UM-NLP/whenshouldgo/issues/13) | Future epic | Product direction and phased children; current validation issues remain active |
| [#14 AGENT-01: Agent and shared TripPlan contract](https://github.com/UM-NLP/whenshouldgo/issues/14) | Future | After core schema and provider-license decision: model gateway, tool broker, provenance, pending actions and evaluations |
| [#15 CALENDAR-01: Read-only calendar and explicit routines](https://github.com/UM-NLP/whenshouldgo/issues/15) | Future | After Android/account foundation and OAuth/privacy release gates |
| [#16 PROACTIVE-01: Event-driven monitoring and reminders](https://github.com/UM-NLP/whenshouldgo/issues/16) | Future | After calendar sync, notification consent, task idempotency and cost measurement |
| [#17 MULTISTOP-01: Same-day bounded optimizer](https://github.com/UM-NLP/whenshouldgo/issues/17) | Future | After 20 concierge multi-stop plans validate demand |
| [#18 ROADTRIP-01: Multi-day web builder and Android day view](https://github.com/UM-NLP/whenshouldgo/issues/18) | Future | After 10 interviews and 10 real road-trip plans validate a separate segment |
| [#19 VOICE-01: App Actions and assistant-role research](https://github.com/UM-NLP/whenshouldgo/issues/19) | Future | Push-to-talk first; assistant-role/hotword only after privacy, battery, provider-license, policy and retention gates |
| [#1 Adaptive one-way updates](https://github.com/UM-NLP/whenshouldgo/issues/1) | On hold | Resume only if the one-way arrival feature is revisited |
| [#2 Route comparability/strict deadlines](https://github.com/UM-NLP/whenshouldgo/issues/2) | On hold | Resume only if the one-way arrival feature is revisited |
| [#3 Actual arrival evidence/cost](https://github.com/UM-NLP/whenshouldgo/issues/3) | On hold | Historical evidence gap remains unresolved |

## Pilot constraints

- No paid campaign, custom domain, payment system, native app, background worker or always-on server.
- One Cloud Run service with minimum instances zero and maximum one.
- Maximum 24 paid route estimates per plan, 1,200 Routes units for the whole pilot, 650 per day, 100 paid resolver calls, and three full plans per invited user per day.
- Stop live calls through the provider kill switch when quota, billing or quality controls fail.
- Precise trip data stays out of Git, application URLs/history, analytics and server persistence. Provider-derived results also stay out of IndexedDB/cache; explicit Maps handoff sends chosen endpoints to Google.

Status meanings: **Todo** can begin; **Blocked** awaits listed work; **On hold** is intentionally excluded from the active web MVP. Documentation completion does not mean the method or market is validated.

