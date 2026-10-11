# Web quality, security, and operations plan

Status: proposed controls and release gates for an invite-only pre-seed pilot. No checks described as runtime monitoring have been deployed, and no real-web validation has been executed.

## Quality strategy

The planner is tested independently from the provider and UI. Live provider tests prove integration behavior; they do not replace deterministic correctness tests or establish real driving accuracy.

| Layer | Automated checks | Manual checks | Pilot gate |
|---|---|---|---|
| Planner arithmetic | Unit and property tests for all derived times, feasibility and tie-breaks | Review representative timelines | 100% pass; zero false-feasible result |
| Search quality | At least 1,000 versioned authored/synthetic nonmonotonic surfaces compared with an independent exhaustive one-minute oracle that shares no feasibility/scoring helpers | Inspect retained worst-regret fixtures and the reserved leave-when-ready baseline | Median regret <=2 road min; p95 <=5; p99 <=10; <=24 production estimates |
| Time handling | UTC instant calculations plus IANA display-zone tests, DST gaps/overlaps and midnight boundaries | Verify Toronto DST examples in browser | No silent time correction or ambiguous repeated time |
| Provider contract | Fake server and schema fixtures for valid, fallback, partial, timeout, 429, 4xx, 5xx and malformed replies | One bounded live smoke test in pilot project | Missing/invalid leg never produces a plan; key absent from output |
| API | Schema bounds, session, CSRF, transactional quota, two-process idempotency race/restart, retry budget, cancellation and safe-error tests | Replay pending/completed/mismatched requests from browser dev tools | Stable codes; duplicates make zero provider calls; uncertain crash reservation remains spent |
| Web domain/UI | Component tests for every form and state; IndexedDB migration/unavailable/corrupt/quota failure | 360px, 200% text and loaded-tab offline review | Every field editable; reload restores inputs and requires recalculation; memory fallback is visible |
| End to end | Full fake-provider journey in frozen Playwright Chromium, Firefox and WebKit versions, including impossible plan, outage, denied location, loaded-tab offline behavior and user-triggered Maps URL construction | Full journey and Maps handoff on physical Android Chrome and iPhone Safari | Automated engine matrix passes; physical handoff evidence recorded; offline-shell reload waits for PWA gate |
| Accessibility | Semantic queries, automated scan, focus order/restoration and live-region tests | Keyboard-only, mobile/desktop screen readers, contrast, 200% text and 320 CSS-pixel reflow | No critical/serious automated finding; all actions reachable/named; WCAG 2.2 AA reflow passes |
| Performance | Bundle budget, API latency test with fake provider, bounded concurrency | Cold-start measurement on pilot Cloud Run | Web interaction remains responsive; full-plan p95 <=12 sec in pilot |
| Privacy/security | Secret/build-context scan, dependency audit, CSP/session/header tests, log-redaction and local-data isolation tests | Inspect browser bundle/network/logs/IndexedDB/cache/image layers | No credential/location leak; no provider-derived result persisted; another session sees no prior input |
| Cost | Assert provider-call caps and retry accounting | Reconcile estimated usage with billing after 5, 10 and 20 plans | Median <=20, p95 <=24 route estimates; spend inside pilot cap |

Planner fixtures include constant, rising, falling, sharply nonmonotonic and multi-peak traffic; route changes; tight/loose and exact-boundary deadlines; 15–180 minute visits; one-to-eight-hour windows; exact score ties; partial/missing return legs; all access allowances; toll policies; visit windows; maximum-wait constraints; DST folds/gaps; and no-solution cases. Runtime `SEARCH_INCOMPLETE` is limited to a failed required sample or the fixed schedule failing to finish within its hard call/time cap. Every successful result remains **best evaluated**, not globally optimal.

## Acceptance traceability

Every P0 product criterion in [web-product-plan.md](web-product-plan.md) receives an ID in `contracts/acceptance.yaml`. Tests reference those IDs. A release report lists:

- passing automated suites and browser versions;
- manual checks performed and device/browser used;
- live calls made and their cost estimate;
- known failures and deferred checks;
- commit/revision deployed;
- rollback revision.

Real-trip accuracy, willingness to reuse, and provider invoices remain unexecuted until the assisted pilot. They must never be reported as verified by unit or integration tests.

## Prioritized threat model

| Priority | Threat | Control | Security acceptance test |
|---|---|---|---|
| P0 | Routing credential copied from frontend or repository | Dedicated least-privilege Cloud Run identity with ADC/OAuth, no service-account key, separate Secret Manager values for invite/token signing, explicit build-context scanning | Search upload manifest, image layers, built assets/history and browser requests; no credential appears |
| P0 | Public endpoint used to generate paid calls | Invite-only session, transactional per-session/global reservations, 24-call plan cap, Maps quota, service-wide semaphore and kill switches; max one instance only reduces exposure | Missing/expired session gets zero provider calls; two-process races cannot exceed reserved quotas |
| P0 | Precise locations leak through logs/analytics/errors | Allowlisted telemetry schema, request-body logging disabled, redaction tests, no free text in error tracking | Seed canary address never appears in captured logs or events |
| P0 | Planner returns unsafe false-feasible result | Typed models, deterministic feasibility invariant, missing-leg fail-closed behavior | Property tests and injected provider failures produce no false feasible result |
| P0 | XSS steals session or trip data | No raw HTML rendering, strict CSP, dependency review, output encoding, `HttpOnly` session | Stored/reflected payload tests cannot execute; CSP blocks inline script |
| P0 | CSRF spends quota or modifies outcomes | Strict same-site cookie, CSRF token, Origin validation, JSON-only mutation endpoints | Cross-origin requests fail before provider call |
| P0 | Provider-derived data retained contrary to terms | Persist only user-entered constraints/permitted identifiers; keep route results in memory; publish Terms/Privacy and required attribution | IndexedDB/cache inspection finds no derived route result; attribution and policy pages pass review |
| P0 | Shared browser exposes a prior tester's trip | Session-owner binding, no render before bootstrap, purge on logout/session change/deletion/expiry, `Cache-Control: no-store` | Tester B cannot enumerate or render tester A's local inputs or browser-cached result |
| P1 | Session guessing/fixation/replay | High-entropy opaque IDs, rotate on invite exchange, hash at rest, short expiry, revoke/logout | Old and attacker-supplied session fail after rotation/revocation |
| P1 | SSRF/open redirect through pasted Maps URL | Never server-fetch arbitrary input; allowlist schemes/hosts and parse locally; otherwise treat as search text | Internal/file/custom schemes are rejected without network access |
| P1 | Oversized/hostile fields exhaust service | Strict lengths, duration/range bounds, payload limit, timeout and bounded concurrency | Boundary/fuzz inputs fail quickly with stable 4xx |
| P1 | Dependency or CI compromise | Lockfiles, pinned actions/images, least-privilege deploy identity, dependency and container scan | CI cannot read production provider access on pull requests |
| P1 | Provider terms violated by caching/training | In-request coalescing only; no persistent provider duration, recommendation, body, or geometry; policy review before new storage | Storage inspection contains no provider-derived route result beyond expressly permitted identifiers |
| P2 | Notification or shared-result privacy | No push in pilot; copied/share text omits precise origin by default | Shared text snapshot contains only user-selected fields |

The exact trip payload is operational data, not analytics. It is processed in memory and returned to the browser. Firestore contains pseudonymous access, atomic quota/idempotency state, consent, and structured outcome records only. The outcome schema rejects free text, absolute timestamps, locations, traces, uploads, and unknown fields; interview notes require separate consent and manual redaction.

## Web security baseline

- HTTPS only; HSTS after confirming the deployment domain behavior.
- `Content-Security-Policy` with self-hosted scripts/styles, no `unsafe-eval`, and explicit connect/form/frame sources.
- `X-Content-Type-Options: nosniff`, restrictive `Permissions-Policy`, `Referrer-Policy: no-referrer`, and clickjacking protection through CSP `frame-ancestors`.
- `__Host-` session cookie with `Secure`, `HttpOnly`, `SameSite=Strict`, and no `Domain` attribute.
- CSRF token and strict Origin check for state-changing routes.
- Exact canonical host validation, no CORS, and a custom CSRF header for mutations.
- Strict JSON-only Pydantic schemas, a small body limit, length/range limits, unknown-field rejection, no multipart/uploads, and opaque client-safe errors.
- Separate service identity with only permissions required for Routes/Firestore/monitoring.
- No service-account key file; Cloud Run uses workload identity/Application Default Credentials.
- No precise trip in this application's URLs/history, log attributes, analytics, crash reports, or support screenshots. User-triggered Maps handoff is the disclosed exception that sends selected endpoints to Google.
- User-visible **Delete this plan** and **Delete pilot data** actions with verified deletion of session/outcome records.
- Fixed-host Maps URL construction uses `URLSearchParams`, `noopener`, `noreferrer`, and `Referrer-Policy: no-referrer`; pasted links are never server-fetched.
- API responses use `Cache-Control: no-store`; a future service worker bypasses `/api/`.

Google recommends a server proxy for web-service APIs and restricting/monitoring credentials. [Maps API security](https://developers.google.com/maps/api-security-best-practices).

## Cheap release process

Use one cloud environment for the private pilot and a complete local fake-provider environment. This avoids paying for idle staging infrastructure.

1. Pull request runs Python unit/contract tests, frontend unit tests, the complete Playwright engine matrix, lint/type checks, secret scan, dependency audit, and seeded build-context canary.
2. From a clean commit with locked dependencies and a pinned base image, inspect `gcloud meta list-files-for-upload`, build one image, scan every layer, push once, and record its digest.
3. Run that image locally against the fake provider and a single bounded live smoke test only when provider integration changed.
4. Deploy `IMAGE@sha256:...` to a tagged no-traffic Cloud Run candidate revision.
5. Run authenticated smoke tests against the tag: session exchange, Firestore, one bounded plan, one impossible plan, and quota rejection.
6. Promote the revision, record the previous rollback revision, and inspect the first five computations. Any app-caused failure triggers rollback; an external provider failure triggers its kill switch.
7. Retain five newest images plus tagged stable/rollback images and remove older untagged images after 30 days.

Production-style Terraform, multiple cloud environments, continuous canaries, paid security scanning, and 24/7 on-call tooling are deferred until use justifies them. A dedicated pilot project is reproducible through versioned `infra/bootstrap.ps1`, `infra/verify-infra.ps1`, `infra/cloud-run-service.yaml`, and `infra/deploy.ps1`; verification covers IAM, APIs, Firestore location/indexes/TTL, Artifact Registry, quotas, secrets, alerts, and log settings. `.gcloudignore` and `.dockerignore` explicitly exclude `.env*`, `.local/`, credentials, raw experiment artifacts, and precise-location fixtures.

## Proposed service objectives

These are pilot targets, not measured performance:

- at least 18 of the first 20 eligible supported requests receive `FEASIBLE` or a correct `NO_FEASIBLE_PLAN`; invalid/unsupported input is excluded, while provider/search/timeout/app failure remains in the denominator;
- zero app-caused task failures in those first 20 attempts; external provider/search failures are reported as counts and rates rather than hidden by the sample size;
- p50 end-to-end plan latency <=8 seconds and p95 <=12 seconds;
- zero provider calls after the hard per-plan, per-session, or global quota;
- zero confirmed credential or precise-location leaks;
- RTO four supported hours after acknowledgement for a bad release or provider integration failure; a region-wide outage may pause the pilot without a guaranteed RTO;
- RPO zero for source, configuration, and recorded image digests. Session/quota/outcome records are disposable during the cheap pilot and may be lost completely; active route results are intentionally not persisted.

At this scale, a percentage can hide the sample size. Every scorecard shows both counts and rates.

## Metrics and alerts

Allowlisted plan metrics:

- result category;
- candidate and provider-call count;
- retry count and provider status category;
- API and provider latency;
- search-cap and incomplete-search flags;
- duration/window buckets, never exact times;
- recommendation accepted, Maps handoff, optional outcome, and reuse intent;
- estimated billable units and pricing-table version.

Scorecard definitions:

- `task_success`: `FEASIBLE` or correctly computed `NO_FEASIBLE_PLAN`;
- `usable_recommendation`: `FEASIBLE`;
- `task_failure`: provider unavailable, incomplete search, timeout, or internal error;
- estimated/actual cost per success: all provider cost, including failures, retries, location resolution, refresh, leaving-now, and smoke calls, divided by `task_success` count;
- actual cost comes only from invoice reconciliation and remains separate from the real-time estimated ledger. Record SKU, USD price-table date, billing-account currency, and numerator/denominator counts.

Do not record addresses, coordinates, place names, route descriptions, geometry, full request/response bodies, exact trip timestamps, invite codes, session cookies, or feedback text in telemetry.

Proposed alerts:

- provider call volume reaches 50%, 80%, or 100% of the daily cap;
- estimated spend reaches CA$1, CA$5, or CA$10 in the pilot month;
- three consecutive provider failures;
- API 5xx rate above 10% over five minutes when at least five requests occurred;
- any plan exceeds 24 route estimates;
- Cloud Run revision fails readiness or repeated cold starts exceed the 20-second request deadline.

The redaction canary is a CI and manual log-capture release test unless a concrete runtime detector is later implemented. Passive Cloud Run request/error metrics and one owner email channel replace periodic uptime probes so the service can scale to zero.

Billing alerts alone do not stop spending. The Maps quota, session quotas, provider kill switch, maximum instance count, and an eligible spend cap are enforcement controls. [Google Cloud budget guidance](https://docs.cloud.google.com/billing/docs/how-to/budgets).

The pilot also enforces transactional reservations of 24 Routes units per full computation, 1,200 Routes units across the pilot with 240 reserved for smoke/recovery, a 650-unit Maps daily quota, and 100 paid location-resolution calls. Invalid, duplicate, killed, or capped requests make zero provider calls. A crash leaves its uncertain reservation spent until manual reconciliation. Separate Routes and resolver kill switches are fail-closed, auditable, and checked before every provider call.

## Incident levels

- **SEV1:** credential/location leak, authorization bypass, false-feasible plan, quota breach, or runaway spend. Stop the pilot and enable the relevant kill switch immediately.
- **SEV2:** bad deployment, repeated application 5xx, or provider outage. Roll back or disable the provider within four supported hours after acknowledgement.
- **SEV3:** isolated browser or UX defect. Record it and fix it in the next planned release.

## Runbooks

### Provider outage or malformed responses

1. Enable the provider kill switch.
2. Preserve the input draft and any still-loaded in-memory result as stale; show maintenance copy.
3. Inspect redacted status/latency only; do not enable body logging.
4. Run one bounded health request after provider recovery.
5. Re-enable for one tester, then the full invite cohort.

### Runaway calls or cost

1. Enable the kill switch and lower/disable the Maps quota.
2. Revoke suspicious sessions.
3. Inspect counts by pseudonymous session, endpoint and revision.
4. Fix retry/idempotency/rate-limit behavior with the fake provider.
5. Reconcile the billing report before reopening.

### Bad deployment

1. Route traffic to the previous known-good Cloud Run revision.
2. Confirm compute and impossible-plan smoke tests.
3. Preserve the failed revision logs and digest.
4. Open a defect with trigger, impact and missing test.

### Credential exposure

1. Disable/rotate the key or revoke the affected identity.
2. Enable the provider kill switch and inspect usage.
3. Remove the secret from source/history or build artifacts.
4. Redeploy, validate restrictions, and document possible cost exposure.

### Location/privacy incident

1. Stop the affected logging/analytics path.
2. Preserve access evidence without copying sensitive payloads.
3. Delete exposed data where possible and revoke access.
4. Identify affected users and legal obligations before communication.
5. Add a regression canary and update retention controls.

## Recovery and data lifecycle

The source repository, versioned bootstrap/verification scripts, and recorded image digest are the recovery source. A Cloud Run loss is recovered by redeploying the last known-good digest. Firestore holds no unique trip plan; invite/session/quota/outcome data is disposable and users can be re-invited. The pilot accepts possible total loss of this pseudonymous research data instead of paying for backups. If outcomes become business-critical, enable measured backups and complete a restore drill before changing the RPO claim.

Proposed retention:

- session: 14 days after last pilot use;
- idempotency lease/state: 24 hours, with only HMAC request digest, state, lease, counts, and expiry;
- quota counters: 35 days;
- Cloud Logging `_Default` operational logs: 30 days unless deployment verification records a shorter configured retention; `_Required` audit-log retention follows Google Cloud policy and is disclosed;
- structured voluntary outcomes: 90 days or immediate authenticated deletion;
- precise trip requests/provider responses: never persisted by the application;
- browser user-entered inputs: session-scoped and no later than 24 hours after `homeBy`; provider-derived plan results: never persisted.

Every Firestore collection has a versioned schema and `expireAt` TTL field. Application authorization rejects logical expiry immediately because Firestore TTL deletion is asynchronous. User deletion is synchronous at the application layer. Retention and deletion behavior must be verified before the first external tester.

## Pilot release checklist

- [ ] All acceptance IDs pass in CI.
- [ ] Worst oracle-regret fixtures reviewed and production search stays within 24 estimates.
- [ ] Oracle is implementation-independent, baseline capacity is reserved, exact-second ranking matches the contract, and every `SEARCH_INCOMPLETE` reason is deterministic.
- [ ] No false-feasible or missing-leg recommendation in tests.
- [ ] Provider credential absent from code, image history, web bundle and browser requests.
- [ ] Session, token purpose/expiry, CSRF, two-process transactional quota/idempotency, cancellation and kill-switch tests pass.
- [ ] Location redaction canary absent from logs and analytics.
- [ ] Full fake-provider journeys pass in frozen Playwright Chromium, Firefox and WebKit; physical Android Chrome and iPhone Safari complete the full journey and Maps handoff.
- [ ] Keyboard, mobile/desktop screen reader, focus restoration/live region, 200% text, 360px layout and 320 CSS-pixel reflow checks pass.
- [ ] Maps handoff and copy fallback pass.
- [ ] IndexedDB/cache contain no provider-derived result; session isolation, 24-hour input expiry, memory fallback, no-store responses and deletion pass.
- [ ] Public Terms/Privacy and Google attribution pass policy review; handoff disclosure and telemetry exclusion are verified.
- [ ] Cloud Run min `0`, max `1`; Maps project daily quota configured.
- [ ] Dedicated pilot project passes `verify-infra.ps1`; upload manifest/image layers exclude `.env*`, `.local/`, credentials and trip artifacts; deployed digest and rollback revision are recorded.
- [ ] Routes cap 1,200, daily quota 650, resolver cap 100, service-wide concurrency four, and separate audited kill switches are exercised.
- [ ] Billing alerts/spend cap status recorded; owner knows alerts alone do not cap spend.
- [ ] Rollback and provider-outage runbooks exercised once.
- [ ] Consent, outcome collection, retention and deletion copy approved.
- [ ] First five invite codes issued individually; public signup remains disabled.

