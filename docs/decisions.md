# Active Decisions

Updated 2026-10-11. This file contains only decisions that govern the current product.

## Product and validation

1. **Product:** Build an agent-first personal trip assistant centered on a living Trip Workspace. Unknown place, activity, date, time, budget, or travel mode may remain open while the agent discovers and compares options.
2. **First release:** Validate a same-day local-outing journey on mobile web before building the Android product. Android work begins only after the pilot demonstrates useful proposals, action, repeat use, quality, and sustainable cost.
3. **Planning method:** The language model interprets goals, proposes questions, compares evidence, and explains plans. Typed tools provide places, routes, weather, Calendar context, memory, reminders, and actions. Deterministic solvers are optional tools for constrained schedules rather than the product's central planner.
4. **Question policy:** Ask the next question only when its answer can materially change feasibility, ranking, safety, permission, or an external action. Present grounded options early and keep assumptions visible and editable.

## Access and cost

5. **Anonymous first use:** Start with a silent anonymous identity and no login screen. A guest receives 50 successful user turns or 10 activated trips, whichever comes first. A registered free account receives 100 successful turns and 10 activated trips per rolling 30 days. Limits are versioned pilot hypotheses.
6. **Model hosting:** Use a provider-neutral server gateway with a low-cost hosted Gemini model as the default and at most one bounded stronger-model escalation. Do not self-host during pre-seed validation.
7. **Deployment:** Use a scale-to-zero managed backend with strict user, workspace, daily, and pilot-wide quotas. Provider credentials remain server-side.

## Scope and safety

8. **Travel-only boundary:** Serve trip and outing discovery, planning, execution, and recovery plus app help. A server-owned gate classifies requests before the planning model. Out-of-scope requests receive a fixed redirect with no search, route, or general-answer tool call.
9. **Tool authority:** The server owns tool allowlists, permissions, state transitions, evidence freshness, and external-action approval. Model output is an untrusted proposal until schema, policy, evidence, and budget validation succeeds.
10. **Token ceilings:** Limit user input to 2,000 tokens; each planning call to 12,000 compiled input and 1,200 output tokens; and each run to 40,000 input, 4,000 output, six model calls, twelve tool calls, and one escalation. Truncated, malformed, or over-budget output cannot update state or trigger an action.
11. **Voice activation:** Android starts with an in-app press-and-hold microphone button. Release submits, Cancel discards, capture stops after 60 seconds, and raw audio is discarded after transcription. The microphone is otherwise off.
12. **User control:** Calendar, location, memory, notifications, and monitoring are requested in context and remain optional. Persistent memories and assumptions are visible, editable, and deletable. External writes require a preview and explicit confirmation.

## Quality gates

13. **Grounding:** Material claims about places, hours, routes, travel time, weather, availability, or disruptions require fresh provider evidence and provenance.
14. **Evaluation:** Do not expand beyond the validation release until offline boundary, safety, grounding, token, identity, and cost gates pass and at least 30 supported real goals demonstrate repeat value.
15. **No precision promise:** Recommendations communicate evidence, freshness, uncertainty, and alternatives. The product does not promise exact or guaranteed arrival times.