# Backlog

Updated 2026-10-10. The product has been redesigned as an agent-first personal trip assistant centered on a Trip Workspace. The active validating release is a low-cost mobile web agent for vague or named same-day local outings. Android, Calendar, proactive assistance, multi-stop and multi-day capabilities are gated by evidence. The coordinator maintains this backlog and the [Project 2 snapshot](project-board.md).

| Item | Status | Dependency / completion evidence |
|---|---|---|
| Planner-centered web documentation | Superseded | Preserved in web-product-plan.md, web-architecture.md, web-roadmap.md and web-quality-operations.md for reusable requirements and history |
| Agent-first product, architecture, policy, model, security, evaluation and roadmap | Done | [Product](trip-assistant-product-plan.md), [architecture](trip-assistant-architecture.md), [conversation policy](agent-conversation-policy.md), [model](model-hosting-decision.md), [security](trip-assistant-security.md), [evaluation](agent-evaluation-plan.md) and [roadmap](trip-assistant-roadmap.md); implementation and validation remain Todo |
| [#13 VISION-01: Full personal trip assistant](https://github.com/UM-NLP/whenshouldgo/issues/13) | Active epic | Agent-first direction and phased children |
| [#14 AGENT-01: Trip Workspace and agent runtime](https://github.com/UM-NLP/whenshouldgo/issues/14) | Todo | Workspace/belief contracts, model gateway, typed tools, evidence validator, pending actions and cost ledger |
| [#22 ACCESS-01: Anonymous guest and registered-free tiers](https://github.com/UM-NLP/whenshouldgo/issues/22) | Todo with #14 | Silent Firebase guest, 50-turn/10-trip policy, local-first retention, free account linking/merge, quota ledger and access-tier evaluation; [specification](access-tiers.md) |
| [#20 AGENT-MVP-01: Same-day local outing web concierge](https://github.com/UM-NLP/whenshouldgo/issues/20) | Todo after #14 | Vague/named discovery, grounded shortlist, Trip Board, finalist route checks, Maps handoff and outcome capture |
| [#21 AGENT-EVAL-01: Quality, safety, cost and pilot](https://github.com/UM-NLP/whenshouldgo/issues/21) | Blocked by #20 | 300 reviewed dialogues, 2,000 variants, 150 adversarial cases, then 30+ real goals and evidence-gated decision |
| [#11 EXPAND-01: PWA/Android gate](https://github.com/UM-NLP/whenshouldgo/issues/11) | On hold | Starts only after #21 demonstrates repeat use and a surface-specific need |
| [#15 CALENDAR-01: Read-only calendar and explicit routines](https://github.com/UM-NLP/whenshouldgo/issues/15) | Future | After Android/account foundation and OAuth/privacy release gates |
| [#16 PROACTIVE-01: Event-driven monitoring and reminders](https://github.com/UM-NLP/whenshouldgo/issues/16) | Future | After calendar sync, notification consent, task idempotency and cost measurement |
| [#17 MULTISTOP-01: Same-day bounded optimizer](https://github.com/UM-NLP/whenshouldgo/issues/17) | Future | After 20 concierge multi-stop plans validate demand |
| [#18 ROADTRIP-01: Multi-day web builder and Android day view](https://github.com/UM-NLP/whenshouldgo/issues/18) | Future | After 10 interviews and 10 real road-trip plans validate a separate segment |
| [#19 VOICE-01: App Actions and assistant-role research](https://github.com/UM-NLP/whenshouldgo/issues/19) | Future | Push-to-talk first; assistant-role/hotword only after privacy, battery, provider-license, policy and retention gates |
| [#1 Adaptive one-way updates](https://github.com/UM-NLP/whenshouldgo/issues/1) | On hold | Resume only if the one-way arrival feature is revisited |
| [#2 Route comparability/strict deadlines](https://github.com/UM-NLP/whenshouldgo/issues/2) | On hold | Resume only if the one-way arrival feature is revisited |
| [#3 Actual arrival evidence/cost](https://github.com/UM-NLP/whenshouldgo/issues/3) | On hold | Historical evidence gap remains unresolved |
| Issues #4–#10 and #12 planner-centered web plan | On hold | Superseded as the active product path; reusable constraint, API, testing and operations requirements will be pulled into #14 and the agent MVP |

## Agent pilot constraints

- No paid campaign, custom domain, payment system, native app, background worker or always-on server.
- No login screen before value. Guests use a silent anonymous principal; no model or provider endpoint is publicly unauthenticated.
- Guest allowance begins at 50 successful user turns or 10 activated trips, lifetime. Registered free begins at 100 turns and 10 trips per rolling 30 days. These are versioned validation hypotheses, not pricing promises.
- One Cloud Run service with minimum instances zero and maximum one.
- Per interactive workspace: at most six model turns, twelve tool calls, one stronger-model escalation, and frozen caps for Maps-grounding queries, place-detail calls, route finalists, elapsed time and estimated dollars.
- Freeze account, per-user/day and whole-pilot spend caps only after a one-user live smoke test reconciles actual provider billing. Stop live calls through model and provider kill switches when cost, quota, grounding or quality controls fail.
- Search broadly, fetch details for a shortlist and route finalists only. Do not build route matrices for raw discovery candidates.
- Precise trip data stays out of Git, URLs and analytics. Phase 0 session data is local/ephemeral; Google Maps Grounded Results follow their specific display, provider-processing and chat-history retention terms.

Status meanings: **Todo** can begin; **Blocked** awaits listed work; **On hold** is intentionally excluded from the active web MVP. Documentation completion does not mean the method or market is validated.

