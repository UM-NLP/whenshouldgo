# Backlog

Updated 2026-10-10. The active direction is a low-cost, mobile-first web application and assisted private pilot. Native Android is gated by web evidence. The coordinator maintains this backlog and the [Project 2 snapshot](project-board.md).

| Item | Status | Dependency / completion evidence |
|---|---|---|
| Product, architecture, quality and roadmap documentation | Done | [Product](web-product-plan.md), [architecture](web-architecture.md), [roadmap](web-roadmap.md), and [quality/operations](web-quality-operations.md) reviewed locally |
| [#4 WEB-01: Product/schema/acceptance/cost contract](https://github.com/UM-NLP/whenshouldgo/issues/4) | Todo | No dependency; freeze exact-second objective, search completeness, pilot denominators, provider policy and cost contract |
| [#5 CORE-01: Planner and oracle](https://github.com/UM-NLP/whenshouldgo/issues/5) | Blocked by #4 | Independent oracle, reserved baseline, zero false-feasible results, p95 regret <=5 road minutes, <=24 estimates |
| [#6 API-01: Provider and secure API](https://github.com/UM-NLP/whenshouldgo/issues/6) | Blocked by #4/#5 interface | Workload identity, typed operations, atomic reservations/idempotency, redaction and deletion verified |
| [#7 WEB-02: Responsive end-to-end journey](https://github.com/UM-NLP/whenshouldgo/issues/7) | Blocked by #4/fake API | Complete mobile-browser journey and all recovery states |
| [#8 TEST-01: Verification](https://github.com/UM-NLP/whenshouldgo/issues/8) | Blocked by #5/#6/#7 | Independent oracle, multi-process races, browser/device/accessibility matrix and release report |
| [#9 OPS-01: Cheap invite-only deployment](https://github.com/UM-NLP/whenshouldgo/issues/9) | Blocked by #6/#7/#8 | Reproducible pilot project, digest release, hard quotas, kill switches, alerts and rollback evidence |
| [#10 PILOT-01: Assisted web validation](https://github.com/UM-NLP/whenshouldgo/issues/10) | Blocked by #9 | 5–8 users, 20+ real attempts, sanitized evidence and stop/revise/continue decision |
| [#11 EXPAND-01: PWA/Android gate](https://github.com/UM-NLP/whenshouldgo/issues/11) | On hold | Starts only after #10 demonstrates repeat use and a surface-specific need |
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

