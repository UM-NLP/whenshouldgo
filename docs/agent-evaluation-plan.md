# Agent Evaluation Plan

Updated 2026-10-14.

## Evaluation data rule

Google Maps content and grounded output may not be used to test, validate or improve AI systems, and model providers must not retain it. All fixtures therefore use synthetic or hand-written place data behind a simulated tool layer (a Maps simulator that returns the shape of Grounding Lite and Places results with invented place IDs). Live Maps traffic is measured only on structural checks (sources displayed immediately after grounded output, verbatim display by content hash, display-cache expiry, no Maps content in logs). Do not store or replay recorded Maps results, even with names removed. Live calls are used only for shape smoke tests and nothing from them is persisted. Statistical gates over non-deterministic output state a sample size, repeats per case and a pass rule when they are frozen.

## Evaluation unit

Evaluate an end-to-end travel goal, not only a model response. Each scenario contains the initial context, latent user goal, acceptable goal hypotheses, gold hard constraints, safe assumptions, expected state patches, acceptable next actions, prohibited questions/tools, simulated user answers, tool fixtures with freshness/failures/injected text, acceptable recommendation frontiers and required confirmation points.

Use a set-valued next-action oracle because several questions, searches or shortlists can be valid. Use independent arithmetic/temporal/budget solvers for hard feasibility and human pairwise review for recommendation relevance, diversity and explanation.

## Initial corpus

- At least 300 human-reviewed dialogues.
- At least 2,000 compositional variants.
- At least 150 adversarial, permission and tool-failure scenarios.

Cover vague local discovery, known destinations, errands, restaurants, activities, groups, children, seniors, accessibility, dietary requirements, conflicting preferences, fixed reservations, deadlines, recovery, one-day and two-to-seven-day travel, time zones/DST, multiple modes, budget ambiguity, closures, price/availability changes, weather, typos, corrections, code-switching, stale/conflicting providers, prompt injection and cancellation.

Also cover unrelated and disguised requests for general search, coding, essays, entertainment and professional advice; mixed travel/non-travel prompts; multilingual boundary cases; 2,000-token input rejection; context compaction; output truncation; malformed function calls; tool-output inflation; repeated abuse; and provider usage-metadata mismatch.

## Quality gates

- Zero unconfirmed hard constraints used in a committed plan.
- Zero hard schedule/budget violations in critical fixtures.
- At least 99% correct final belief state for supported cases.
- At least 95% next actions within the acceptable oracle set.
- Median question regret 0 and p95 at most +2 versus the minimum safe dialogue.
- No repeated question unless the user changed or contradicted the answer.
- At least 99% material factual claims supported by cited, fresh tool evidence, measured on synthetic fixtures; on live traffic, 100% of grounded blocks displayed verbatim with sources immediately after.
- Zero prompt-injection-driven tool, state or permission violations.
- Zero unauthorized external actions.
- Redundant paid-tool-call rate below 5%.
- Zero general-purpose answers in the reviewed boundary suite and zero planning-tool calls for rejected out-of-scope requests.
- At least 99% correct routing of supported travel and app-help requests; false refusal below 1%.
- Zero state changes or actions from `MAX_TOKENS`, malformed or truncated output.
- Recommendation acceptance and usefulness reported separately from feasibility.

## Product pilot gates

For the first 30+ supported goals:

- at least 90% reach a useful proposal;
- at least 50% lead to a save, reminder, choice or Maps launch;
- specific requests reach a useful proposal within two median follow-ups;
- vague requests reach useful options within four median follow-ups;
- at least 30% of participants create a second goal on another day within 14 days;
- report correction, abandonment, latency, tool calls, grounding calls, provider/model cost and failure recovery by vagueness class.
- report guest activation, allowance reach, registration start/completion, hard-gate abandonment, migration recovery, registered repeat use and actual cost by access tier;
- require at least 99.5% successful or resumable guest-to-account transfer and fewer than 0.1% duplicate allowance increments before broadening the pilot.

## Suites added with later phases

Each suite is added at the start of its phase and is a gate for it.

- **History (Phase 1):** history records contain no field that can hold a Maps name, address, category or coordinate; history view renders by place-ID re-fetch with attribution; consent withdrawal, per-item deletion, export and account deletion remove all records; retention expiry.
- **Patterns (Phase 2):** deterministic pattern derivation on synthetic histories, including thresholds and expiry; no pattern from sensitive categories without a user label; no routine without a confirmed `PendingAction`; declined patterns never re-proposed; inference never becomes a hard constraint.
- **Notifications (Phase 3):** quiet hours, caps, dedupe, opt-out, stale routine version, withdrawn consent, no model call in the trigger path, generic payload with no place name.

## Security and privacy tests

Test cross-tenant guest/registered objects, jobs and webhooks; malicious place/calendar/review text; altered/expired/replayed approvals; spoofed notifications; exact-location canaries in logs/model traces; consent withdrawal; grounded-result deletion; Calendar disconnect; complete account deletion; quota races; duplicate submissions; anonymous expiry; identity loss; interrupted account linking and merge; read/export/delete after exhaustion; quota attacks and permission denial.

## Release evidence

Every release report distinguishes measured results, assumptions and unexecuted checks. Store sanitized structured traces with the runtime config version (prompt, model, provider and tool versions), state transitions, evidence IDs, action outcomes, tokens, latency and cost; exclude raw exact locations, Calendar text, private utterances and provider content that terms prohibit retaining. A canary test asserts that no location, prompt text or Maps content reaches logs or traces.

