# Access and quota agent

Use this role for guest bootstrap, identity, entitlements, quota, account linking,
and export/delete (#22).

Read `AGENTS.md`, `docs/access-tiers.md`, and the identity sections of
`docs/trip-assistant-architecture.md` and `docs/trip-assistant-security.md`.

- Derive `Principal(uid, kind, policyVersion)` only from a verified token. Never
  accept `uid`, `kind` or an owner ID from a request body or model output.
- `QuotaService` keeps `check`, `reserve`, `commit` and `release` atomic and keyed by
  idempotency key. Allowances (guest 50 turns or 10 trips lifetime; registered-free
  100 turns and 10 trips per rolling 30 days) come from versioned policy, not
  constants in handlers.
- A turn commits only when a completed response is returned. Failures, refusals,
  server errors and duplicate keys release the reservation. A trip counts once on
  save, reminder or Maps handoff.
- At the limit, finish the in-flight response and gate the next metered action.
  Viewing, export, delete and Maps handoff stay available.
- Linking preserves the UID through a one-time intent. A credential that already
  owns an account uses a previewed, idempotent merge; never merge by email or add
  allowances together.
- No advertising IDs, hardware IDs, IP history or fingerprinting.
- Endpoints (`docs/access-tiers.md`): `POST /v1/session:bootstrap`,
  `GET /v1/me/capabilities`, `GET /v1/me/usage`, `POST /v1/trips/{id}:activate`,
  `POST /v1/account/link-intents`, `POST /v1/account/link-intents/{id}:complete`,
  `POST /v1/account-migrations`, `POST /v1/me:export`, `POST /v1/me:delete`.

Current foundation: `backend/app/auth.py` (verifier protocol, server-side tier),
`backend/app/entitlements.py` (versioned policy) and `backend/app/quota.py`
(in-memory reserve/commit/release). Trip activation, the rolling 30-day window and
Firestore persistence are not implemented.

Test quota races, duplicate keys, policy-change grace, interrupted link/merge
resume, and cross-tenant isolation between guest and registered principals.
