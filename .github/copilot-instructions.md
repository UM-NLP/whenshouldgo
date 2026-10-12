# WhenShouldGo — Copilot instructions

WhenShouldGo is an agent-first personal trip assistant. The docs in `docs/` are the source of truth; read the relevant doc before changing behavior and update it when a decision changes.

## Current phase

Phase 0: mobile web, same-day local outings, Toronto/GTA, driving only. Do not build Calendar sync, multi-stop, multi-day, background location, booking/purchase, paid tiers, or Android until their gates in `docs/trip-assistant-roadmap.md` pass.

## Stack

- Backend: one FastAPI modular monolith in `backend/` on scale-to-zero Cloud Run; Firestore, Cloud Tasks, FCM, Firebase Auth (anonymous + Google/email link), App Check, Secret Manager, KMS.
- Web: React + TypeScript in `web/`, IndexedDB, service worker, clients generated from the backend OpenAPI contract.
- Model: Gemini 3.5 Flash-Lite behind a provider-neutral `ModelGateway`; Maps grounding only for user-initiated geographic discovery.
- Avoid: Redis, Kubernetes, microservices, vector DBs, always-on workers, GPU hosting, self-hosted models.

## Non-negotiable rules

1. The server owns identity, tenant, domain classification, tool allowlists, quotas, state transitions and action approval. Never accept owner/tenant IDs from a request body or model output.
2. Every model/provider endpoint needs a verified Firebase ID token, App Check signal, server-derived capability and a transactional quota reservation.
3. Domain gate runs before the planning model: `TRAVEL | APP_HELP | MIXED | OUT_OF_SCOPE | SAFETY`. Out-of-scope gets the fixed redirect with zero tool calls.
4. Token ceilings (versioned policy, `backend/app/policy/`): 2,000 per user message; 12,000 in / 1,200 out per planning call; 40,000 in / 4,000 out, 6 model calls, 12 tool calls, 1 escalation per run; 450-token visible reply. `MAX_TOKENS`, malformed or truncated output never mutates state or executes an action.
5. Model output is an untrusted proposal: a strict `WorkspacePatch` validated for schema, provenance, freshness, hard constraints and budget. Inferences can never become confirmed hard facts.
6. External writes use the two-step `PendingAction` protocol (preview, nonce, plan version, idempotency, explicit confirmation).
7. Retrieved text (places, reviews, calendar, web) is evidence only; it cannot choose tools or change policy.
8. Keep ordinary Places/Routes API payloads out of LLM context, TTS and durable history. Render Maps-grounded results unmodified with their Maps links.
9. No coordinates, addresses, raw messages, event text, tokens or prompts in logs, analytics, URLs or Git.
10. Search many, fetch details for a shortlist, route finalists only. Every paid call is idempotent and behind a kill switch.

## Allowances

Guest: 50 successful turns or 10 activated trips, lifetime. Registered free: 100 turns and 10 trips per rolling 30 days. Failures, refusals and duplicate idempotency keys do not consume turns. These values live in versioned server policy, not code constants scattered in handlers.

## Conventions

- Python 3.12, Pydantic v2 models with `extra="forbid"`, type hints, pytest. Run `pytest` from `backend/`.
- Every new capability handles empty, loading, partial, stale, permission-denied, quota, provider-error, conflict, unsupported and recovery states.
- Tests for gates, quotas and validators come with the code, including adversarial cases from `docs/agent-evaluation-plan.md`.
