# Runtime agent

Use this role for work in `app/` that coordinates a turn: the workspace, the
orchestrator, the model gateway, the tool registry, patch validation and pending
actions.

Read `AGENTS.md`, `docs/trip-assistant-architecture.md`,
`docs/agent-conversation-policy.md` and `docs/model-hosting-decision.md` first.
Preserve this order: identity and quota checks, domain gate, narrow tool
selection, untrusted proposal validation, immutable plan proposal, then explicit
action confirmation.

- `TripWorkspace` lifecycle: `CAPTURED -> FRAMING -> EXPLORING -> PROPOSED ->
  CONFIRMED -> ARMED -> ACTIVE -> ADAPTING -> COMPLETED`, plus `CANCELLED`,
  `EXPIRED`, `UNSUPPORTED`. Only validated server commands confirm, arm or
  activate a plan; plan versions are immutable.
- Model output is a strict `WorkspacePatch`. Reject unknown fields, invalid
  transitions, missing provenance, stale evidence and any inference promoted to a
  hard fact. Changing date, travelers, destination or a fixed anchor invalidates
  dependent evidence and plans.
- The gateway is provider-neutral: `generate(workspaceContext, allowedTools,
  outputSchema, budget, privacyClass, groundingPolicy)`. Pin model and prompt
  versions; record tokens, latency and cost without raw content.
- Run limits: 6 model calls, 12 tool calls, 1 escalation, 40,000 input and 4,000
  output tokens. At any limit return the best validated partial result with the
  unresolved choices; never silently extend a budget.
- At most one question per turn and up to three contrasting options.
- Search many, fetch details for a shortlist, route finalists only.

Known gaps in the current foundation: `app/main.py` accepts `principal_id` and
`principal_kind` from the request body, and `app/quota.py` hard-codes the
allowances. Both must move to server-derived identity and versioned policy.
