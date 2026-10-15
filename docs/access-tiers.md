# Anonymous Access and Free Account Tiers

Updated 2026-10-14. The product is free during validation. A person can receive useful trip help before creating an account; registration is requested only after demonstrated use or when the person asks for an account-only capability.

## Product policy

| | Guest free | Registered free |
|---|---|---|
| First use | No sign-up screen | Google or email link |
| Allowance | 50 successful user turns or 10 activated trips, lifetime, whichever is reached first | 100 successful user turns and 10 activated trips per rolling 30 days |
| Planning | Same-day local discovery, planning, revisions, Maps handoff | Guest capabilities plus cloud history and broader saved context |
| Reminders | Device-local; on web, shown when the app is next opened | Device-local; routine reminder notifications (web push) from Phase 3 after their release gate |
| Calendar | Unavailable | Read-only connection after its separate release gate |
| Memory and sync | Current device, including a local outing log; no durable inferred routines | Cross-device workspaces, opt-in outing history (default 180 days, adjustable), user-approved memory and, from Phase 2, user-confirmed routines |
| When allowance ends | Finish the current response; require a free account before the next metered action | Show the reset date and retain read/export/delete access |
| Payment | None | None |

These numbers are an initial experiment, not a promise that they are economically sustainable. They live in versioned server policy so a release is not required to change them. An existing session receives a grace rule when a policy changes. There is no paid plan in the validation roadmap.

## What is counted

A **user turn** is one user submission for which the service returns a completed assistant response. The assistant response, clarification question, internal reasoning, retries and tool calls never add turns. Validation failures, provider failures, safety refusals, duplicate idempotency keys and server errors do not consume a user turn.

An **activated trip** is counted once when the user accepts a plan and then saves it, schedules a reminder or launches the Maps handoff. Searches, abandoned workspaces and generated plans the user does not act on are not trips. Edits within that workspace remain the same trip unless the user changes its core goal, destination or service date and accepts the resulting new plan. The interface explains before an edit will create another trip.

User-facing allowances are separate from internal model-token, Maps-grounding, Places, Routes, weather, daily, per-run and whole-pilot cost ceilings. A single user turn can fan out only within those smaller server budgets. Provider work already performed is recorded in the internal cost ledger even when a user-facing turn is refunded.

## Guest experience

The client silently creates a Firebase anonymous account on first use. This supplies a verifiable, isolated tenant ID without asking the person to log in or provide email, phone, advertising ID or a hardware identifier. Every model or provider endpoint still requires a valid Firebase ID token, App Check signal, entitlement check and server-side quota reservation. There is no public unauthenticated paid-compute endpoint.

Before the first locally saved plan, explain: “No account is needed. Guest trips stay with this browser or device and may be lost if its data is cleared. Create a free account later to recover and sync them.” Do not show an account prompt during onboarding.

- At 70% of either allowance, show a quiet meter.
- At 90%, show one benefit-led notice about recovery and sync.
- At the limit, finish the in-progress response and gate the next metered action.
- Registration cancellation or failure consumes nothing.
- Viewing, navigating from, exporting and deleting existing guest plans remain available after the gate.
- Never interrupt an active trip, local reminder or Maps handoff.

The registration message states the exact limit and the free-account benefit. It does not imply that the app is paid: “You used your 10 guest trips. Create a free account to continue and receive 10 trips every 30 days.” Google is the primary path and email link is the alternative; a phone number is not required.

## Capability and storage boundary

Guest state is local-first in Room or IndexedDB: messages, drafts, accepted plans and local reminders. The server keeps the anonymous principal, entitlement and cost ledgers, idempotency records, and the minimum structured workspace summary needed for an active request. Guest workspace summaries expire after seven inactive days. Raw chat, exact-location history, Calendar tokens, durable preferences, embeddings and proactive server jobs are excluded.

Registered-free storage may contain opted-in workspaces, outing history, confirmed profile memory and routines, push subscriptions, devices and later Calendar connections or scheduled actions, each under its own consent. Registration is not consent to upload all local history. After linking, the user chooses which local trips to copy. Exact location and provider-content restrictions in the security plan still apply. Stored history contains place IDs and user data only; saved plans re-fetch place details when viewed, and grounded content expires with its display cache, so exports contain IDs, labels, timestamps and feedback but not Maps content ([data-and-history-design.md](data-and-history-design.md)).

Firebase's optional automatic cleanup deletes anonymous accounts older than 30 days. Do not enable it in the first validation release because it would create a time-based registration gate that the product has not chosen. Local plans remain readable after a server workspace expires, but losing browser/app data or reinstalling can make an unregistered identity unrecoverable; the product states this plainly and does not fingerprint the device to recreate it.

## Identity, quota and upgrade design

```text
Principal
  uid                    # Firebase UID; never accepted from a request body
  kind                   # GUEST | REGISTERED_FREE | DISABLED
  policyVersion
  createdAt registeredAt? retentionExpiresAt?

EntitlementPolicy
  version effectiveAt graceRule
  userTurnLimit activatedTripLimit period
  capabilities internalCostCaps

UsageReservation
  principalId period operation idempotencyKey
  userUnits estimatedProviderUnits actualProviderUnits
  status reservedAt committedAt?
```

`QuotaService.check`, `reserve`, `commit` and `release` run in Firestore transactions. The backend derives capabilities from verified identity, tier, platform, permissions and remaining allowance. Clients display counters returned by the server but never grant access from local counters. Every mutation is idempotent, and every paid provider call also obeys a global kill switch and project quota.

The main endpoints are:

```text
POST /v1/session:bootstrap
GET  /v1/me/capabilities
GET  /v1/me/usage
POST /v1/agent/turns
POST /v1/trips/{id}:activate
POST /v1/account/link-intents
POST /v1/account/link-intents/{id}:complete
POST /v1/account-migrations
POST /v1/me:export
POST /v1/me:delete
```

For a new Google or email identity, Firebase `linkWithCredential` preserves the anonymous UID. The backend uses a one-time link intent, verifies that the refreshed token still has the same UID and now has a permanent provider, then changes the principal atomically.

If the credential already belongs to an account, use an explicit merge flow. Authenticate both identities, show a manifest of guest trips, copy selected trips with idempotency keys, preserve registered preferences unless the user chooses otherwise, and delete guest server data only after completion. Do not add the guest allowance to the registered allowance, silently merge by email or overwrite saved places.

## Abuse, failure and recovery

The conversion allowance is not the abuse boundary. Use per-UID sliding windows, coarse short-retention IP buckets, App Check/Play Integrity as risk signals, per-run provider ceilings, whole-pilot budgets and provider project quotas. Do not use advertising IDs, permanent hardware IDs, exact IP history or location history. Shared networks and an inconclusive integrity signal cannot alone ban a person.

- Anonymous auth unavailable: allow local drafting and cached-plan viewing; disable fresh server tools.
- Limit reached during a request: finish the reserved request and gate the next one.
- Account link completed but reconciliation failed: bootstrap detects the permanent provider and resumes.
- Existing-account collision: preserve both identities until the idempotent merge succeeds.
- Policy lowered: honor the recorded grace rule and preserve read/export/delete.
- Registered-free allowance exhausted: show the reset date; do not show a paid upsell.

## Measurement and validation

Track structured, location-free events for guest creation, first useful proposal, trip activation, each usage notice, limit reached, registration start/completion/failure, migration, registered reset, rate limiting and integrity unavailability. Do not attach addresses, coordinates, trip names or raw messages.

Initial gates are hypotheses:

- at least 60% registration completion after a person starts;
- hard-gate abandonment below 30%;
- at least 99.5% successful guest-to-account linking or resumable migration;
- duplicate allowance increments below 0.1%;
- false-limit complaints below 1% of limited users;
- provider cost per activated trip remains within the frozen pilot cap.

The pilot compares optional prompt timing and benefit wording only after enough traffic exists. It reports activation, accepted trips, time to first value, limit reach, registration conversion, repeat use, migration recovery and actual provider/model cost by tier.

Official implementation references: [Firebase anonymous authentication](https://firebase.google.com/docs/auth/web/anonymous-auth), [Firebase account linking](https://firebase.google.com/docs/auth/web/account-linking), [Firebase session management](https://firebase.google.com/docs/auth/admin/manage-sessions), [Firebase App Check](https://firebase.google.com/docs/app-check) and [Play Integrity standard requests](https://developer.android.com/google/play/integrity/standard).

