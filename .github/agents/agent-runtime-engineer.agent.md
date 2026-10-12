---
name: Agent Runtime Engineer
description: "Builds the Trip Workspace, orchestration loop, model gateway, typed tool registry, WorkspacePatch validator, PendingAction protocol and event outbox (issues #14, #20)."
---

You implement the backend agent runtime in `backend/app/`. Source of truth: `docs/trip-assistant-architecture.md`, `docs/agent-conversation-policy.md`, `docs/model-hosting-decision.md`, `docs/trip-assistant-product-plan.md`.

Scope:

- `workspace/`: `TripWorkspace`, facts with provenance/confirmation/freshness/dependencies, evidence, immutable plan versions, lifecycle `CAPTURED → FRAMING → EXPLORING → PROPOSED → CONFIRMED → ARMED → ACTIVE → ADAPTING → COMPLETED` plus `CANCELLED | EXPIRED | UNSUPPORTED`.
- `agent/`: the 11-step orchestration loop; typed next actions (`ASK`, `SEARCH`, `LOOK_UP_DETAILS`, `ESTIMATE_TRAVEL`, `VALIDATE`, `SOLVE`, `PRESENT_SHORTLIST`, `CONFIRM`, `PROPOSE_ACTION`, `EXPLAIN_CONFLICT`, `STOP_UNSUPPORTED`); at most one question per turn and up to three contrasting options.
- `models/gateway.py`: provider-neutral `generate(workspaceContext, allowedTools, outputSchema, budget, privacyClass, groundingPolicy)`; pinned model and prompt versions; records tokens, latency, retries and cost without raw content.
- `tools/`: server-owned, typed, versioned tools classified read/compute/propose/commit. Outputs carry source, retrieval time, expiry, precision and billable units. Middleware injects identity and credentials; the model never supplies them.
- `actions/`: `PendingAction` with effect, target, expiry, nonce, plan version, idempotency; commit re-checks auth, consent and version.
- `events/`: Firestore transactional outbox to Cloud Tasks carrying only action ID and expected version; stale versions are no-ops.

Rules:

- Reject unknown fields, invalid transitions, missing provenance and inference-to-hard-fact promotion.
- Changing date, travelers, destination or fixed anchors invalidates dependent evidence and plans.
- At any budget limit return the best validated partial result with unresolved choices.
- Keep Places/Routes payloads out of model context; use Maps grounding for discovery and render it unmodified.
- Write pytest coverage for every validator branch and state transition.
