# Active Decisions

Updated 2026-10-14. This file contains only decisions that govern the current product.

## Product and validation

1. **Product:** Build an agent-first personal trip assistant centered on a living Trip Workspace. Unknown place, activity, date, time, budget, or travel mode may remain open while the agent discovers and compares options.
2. **First release and phase order:** Validate a same-day local-outing journey on mobile web (Phase 0). Trip history (Phase 1), confirmed patterns and routines (Phase 2) and routine reminder notifications through web push (Phase 3) follow on web, before Android. Android, Calendar, multi-stop and multi-day work stay gated as in [trip-assistant-roadmap.md](trip-assistant-roadmap.md). Android work begins only after the pilot demonstrates useful proposals, action, repeat use, quality, and sustainable cost.
3. **Planning method:** The language model interprets goals, proposes questions, compares grounded options, and explains plans. Typed tools provide places, routes, weather, Calendar context, memory, reminders, and actions. Ordinary Places and Routes API payloads are rendered by the server and are not placed in model context (see decision 17). Deterministic solvers are optional tools for constrained schedules rather than the product's central planner.
4. **Question policy:** Ask the next question only when its answer can materially change feasibility, ranking, safety, permission, or an external action. Present grounded options early and keep assumptions visible and editable.

## Access and cost

5. **Anonymous first use:** Start with a silent anonymous identity and no login screen. A guest receives 50 successful user turns or 10 activated trips, whichever comes first. A registered free account receives 100 successful turns and 10 activated trips per rolling 30 days. Limits are versioned pilot hypotheses.
6. **Model hosting:** Use a provider-neutral server gateway with a low-cost open model reached through OpenRouter as the default and at most one bounded stronger-model escalation. Every request pins the provider and requires zero data retention and no training use. Gemini is not used in the first version. Do not self-host during pre-seed validation. Details are in [model-hosting-decision.md](model-hosting-decision.md).
7. **Deployment:** Use a scale-to-zero managed backend with strict user, workspace, daily, and pilot-wide quotas. Provider credentials remain server-side.

## Scope and safety

8. **Travel-only boundary:** Serve trip and outing discovery, planning, execution, and recovery plus app help. A server-owned gate classifies requests before the planning model. Out-of-scope requests receive a fixed redirect with no search, route, or general-answer tool call.
9. **Tool authority:** The server owns tool allowlists, permissions, state transitions, evidence freshness, and external-action approval. Model output is an untrusted proposal until schema, policy, evidence, and budget validation succeeds.
10. **Token ceilings:** Limit user input to 2,000 tokens; each planning call to 12,000 compiled input and 1,200 output tokens; and each run to 40,000 input, 4,000 output, six model calls, twelve tool calls, and one escalation. Truncated, malformed, or over-budget output cannot update state or trigger an action.
11. **Voice activation:** Android starts with an in-app press-and-hold microphone button. Release submits, Cancel discards, capture stops after 60 seconds, and raw audio is discarded after transcription. The microphone is otherwise off.
12. **User control:** Calendar, location, trip history, pattern suggestions, notifications, and monitoring are requested in context, each with its own consent, and remain optional. Persistent memories, history and assumptions are visible, editable, and deletable. External writes require a preview and explicit confirmation.

## Quality gates

13. **Grounding:** Material claims about places, hours, routes, travel time, weather, availability, or disruptions require fresh provider evidence and provenance. Google Maps grounded output is shown verbatim with its sources and counts as that evidence.
14. **Evaluation:** Do not expand beyond the validation release until offline boundary, safety, grounding, token, identity, and cost gates pass and at least 30 supported real goals demonstrate repeat value. Google Maps content and grounded output may not be used to test or validate AI systems, so offline fixtures use synthetic place data; live traffic is measured only on structural checks.
15. **No precision promise:** Recommendations communicate evidence, freshness, uncertainty, and alternatives. The product does not promise exact or guaranteed arrival times.

## Data, history and proactive features

16. **Discovery path:** Open-ended discovery uses Google Maps Grounding Lite through a server-side MCP client with the OpenRouter model. The grounded output is displayed unmodified with its Google Maps sources immediately after it. The server may add framing above it but cannot edit, trim, reorder or intersperse it. Grounding Lite's launch stage and Canadian eligibility are tracked as a legal gate in [docs/legal/open-questions.md](legal/open-questions.md).
17. **Places and Routes payloads:** Until a written Google determination says otherwise, ordinary Places and Routes API payloads are fetched and rendered by the server and are not placed in LLM context, text-to-speech or durable history. The model sees only opaque place IDs and the user's own data for these. Evidence carries an `llm_visible` flag so this can change without a redesign. The owner has asked to revisit this with a written determination; it stays an open decision until one is on file.
18. **Maps content storage:** Store only place IDs (indefinitely) and user data. Grounded output may be cached only for redisplay with a hard maximum of 30 consecutive days (default 24 hours). Names, addresses, hours, ratings, categories and reviews are re-fetched by place ID at display time and are not stored. Coordinates are cached at most 30 days.
19. **History:** A per-user outing history stores only user actions and user-supplied data (place ID, the user's label, an activity tag derived from the user's own request, timestamp, outcome and feedback). It is opt-in, with a 180-day rolling retention ceiling that the user can shorten or switch off but not extend for now (owner decision, 2026-10-15), and the notice states the period. Guests keep it on the device; registered users may store it on the server. See [data-and-history-design.md](data-and-history-design.md).
20. **Patterns and routines:** From Phase 2, patterns are computed deterministically from stored history. A pattern is shown to the user with its basis and becomes a routine only through a confirmed `PendingAction`. Nothing inferred is stored or treated as a hard fact without the user's confirmation.
21. **Routine reminder notifications:** From Phase 3, a registered user who has confirmed a routine and separately opted in may receive a web-push reminder at the routine's time. The text is generic and names no place. There is no model call, no Maps content and no background location in the trigger path. Guests receive none.
22. **Legal gates:** Legal and privacy questions that gate a phase are tracked in [docs/legal/open-questions.md](legal/open-questions.md) and in GitHub issues. The legal summary supplied by the owner on 2026-10-13 is stored in [docs/legal/google-maps-llm-policy-summary.md](legal/google-maps-llm-policy-summary.md). Neither is legal advice or a determination from Google.
