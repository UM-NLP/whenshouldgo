# Agent Evaluation Plan

Updated 2026-10-10.

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
- At least 99% material factual claims supported by cited, fresh tool evidence.
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

## Security and privacy tests

Test cross-tenant guest/registered objects, jobs and webhooks; malicious place/calendar/review text; altered/expired/replayed approvals; spoofed notifications; exact-location canaries in logs/model traces; consent withdrawal; grounded-result deletion; Calendar disconnect; complete account deletion; quota races; duplicate submissions; anonymous expiry; identity loss; interrupted account linking and merge; read/export/delete after exhaustion; quota attacks and permission denial.

## Release evidence

Every release report distinguishes measured results, assumptions and unexecuted checks. Store sanitized structured traces with prompt/model/tool versions, state transitions, evidence IDs, action outcomes, tokens, latency and cost; exclude raw exact locations, Calendar text, private utterances and provider content that terms prohibit retaining.

