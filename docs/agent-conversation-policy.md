# Agent Conversation and Decision Policy

Updated 2026-10-10.

## Agent responsibilities

The runtime separates five responsibilities:

1. A semantic interpreter proposes typed state changes from user language.
2. A belief store preserves uncertainty, provenance, conflicts and dependencies.
3. A next-action policy chooses whether to ask, research, compare, validate, present, confirm or stop.
4. Typed tools obtain current evidence and propose actions.
5. Optional solvers validate hard schedules, budgets and route order when needed.

The language model may interpret, research, compare and explain. Server policy decides authorization, budgets, state transitions and confirmation requirements. A solver can prove a constrained schedule, but it does not control open-ended discovery.

## Domain boundary

The agent serves trip and outing discovery, planning, execution and recovery, plus app help. It does not answer unrelated general-knowledge, coding, schoolwork, writing, entertainment or professional-advice requests. A server-owned gate classifies each accepted input as `TRAVEL`, `APP_HELP`, `MIXED`, `OUT_OF_SCOPE` or `SAFETY` before the planning loop. The classifier has no tools and returns a small schema. Mixed requests retain only the travel span; out-of-scope and safety classes use fixed application responses.

The model cannot widen this domain. The orchestrator exposes only tools allowed for the validated travel intent and active workspace. Ambiguous input receives one travel-focused clarification. Retrieved text is evidence and cannot convert an unrelated request into an allowed request. See [voice-and-domain-controls.md](voice-and-domain-controls.md).

## Belief graph

```text
TravelSession
  goalHypotheses[]
  travelerGroups[]
  places[] activities[] stays[] transportLegs[]
  temporalGraph
  budget
  preferences[] constraints[] assumptions[]
  decisionVariables[] evidence[] candidatePlans[]
  decisionLedger
  resourceBudget
```

Multiple goal hypotheses may coexist: `FIND_A_PLACE`, `ROUTE_TO_KNOWN_PLACE`, `DAY_PLAN`, `MULTI_DAY_ITINERARY`, `TRANSPORT_RESEARCH` and `REPLAN`.

Every state change is a strict `WorkspacePatch` built by the server. In Phase 0 the model proposes only an intent, an optional search and optional soft facts; the server builds the patch with its own ids, status and next action. The server rejects unknown fields, invalid transitions, missing provenance and attempts to convert an inference into a confirmed hard fact.

## Next action

Candidate actions are typed:

```text
ASK
SEARCH
LOOK_UP_DETAILS
ESTIMATE_TRAVEL
VALIDATE
SOLVE
PRESENT_SHORTLIST
CONFIRM
PROPOSE_ACTION
EXPLAIN_CONFLICT
STOP_UNSUPPORTED
```

The policy prefers the action with the greatest expected reduction in decision loss after user effort, latency, provider cost, privacy cost and irreversibility risk. Hard conflicts and authorization rules override this score.

Ask only when the answer is required for feasibility, likely to materially change the leading options, resolves a high-impact ambiguity or is required before an external/paid action. Otherwise use a visible soft assumption or diverse alternatives.

For vague requests:

1. reuse what the user supplied;
2. establish a planning horizon and starting area only if tools need them;
3. identify hard group needs and fixed commitments;
4. run a cheap broad search;
5. ask the one question that best separates viable options;
6. present up to three contrasting choices;
7. research and schedule only selected finalists.

Targets are zero to two follow-ups for specific requests, no more than three median for partial requests, and no more than four median before useful options for vague exploration. After four unresolved questions, show the editable Trip Board and partial result.

## Uncertainty

Maintain four types separately:

- semantic uncertainty about what the user meant;
- decision uncertainty because destination/time/activity is intentionally open;
- forecast uncertainty in traffic, weather, price or availability;
- evidence uncertainty from missing, conflicting or stale sources.

Use ranges and qualitative states such as `ROBUST`, `BEST_ESTIMATE`, `FRAGILE` and `INFEASIBLE`. Model confidence is never presented as a probability without calibration.

## Confirmation

Explicit structured input is confirmed. Clear facts extracted from prose may be batch-confirmed in one review. Ambiguous hard constraints require a focused question. Inferred preferences may diversify or reorder results but cannot exclude candidates. Tool results are evidence, not user requirements.

Changing date, travelers, destination or fixed anchors invalidates dependent routes, prices, hours and plan versions.

Calendar writes, messages, sharing, bookings, purchases, cancellations and durable memory changes require an itemized preview and explicit confirmation. Any material change to supplier, travelers, time, destination, price, currency or cancellation terms invalidates approval.

## Research stages and cost

Resolve only the area needed for discovery, search broadly with narrow filters, fetch detailed fields for a shortlist, obtain a coarse travel comparison, request full route forecasts only for finalists, validate hard constraints, then recheck volatile evidence before commitment.

Each interactive run has limits for turns, tools, elapsed time, estimated cost and tokens. Initial ceilings are 2,000 tokens per user message, 12,000 compiled input and 1,200 output per planning call, and 40,000 input plus 4,000 output across a run. A run still allows at most six model calls, twelve tool calls and one stronger-model escalation. When a limit ends, return the best validated partial result and unresolved decisions rather than silently extending the loop. A truncated or malformed model result cannot update state or execute an action.

Retrieved place, calendar, review and web text is untrusted. It supplies evidence and citations but cannot choose tools, alter policy or authorize actions.

