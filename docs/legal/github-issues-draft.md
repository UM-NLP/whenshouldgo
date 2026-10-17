# GitHub issue drafts

Updated 2026-10-17. These drafts were prepared for the repository `UM-NLP/whenshouldgo` and created as issues #24 to #35 on 2026-10-17 (LEGAL-01 to LEGAL-07 are #24 to #30, LLMOPS-01 #31, HISTORY-01 #32, ROUTINE-01 #33, NOTIFY-01 #34, ACCESS-02 #35) and added to Project 2. The "Updates to existing issues" section was posted as comments on #14, #15, #16, #20 and #21. This file stays as the source text; the issues are authoritative. Legal items are research follow-ups, not legal advice; each needs counsel or a written Google answer before its gate passes. Context: [open-questions.md](open-questions.md).

## Legal and privacy issues

### LEGAL-01: Confirm Grounding Lite launch stage and Canadian eligibility
Phase 0 entry gate. Update 2026-10-15: an owner-supplied analysis reports Grounding Lite as Generally Available (announced 22 April 2026) with a 99.9% uptime objective and no Canadian exclusion, but says it did not verify the owner's account. Earlier research had found conflicting sources (one said Experimental). A live capture on 2026-10-15 reached the endpoint and listed tools, but every tool call returned HTTP 403 "blocked" until the key's API restrictions and enablement are fixed. Confirm the stage and eligibility in the Cloud console for the owner's project.
- Ask Google (question 1 in `docs/legal/open-questions.md`) and test enabling the API on a Canadian-billed project.
- Record the answer, date and case number in `docs/legal/`.
- Decide with the owner: if Experimental or Preview, run Phase 0 as pilot-only on pre-GA terms with a degradation path.
Done when: the launch stage and eligibility are on file, or the owner has recorded the pilot-only decision.

### LEGAL-02: Written processor terms for OpenRouter and the upstream model host
Phase 0 entry gate. Grounding Lite terms require that the model provider not store, cache or train on Maps content.
- The bake-off chose DeepInfra (Gemma 4 31B primary, Qwen3-Next second, 2026-10-17); read DeepInfra's retention, training, caching and data-location terms and request its data processing agreement.
- Request a data processing agreement and sub-processor list from OpenRouter (none is published).
- Get Google's answer on whether zero data retention with in-memory prompt caching meets “not cached by the LLM” (question 2). The 2026-10-15 analysis says it cannot be confirmed compliant: OpenRouter allows implicit in-memory prompt caching under zero retention. Verify and disable reusable prompt caching on every allowlisted upstream endpoint and fallback, or get Google's written acceptance, and record it per provider.
- Save the terms in `docs/legal/` and review each host's stated retention monthly.
Done when: terms for the chosen host are on file and Google's answer to question 2 is recorded.

### LEGAL-03: Google written answers on place IDs, patterns, ML limits and ordinary Places in an LLM
Questions 3–8 in `docs/legal/open-questions.md`. Gates: place-ID storage (Phase 0 stopgap otherwise), pattern computation (Phase 2), the train/test/validate scope (evaluation design), and the owner's request to let the LLM read ordinary Places data (decision 17, `Evidence.llm_visible`).
Done when: written answers are on file and decisions 17 and 18 and `docs/data-and-history-design.md` are updated or confirmed.

### LEGAL-04: Privacy policy, terms of use and consent notice wording
Phase 0 entry gate. Must include the Google Maps terms and privacy notice links, name Google, OpenRouter and the upstream host as processors, state processing outside Canada and foreign-authority access, state the 180-day maximum history retention, and describe each consent separately (location, history, pattern suggestions, notifications). Add a safety disclaimer for dietary and accessibility information.
Done when: counsel has approved text and the consent ledger stores the notice version.

### LEGAL-05: Counsel review of profiling, inferences and Quebec Law 25
Phase 2 entry gate. Items 5–8 and 10–11 of the counsel checklist. Includes whether inferences are personal information for access and correction, whether pattern suggestions need express opt-in, the sensitive-inference rule, Quebec profiling and privacy impact assessment, and retention maximums.
Done when: counsel's written view is on file and the Phase 2 design reflects it.

### LEGAL-06: Counsel review of CASL for push notifications
Phase 3 entry gate. Items 1–4 of the counsel checklist. Also verify iOS web-push requirements and FCM terms.
Done when: counsel's written view is on file and the consent, unsubscribe and logging design reflects it.

### LEGAL-07: Save the live Google terms and re-verify the owner's legal summary
The research could not load the current Maps Platform Terms and Service Specific Terms. Save the live versions as dated PDFs in `docs/legal/`, then check the unverified points listed in `docs/legal/open-questions.md`. Repeat before launch and whenever Google changes the terms.

## Engineering issues

### LLMOPS-01: Runtime config, telemetry allowlist, cost ledger and eval pipeline (Phase 0)
Part of the #14 contract freeze.
- `RuntimeConfig` bundle (model, provider, prompt hashes, tool schema versions, classifier and policy versions, TTLs, per-phase flags) stamped on every turn; rollback by config version; kill switches.
- Typed trace schema with an allowlist and a log-canary test (no locations, prompt text or Maps content), added before the first model call.
- Versioned price table and cost ledger covering the model, Grounding Lite, Places and Routes; dollar caps per goal, per tier per day and pilot-wide, with fallback behavior at exhaustion.
- Eval pipeline: recorded synthetic suite on every pull request; live-model suite on a schedule or config change with repeats; statistical pass rule for the “zero” and “99%” gates.
- Provider failure ladder (rate limit, quota, empty grounding, timeout) and circuit breaker thresholds.
- Built so far on branch `feat/gateway-and-telemetry`: fail-closed OpenRouter gateway, resilience ladder, typed telemetry allowlist and log guard, canary tests. Remaining: real prices in the price table, per-run dollar ceiling, a shared (Firestore) kill-switch flag replacing the per-process 402 latch, wiring the gateway into the orchestrator (the strict xfail `test_orchestrator_path_never_sends_maps_text_to_frame_or_compose` must pass first), verifying the OpenRouter response `provider` field format, a dependency lockfile with hashes and `pip-audit` in CI, a pinned Python 3.12 in CI, Ruff in CI (the print ban is untested locally), a dashboard or alert on telemetry `health()` counters, and a decision on whether a rate-limited (429) call should be charged at the worst-case reservation.
Done when: the above exist with tests and `docs/agent-evaluation-plan.md` states the sample size and pass rule.

### HISTORY-01: Opt-in trip history (Phase 1)
`OutingRecord` stored server-side for registered users; guests keep it on the device. Place IDs and user data only; history view rebuilt from place IDs with attribution; edit, delete, export; 180-day retention ceiling the user can shorten; consent ledger; place-ID refresh job.
Done when: the Phase 1 exit gate in `docs/trip-assistant-roadmap.md` passes. Depends on Phase 0 exit, LEGAL-02, LEGAL-03 (question 3) and LEGAL-04.

### ROUTINE-01: Patterns and confirmed routines (Phase 2)
Deterministic pattern derivation, in-app proposals, `CREATE_ROUTINE` pending action, routine CRUD, “never suggest”, sensitive-inference rule. Narrow the AGENTS.md inferred-memory rule per decision 20 once the gate passes.
Done when: the Phase 2 exit gate passes. Depends on HISTORY-01, LEGAL-03 (questions 4–5) and LEGAL-05.

### NOTIFY-01: Routine reminder notifications over web push (Phase 3)
`NotificationPreference`, `PushSubscription`, `NotificationOutbox`, daily planner, Cloud Task per occurrence, handler checks (version, consent, quiet hours, caps, dedupe, opt-out), generic payload, stop action, in-app inbox, installed-PWA flow for iOS.
Done when: the Phase 3 exit gate passes. Depends on ROUTINE-01 and LEGAL-06.

### ACCESS-02: Shared spend controls and abuse limits (gate before any pilot traffic)
Found by the second security review of the orchestrator wiring (2026-10-16). The per-user cost ceiling, the paid-failed-turn limiter, the 402 latch and the Grounding Lite kill switch are per user and per process. A script that creates new anonymous accounts gets a fresh daily ceiling, limiter and allowance each time, and Cloud Run scale-to-zero or a second instance starts with an empty ledger. `docs/access-tiers.md` and `docs/trip-assistant-security.md` already require global and whole-pilot budgets, coarse IP buckets, App Check and a global cost kill switch.
- Firestore-backed cost ledger and kill switches shared across instances (atomic reserve against the ledger, sharded counters).
- Global rolling spend counter and a daily project cap checked before the first model call, with a kill switch.
- Identity-creation rate limit and coarse short-retention IP bucket at session bootstrap.
- App Check signal required before any paid call.
- Decided 2026-10-16 (option A): provider-side failures are recorded in the cost ledger at worst-case charge but do not count toward a user's daily ceiling. The ledger exposes a provider-attributed total (`provider_total`, `provider_usage_for`), which this item must consume in a whole-pilot budget and in the shared Firestore ledger, because nothing else caps outage spend.
Done when: guest churn and multi-instance tests show a bounded total spend, including provider-attributed spend, against a whole-pilot budget.

### Updates to existing issues
- **#14:** add `schema_version`, `Evidence` and `PlanVersion` changes from `docs/data-and-history-design.md`, typed `TurnResponse` and error model, OpenRouter gateway that fails closed without pinned provider and zero retention, and the Maps content taint type.
- **#20:** discovery through Grounding Lite shown verbatim; server-rendered Places Details and Routes; in-app reminder; local outing log.
- **#21:** synthetic Maps simulator; structural live checks; re-baseline the corpus size and scope (multi-day and DST cases belong to later phases).
- **#16 PROACTIVE-01:** now depends on NOTIFY-01, not on Calendar (#15).
- **#15 CALENDAR-01:** no longer a prerequisite for routines or notifications.
