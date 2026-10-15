# GitHub issue drafts

Updated 2026-10-14. These drafts were prepared for the repository `UM-NLP/whenshouldgo` and have not been created: the coding session had no GitHub access. Create each as an issue, add them to Project 2, then replace the “TBD” numbers in [../backlog.md](../backlog.md) and [../project-board.md](../project-board.md). Legal items are research follow-ups, not legal advice; each needs counsel or a written Google answer before its gate passes. Context: [open-questions.md](open-questions.md).

## Legal and privacy issues

### LEGAL-01: Confirm Grounding Lite launch stage and Canadian eligibility
Phase 0 entry gate. Research found conflicting sources: one says Experimental (no SLA, support or deprecation policy), another lists a paid SKU. Regional availability for Canadian billing accounts was not stated.
- Ask Google (question 1 in `docs/legal/open-questions.md`) and test enabling the API on a Canadian-billed project.
- Record the answer, date and case number in `docs/legal/`.
- Decide with the owner: if Experimental or Preview, run Phase 0 as pilot-only on pre-GA terms with a degradation path.
Done when: the launch stage and eligibility are on file, or the owner has recorded the pilot-only decision.

### LEGAL-02: Written processor terms for OpenRouter and the upstream model host
Phase 0 entry gate. Grounding Lite terms require that the model provider not store, cache or train on Maps content.
- Choose candidate hosts from the model bake-off; read each host's retention and training terms.
- Request a data processing agreement and sub-processor list from OpenRouter (none is published).
- Get Google's answer on whether zero data retention with in-memory prompt caching meets “not cached by the LLM” (question 2).
- Save the terms in `docs/legal/` and review each host's stated retention monthly.
Done when: terms for the chosen host are on file and Google's answer to question 2 is recorded.

### LEGAL-03: Google written answers on place IDs, patterns, ML limits and ordinary Places in an LLM
Questions 3–8 in `docs/legal/open-questions.md`. Gates: place-ID storage (Phase 0 stopgap otherwise), pattern computation (Phase 2), the train/test/validate scope (evaluation design), and the owner's request to let the LLM read ordinary Places data (decision 17, `Evidence.llm_visible`).
Done when: written answers are on file and decisions 17 and 18 and `docs/data-and-history-design.md` are updated or confirmed.

### LEGAL-04: Privacy policy, terms of use and consent notice wording
Phase 0 entry gate. Must include the Google Maps terms and privacy notice links, name Google, OpenRouter and the upstream host as processors, state processing outside Canada and foreign-authority access, state the 180-day default history retention, and describe each consent separately (location, history, pattern suggestions, notifications). Add a safety disclaimer for dietary and accessibility information.
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
Done when: the above exist with tests and `docs/agent-evaluation-plan.md` states the sample size and pass rule.

### HISTORY-01: Opt-in trip history (Phase 1)
`OutingRecord` stored server-side for registered users; guests keep it on the device. Place IDs and user data only; history view rebuilt from place IDs with attribution; edit, delete, export; 180-day default retention adjustable; consent ledger; place-ID refresh job.
Done when: the Phase 1 exit gate in `docs/trip-assistant-roadmap.md` passes. Depends on Phase 0 exit, LEGAL-02, LEGAL-03 (question 3) and LEGAL-04.

### ROUTINE-01: Patterns and confirmed routines (Phase 2)
Deterministic pattern derivation, in-app proposals, `CREATE_ROUTINE` pending action, routine CRUD, “never suggest”, sensitive-inference rule. Narrow the AGENTS.md inferred-memory rule per decision 20 once the gate passes.
Done when: the Phase 2 exit gate passes. Depends on HISTORY-01, LEGAL-03 (questions 4–5) and LEGAL-05.

### NOTIFY-01: Routine reminder notifications over web push (Phase 3)
`NotificationPreference`, `PushSubscription`, `NotificationOutbox`, daily planner, Cloud Task per occurrence, handler checks (version, consent, quiet hours, caps, dedupe, opt-out), generic payload, stop action, in-app inbox, installed-PWA flow for iOS.
Done when: the Phase 3 exit gate passes. Depends on ROUTINE-01 and LEGAL-06.

### Updates to existing issues
- **#14:** add `schema_version`, `Evidence` and `PlanVersion` changes from `docs/data-and-history-design.md`, typed `TurnResponse` and error model, OpenRouter gateway that fails closed without pinned provider and zero retention, and the Maps content taint type.
- **#20:** discovery through Grounding Lite shown verbatim; server-rendered Places Details and Routes; in-app reminder; local outing log.
- **#21:** synthetic Maps simulator; structural live checks; re-baseline the corpus size and scope (multi-day and DST cases belong to later phases).
- **#16 PROACTIVE-01:** now depends on NOTIFY-01, not on Calendar (#15).
- **#15 CALENDAR-01:** no longer a prerequisite for routines or notifications.
