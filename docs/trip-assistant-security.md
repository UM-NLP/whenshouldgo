# Trip Assistant Privacy and Security Plan

Updated 2026-10-14. Location, routines, calendar events, trip history, and OAuth credentials can expose home, work, health, religion, relationships, and daily absence patterns. The product collects them only for an invoked feature and keeps every persistent inference visible and editable. Stored data classes and retention are in [data-and-history-design.md](data-and-history-design.md).

## Consent and authority

- Manual planning works without calendar connection or background location.
- Calendar access is requested in context, one provider and selected calendars at a time. Start with free/busy or read-only scope; request write scope separately.
- Foreground location is the initial default. Background location is out of scope until a specific, validated feature cannot work without it.
- Notification permission is requested after the user confirms a routine or schedules a reminder and separately opts in to notifications. History storage, pattern suggestions and notifications each have their own consent.
- Microphone access is push-to-talk only in early releases. The server stores text only when needed for the active request.
- Push-to-talk starts only while the user holds the visible control, stops on release or at 60 seconds, and discards audio after transcription or cancellation. There is no background microphone service or wake-word buffer.
- Reads and computations may run within the granted feature. Calendar writes, routine changes, sharing, bookings, messages, and purchases require an explicit preview and confirmation. Purchases are excluded initially.

The consent screen and settings show the data, purpose, retention, last use, and revocation effect. Revocation stops future reads and scheduled work, invalidates affected plans, deletes refresh credentials, and leaves manual planning available.

OAuth permission alone is not consent for model processing, routine inference, staff access, or broad history. Record a separate purpose-specific consent with notice version, processors, planning horizon, selected calendars, and withdrawal time. Google OAuth applications left in Testing mode issue refresh tokens that expire after seven days, so a persistent Calendar pilot needs an explicit weekly reconnect design or production/sensitive-scope verification.

## Memory policy

Memory is a user-owned structured profile: saved place IDs/labels, preparation duration, parking and walking allowances, route preferences, reminder lead time, confirmed routines, and outcome summaries. The assistant asks “Remember this?” and lets the user choose fields. It never silently derives a routine from location history. From Phase 2 it may propose a pattern computed from the user's own outing history (place IDs, labels, times and feedback, not location traces); the proposal shows its basis, and nothing inferred is stored or treated as a hard fact without the user's confirmation. Before Phase 2 nothing inferred is persisted. Patterns that could reveal health, religion, sexual orientation or children's activities are not surfaced unless the user chose to label them, and the user can hide any entry.

Each remembered value carries provenance, confirmation time, last use, and an optional expiry. Candidates decay and are removed if not confirmed. Users can inspect, edit, export, forget one item, clear history, disconnect a calendar, or delete the account.

Do not keep detailed movement traces or a permanent raw calendar mirror. Current GPS, raw audio, and voice transcripts are memory-only and discarded after confirmation. Calendar descriptions, attendees, attachments, conference URLs, and invitation text are never collected. Trip history is opt-in with a default rolling retention of 180 days that the user can shorten, extend or disable, and the consent notice states the period (decision 19). It stores place IDs and user data only. Logs and analytics exclude addresses, coordinates, event text, tokens, full prompts, and free-text outcomes.

## Storage and credentials

Firebase Authentication identifies both guests and registered users. The client silently creates an anonymous Firebase account before its first server tool call, so the person sees no login flow while the backend still receives a verifiable tenant. Use the verified OpenID Connect `(issuer, sub)` pair as the stable identity, never email, installation ID or a client-supplied owner. Every read and write is authorized against that server-derived tenant; the model and request body cannot supply it. Firestore server libraries bypass client Security Rules, so application authorization and IAM must cover every record, cache, job, webhook channel, and device token. Use App Check as an abuse signal, server-side quotas as the control, Secret Manager for application credentials, and Cloud KMS envelope encryption for per-user OAuth refresh tokens. Separate production and development projects and credentials.

Guest storage is local-first. Server-side guest workspace summaries expire after seven inactive days. Firebase's optional cleanup deletes anonymous accounts older than 30 days, so leave it disabled unless the product deliberately adopts and discloses a time-based registration gate. Guests cannot connect Calendar, create durable inferred routines, schedule server monitoring or perform external writes. Registration does not silently upload local precise-location history. The user chooses which trips to copy. Clearing app/browser data can make guest state unrecoverable; do not recreate it through fingerprinting.

Link a new credential to the active anonymous UID with a one-time, App-Check-bound intent. If the credential already belongs to an account, authenticate both identities and use a previewed, idempotent merge. Never merge by email alone, overwrite registered preferences silently or delete the guest until migration completes. Preserve read, export and deletion access when a usage limit is reached. See [access-tiers.md](access-tiers.md).

Keep only provider fields allowed by applicable terms. Do not persist raw Google route responses or polylines pending a written policy review. Store the minimal plan decision record, forecast age, selected provider, permitted identifiers, and user-authored constraints.

Use two separate Maps data paths. For open-ended place discovery, Google Maps Grounding Lite with the OpenRouter model may generate grounded output under the Maps service-specific terms. Display that output with its Google Maps sources immediately after it and without modification or interspersed content; do not extract, export or train, test, validate or improve any model with it; cache it only for redisplay for at most 30 consecutive days; and require that the model provider does not store or cache it (zero data retention, no training). For ordinary Places and Routes APIs, current Maps terms restrict text-to-speech, creating content from Maps content and specified AI uses, and they allow storing place IDs but not other content. Pending a written determination, keep those API durations, ETAs, geometry and derived recommendations out of LLM context, TTS, durable history and AI evaluation data. Storing only place IDs and user data keeps history compliant. User consent does not replace Google's licence terms. The owner's legal summary is in [legal/google-maps-llm-policy-summary.md](legal/google-maps-llm-policy-summary.md) and open questions are in [legal/open-questions.md](legal/open-questions.md). See [Grounding Lite](https://developers.google.com/maps/ai/grounding-lite), [Places policies](https://developers.google.com/maps/documentation/places/web-service/policies), [Maps terms](https://cloud.google.com/maps-platform/terms) and [Routes policies](https://developers.google.com/maps/documentation/routes/policies).

Telemetry is an allowlist by construction: typed fields only (identifiers, enums, counts, token counts, latency, cost, finish reason, evidence IDs, runtime config version). Free-form strings and Maps content cannot be logged, and a canary test asserts this before the first model call.

## Agent and tool safety

Calendar names, event locations, place text, reviews, shared URLs and provider content are untrusted. Official grounding runs only through the provider's bounded tool. Custom tool results are reduced to typed fields and citations before model use. Tool responses cannot change system policy or tool permissions. The orchestrator selects a narrow tool set, validates JSON Schema, removes unexpected fields, caps lengths and counts, and rejects arbitrary URLs.

Side effects use a two-step pending-action protocol. The proposal records exact effect, account, target, expiry, and one-time nonce. The commit endpoint checks authentication, authorization, current consent, matching plan version, nonce, idempotency, and confirmation. Every commit is auditable and replay-safe.

An authenticated request passes an input-size and travel-domain gate before the planning model. Out-of-scope requests receive a fixed response and no travel tools. The server selects the tool allowlist; neither user input nor retrieved content can expand it. Enforce per-message, per-call, per-run and per-principal token ceilings. Reserve worst-case tokens before generation, commit provider-reported actual usage, and reject `MAX_TOKENS`, malformed or truncated actions. Repeated out-of-scope attempts enter an abuse cooldown without repeated model calls.

## Proactive notifications

Notifications reveal little on the lock screen by default: “Your departure plan changed” or “Time for your usual Saturday outing?” rather than place names, addresses, times, event titles, or absence windows. Web push and FCM payloads and deep links carry opaque IDs rather than sensitive content. Each notification names the sender and offers a one-tap stop, and an unsubscribe is honoured immediately. Push subscription endpoints are secret, stored per user and deleted on unsubscribe or account deletion. Counsel's answer on whether a push notification is a commercial electronic message under CASL is required before Phase 3 ([legal/open-questions.md](legal/open-questions.md)). Android uses private or secret visibility. A monitoring policy defines the trip, time window, refresh frequency, material-change threshold, quiet hours, and expiry. Every scheduled task references a current plan version; cancellation or edit makes older tasks no-ops.

## Abuse and failure controls

Use per-user and global limits for model tokens, route elements, place searches, weather calls, calendar sync, and notifications. The friendly guest allowance is a conversion rule, not an abuse control. Also enforce per-UID sliding windows, coarse short-retention IP buckets, per-run provider ceilings, App Check or Play Integrity risk signals, daily project quotas and global cost kill switches. Do not use advertising IDs, persistent hardware identifiers, exact IP history or location history to prevent resets. Add idempotency to expensive and mutating operations, bounded retries, circuit breakers and budget alerts. Detect token theft and repeated place probing. Rate-limit identity creation and sensitive exports without treating a shared network or inconclusive integrity signal as proof of abuse.

An incident runbook must cover OAuth token exposure, provider-key exposure, calendar overcollection, notification leakage, model prompt injection, unauthorized tool commits, quota runaway, deletion failure, and a wrong or unsafe recommendation (for example a closed venue or an allergen or accessibility miss), with a switch that disables discovery and serves fixed text. Backups must not defeat deletion promises; record and test the deletion window.

## Release gates

Before calendar beta: verify least-privilege scopes, PKCE/state, encrypted refresh tokens, selected-calendar enforcement, incremental-sync recovery, revocation, export, deletion, and redacted logs.

Before persistent accounts: test cross-tenant reads/writes across direct object access, scheduled jobs, webhook channels, caches, device tokens, and model-memory retrieval. Account deletion must stop Calendar channels and scheduled work, revoke tokens, delete histories/routines/memories/device tokens/exports, prevent late jobs from recreating the user, and have both in-app and web entry points before Play distribution.

Before anonymous beta: test quota races, duplicate idempotency keys, guest expiry, lost local identity, read/export/delete after the gate, no paid endpoint without verified identity, same-UID account linking, existing-account merge, interrupted/resumed migration, no implicit local-history upload and zero cross-tenant access across guest and registered principals.

Before history (Phase 1): verify consent ledger, per-item and bulk deletion, export, retention default shown in the notice, place-ID re-fetch rendering, and zero Maps content at rest outside the display cache.

Before pattern suggestions (Phase 2): verify separate pattern consent, the sensitive-inference rule, that no routine exists without a confirmed `PendingAction`, and that declined patterns are never re-proposed.

Before proactive beta (Phase 3 and later): verify task authentication, cancellation, idempotency, plan-version or routine-version suppression, quiet hours, lock-screen privacy, duplicate suppression, notification opt-out, and that the trigger path makes no model call.

Before model-agent beta: achieve zero unauthorized commits in adversarial tests, preserve all hard constraints in critical fixtures, block instructions embedded in tool data, and demonstrate deterministic-form fallback.

Before grounded discovery beta: verify user-initiated enablement, attribution and Maps-link rendering with sources immediately after the output, non-modification of grounded results, display-cache expiry and purge, the OpenRouter zero-retention and pinned-provider parameters on every request (a request without them is rejected), disclosure of Google, OpenRouter and the upstream host as processors, no training/testing/validation reuse of Maps data, synthetic-only evaluation fixtures, and separation from ordinary Places/Routes payloads.

Before background location or assistant-role work: complete a separate privacy, battery, store-policy, threat-model, and user-value review.

