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

Test quota races, duplicate keys, policy-change grace, interrupted link/merge
resume, and cross-tenant isolation between guest and registered principals.
