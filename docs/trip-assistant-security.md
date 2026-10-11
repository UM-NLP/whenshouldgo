# Trip Assistant Privacy and Security Plan

Updated 2026-10-10. Location, routines, calendar events, and OAuth credentials can expose home, work, health, religion, relationships, and daily absence patterns. The product collects them only for an invoked feature and keeps every persistent inference visible and editable.

## Consent and authority

- Manual planning works without calendar connection or background location.
- Calendar access is requested in context, one provider and selected calendars at a time. Start with free/busy or read-only scope; request write scope separately.
- Foreground location is the initial default. Background location is out of scope until a specific, validated feature cannot work without it.
- Notification permission is requested after the user schedules a reminder or enables monitoring.
- Microphone access is push-to-talk only in early releases. The server stores text only when needed for the active request.
- Reads and computations may run within the granted feature. Calendar writes, routine changes, sharing, bookings, messages, and purchases require an explicit preview and confirmation. Purchases are excluded initially.

The consent screen and settings show the data, purpose, retention, last use, and revocation effect. Revocation stops future reads and scheduled work, invalidates affected plans, deletes refresh credentials, and leaves manual planning available.

OAuth permission alone is not consent for model processing, routine inference, staff access, or broad history. Record a separate purpose-specific consent with notice version, processors, planning horizon, selected calendars, and withdrawal time. Google OAuth applications left in Testing mode issue refresh tokens that expire after seven days, so a persistent Calendar pilot needs an explicit weekly reconnect design or production/sensitive-scope verification.

## Memory policy

Memory is a user-owned structured profile: saved place IDs/labels, preparation duration, parking and walking allowances, route preferences, reminder lead time, confirmed routines, and outcome summaries. The assistant asks “Remember this?” and lets the user choose fields. It never silently derives a routine from location history.

Each remembered value carries provenance, confirmation time, last use, and an optional expiry. Candidates decay and are removed if not confirmed. Users can inspect, edit, export, forget one item, clear history, disconnect a calendar, or delete the account.

Do not keep detailed movement traces or a permanent raw calendar mirror. Current GPS, raw audio, and voice transcripts are memory-only and discarded after confirmation. Calendar descriptions, attendees, attachments, conference URLs, and invitation text are never collected. Trip history defaults off for the first Calendar pilot; if later enabled, start with a short rolling period such as 30 days and let the user shorten or disable it. Logs and analytics exclude addresses, coordinates, event text, tokens, full prompts, and free-text outcomes.

## Storage and credentials

Firebase Authentication identifies both guests and registered users. The client silently creates an anonymous Firebase account before its first server tool call, so the person sees no login flow while the backend still receives a verifiable tenant. Use the verified OpenID Connect `(issuer, sub)` pair as the stable identity, never email, installation ID or a client-supplied owner. Every read and write is authorized against that server-derived tenant; the model and request body cannot supply it. Firestore server libraries bypass client Security Rules, so application authorization and IAM must cover every record, cache, job, webhook channel, and device token. Use App Check as an abuse signal, server-side quotas as the control, Secret Manager for application credentials, and Cloud KMS envelope encryption for per-user OAuth refresh tokens. Separate production and development projects and credentials.

Guest storage is local-first. Server-side guest workspace summaries expire after seven inactive days. Firebase's optional cleanup deletes anonymous accounts older than 30 days, so leave it disabled unless the product deliberately adopts and discloses a time-based registration gate. Guests cannot connect Calendar, create durable inferred routines, schedule server monitoring or perform external writes. Registration does not silently upload local precise-location history. The user chooses which trips to copy. Clearing app/browser data can make guest state unrecoverable; do not recreate it through fingerprinting.

Link a new credential to the active anonymous UID with a one-time, App-Check-bound intent. If the credential already belongs to an account, authenticate both identities and use a previewed, idempotent merge. Never merge by email alone, overwrite registered preferences silently or delete the guest until migration completes. Preserve read, export and deletion access when a usage limit is reached. See [access-tiers.md](access-tiers.md).

Keep only provider fields allowed by applicable terms. Do not persist raw Google route responses or polylines pending a written policy review. Store the minimal plan decision record, forecast age, selected provider, permitted identifiers, and user-authored constraints.

Use two separate Maps data paths. For open-ended place discovery, the official Gemini Grounding with Google Maps feature may generate a Maps Grounded Result under its additional terms. Display that result with its associated Maps links without modification or interspersed content; do not scrape, export or train on it; disclose its 30-day provider processing and configured chat-history retention. For ordinary Places and Routes APIs, current Maps terms restrict text-to-speech, creating content from Maps content and specified AI uses. Pending a written determination, keep those API durations, ETAs, geometry and derived recommendations out of LLM context, TTS, durable history and AI evaluation data. See [Maps grounding terms](https://ai.google.dev/gemini-api/terms), [Maps terms](https://cloud.google.com/maps-platform/terms) and [Routes policies](https://developers.google.com/maps/documentation/routes/policies).

## Agent and tool safety

Calendar names, event locations, place text, reviews, shared URLs and provider content are untrusted. Official grounding runs only through the provider's bounded tool. Custom tool results are reduced to typed fields and citations before model use. Tool responses cannot change system policy or tool permissions. The orchestrator selects a narrow tool set, validates JSON Schema, removes unexpected fields, caps lengths and counts, and rejects arbitrary URLs.

Side effects use a two-step pending-action protocol. The proposal records exact effect, account, target, expiry, and one-time nonce. The commit endpoint checks authentication, authorization, current consent, matching plan version, nonce, idempotency, and confirmation. Every commit is auditable and replay-safe.

## Proactive notifications

Notifications reveal little on the lock screen by default: “Your departure plan changed” rather than addresses, times, event titles, or absence windows. FCM payloads and deep links carry opaque IDs rather than sensitive content. Android uses private or secret visibility. A monitoring policy defines the trip, time window, refresh frequency, material-change threshold, quiet hours, and expiry. Every scheduled task references a current plan version; cancellation or edit makes older tasks no-ops.

## Abuse and failure controls

Use per-user and global limits for model tokens, route elements, place searches, weather calls, calendar sync, and notifications. The friendly guest allowance is a conversion rule, not an abuse control. Also enforce per-UID sliding windows, coarse short-retention IP buckets, per-run provider ceilings, App Check or Play Integrity risk signals, daily project quotas and global cost kill switches. Do not use advertising IDs, persistent hardware identifiers, exact IP history or location history to prevent resets. Add idempotency to expensive and mutating operations, bounded retries, circuit breakers and budget alerts. Detect token theft and repeated place probing. Rate-limit identity creation and sensitive exports without treating a shared network or inconclusive integrity signal as proof of abuse.

An incident runbook must cover OAuth token exposure, provider-key exposure, calendar overcollection, notification leakage, model prompt injection, unauthorized tool commits, quota runaway, and deletion failure. Backups must not defeat deletion promises; record and test the deletion window.

## Release gates

Before calendar beta: verify least-privilege scopes, PKCE/state, encrypted refresh tokens, selected-calendar enforcement, incremental-sync recovery, revocation, export, deletion, and redacted logs.

Before persistent accounts: test cross-tenant reads/writes across direct object access, scheduled jobs, webhook channels, caches, device tokens, and model-memory retrieval. Account deletion must stop Calendar channels and scheduled work, revoke tokens, delete histories/routines/memories/device tokens/exports, prevent late jobs from recreating the user, and have both in-app and web entry points before Play distribution.

Before anonymous beta: test quota races, duplicate idempotency keys, guest expiry, lost local identity, read/export/delete after the gate, no paid endpoint without verified identity, same-UID account linking, existing-account merge, interrupted/resumed migration, no implicit local-history upload and zero cross-tenant access across guest and registered principals.

Before proactive beta: verify task authentication, cancellation, idempotency, plan-version suppression, quiet hours, lock-screen privacy, duplicate suppression, and notification opt-out.

Before model-agent beta: achieve zero unauthorized commits in adversarial tests, preserve all hard constraints in critical fixtures, block instructions embedded in tool data, and demonstrate deterministic-form fallback.

Before grounded discovery beta: verify user-initiated enablement, attribution and Maps-link rendering, non-modification of grounded results, provider-retention disclosure, chat-history deletion, no training/evaluation reuse of Maps data, and separation from ordinary Places/Routes payloads.

Before background location or assistant-role work: complete a separate privacy, battery, store-policy, threat-model, and user-value review.


