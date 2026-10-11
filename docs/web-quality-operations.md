# Web quality, security, and operations plan

Status: proposed controls and release gates for an invite-only pre-seed pilot. No checks described as runtime monitoring have been deployed, and no real-web validation has been executed.

## Quality strategy

The planner is tested independently from the provider and UI. Live provider tests prove integration behavior; they do not replace deterministic correctness tests or establish real driving accuracy.

| Layer | Automated checks | Manual checks | Pilot gate |
|---|---|---|---|
| Planner arithmetic | Unit and property tests for all derived times, feasibility and tie-breaks | Review representative timelines | 100% pass; zero false-feasible result |
| Search quality | At least 1,000 authored/synthetic nonmonotonic duration surfaces compared with exhaustive one-minute oracle | Inspect worst-regret fixtures | Median regret <=2 road min; p95 <=5; p99 <=10; <=24 production estimates |
| Time handling | UTC instant calculations plus IANA display-zone tests, DST gaps/overlaps and midnight boundaries | Verify Toronto DST examples in browser | No silent time correction or ambiguous repeated time |
| Provider contract | Fake server and schema fixtures for valid, fallback, partial, timeout, 429, 4xx, 5xx and malformed replies | One bounded live smoke test in pilot project | Missing/invalid leg never produces a plan; key absent from output |
| API | Schema bounds, session, CSRF, quota, idempotency race, retry budget and safe-error tests | Replay a duplicate request from browser dev tools | Stable codes; one billed computation per idempotency key |
| Web domain/UI | Component tests for every form and state; serialization migrations | 360px and 200% zoom review | Every field editable; stale/result state survives reload |
| End to end | Playwright happy path, impossible plan, provider outage, denied location, offline shell and Maps URL construction using fake provider | Android Chrome and iPhone Safari handoff; Edge/Firefox desktop smoke | Complete journey passes on selected browser matrix |
| Accessibility | Semantic queries, automated accessibility scan, focus-order tests | Keyboard-only, screen reader, contrast and zoom checks | No critical/serious automated finding; all actions reachable and named |
| Performance | Bundle budget, API latency test with fake provider, bounded concurrency | Cold-start measurement on pilot Cloud Run | Web interaction remains responsive; full-plan p95 <=12 sec in pilot |
| Privacy/security | Secret scan, dependency audit, CSP/session/header tests, log-redaction tests | Inspect browser bundle/network/log samples | No credential or precise location in bundle, logs or analytics |
| Cost | Assert provider-call caps and retry accounting | Reconcile estimated usage with billing after 5, 10 and 20 plans | Median <=20, p95 <=24 route estimates; spend inside pilot cap |

Planner fixtures include constant, rising, falling, sharply nonmonotonic and multi-peak traffic; route changes; tight/loose deadlines; 15–180 minute visits; one-to-eight-hour windows; equal scores; partial outages; all access allowances; toll policies; visit windows; maximum-wait constraints; and no-solution cases.

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
| P0 | Routing credential copied from frontend or repository | Use server-side ADC/OAuth where supported; otherwise Secret Manager and API restriction; secret scanning | Search built assets/history; browser requests contain no credential |
| P0 | Public endpoint used to generate paid calls | Invite-only session, per-session and global quotas, 24-call plan cap, max one instance, Maps quota and kill switch | Missing/expired session gets zero provider calls; cap cannot be bypassed concurrently |
| P0 | Precise locations leak through logs/analytics/errors | Allowlisted telemetry schema, request-body logging disabled, redaction tests, no free text in error tracking | Seed canary address never appears in captured logs or events |
| P0 | Planner returns unsafe false-feasible result | Typed models, deterministic feasibility invariant, missing-leg fail-closed behavior | Property tests and injected provider failures produce no false feasible result |
| P0 | XSS steals session or trip data | No raw HTML rendering, strict CSP, dependency review, output encoding, `HttpOnly` session | Stored/reflected payload tests cannot execute; CSP blocks inline script |
| P0 | CSRF spends quota or modifies outcomes | Strict same-site cookie, CSRF token, Origin validation, JSON-only mutation endpoints | Cross-origin requests fail before provider call |
| P1 | Session guessing/fixation/replay | High-entropy opaque IDs, rotate on invite exchange, hash at rest, short expiry, revoke/logout | Old and attacker-supplied session fail after rotation/revocation |
| P1 | SSRF/open redirect through pasted Maps URL | Never server-fetch arbitrary input; allowlist schemes/hosts and parse locally; otherwise treat as search text | Internal/file/custom schemes are rejected without network access |
| P1 | Oversized/hostile fields exhaust service | Strict lengths, duration/range bounds, payload limit, timeout and bounded concurrency | Boundary/fuzz inputs fail quickly with stable 4xx |
| P1 | Dependency or CI compromise | Lockfiles, pinned actions/images, least-privilege deploy identity, dependency and container scan | CI cannot read production provider access on pull requests |
| P1 | Provider terms violated by caching/training | In-request coalescing only; no persistent raw response/polyline; policy review before new storage | Storage inspection contains no raw provider body or geometry |
| P2 | Notification or shared-result privacy | No push in pilot; copied/share text omits precise origin by default | Shared text snapshot contains only user-selected fields |

The exact trip payload is operational data, not analytics. It is processed in memory and returned to the browser. Firestore contains pseudonymous access and quota records only. Outcome collection accepts no GPS trace and warns users not to enter addresses in feedback.

## Web security baseline

- HTTPS only; HSTS after confirming the deployment domain behavior.
- `Content-Security-Policy` with self-hosted scripts/styles, no `unsafe-eval`, and explicit connect/form/frame sources.
- `X-Content-Type-Options: nosniff`, restrictive `Permissions-Policy`, `Referrer-Policy: no-referrer`, and clickjacking protection through CSP `frame-ancestors`.
- `__Host-` session cookie with `Secure`, `HttpOnly`, `SameSite=Strict`, and no `Domain` attribute.
- CSRF token and strict Origin check for state-changing routes.
- Strict Pydantic schemas, length/range limits, and opaque client-safe errors.
- Separate service identity with only permissions required for Routes/Firestore/monitoring.
- No service-account key file; Cloud Run uses workload identity/Application Default Credentials.
- No precise trip in URLs, log attributes, analytics, crash reports, or support screenshots.
- User-visible **Delete this plan** and **Delete pilot data** actions with verified deletion of session/outcome records.

Google recommends a server proxy for web-service APIs and restricting/monitoring credentials. [Maps API security](https://developers.google.com/maps/api-security-best-practices).

## Cheap release process

Use one cloud environment for the private pilot and a complete local fake-provider environment. This avoids paying for idle staging infrastructure.

1. Pull request runs Python unit/contract tests, frontend unit tests, Playwright fake-provider journeys, lint/type checks, secret scan, and dependency audit.
2. Build one immutable container and record its digest.
3. Run the container locally against the fake provider and a single bounded live smoke test only when provider integration changed.
4. Deploy a new Cloud Run revision with no minimum instances.
5. Run authenticated smoke tests before inviting users back.
6. Move traffic to the revision and monitor the first five calculations.
7. Roll back to the prior known-good revision on a release-gate failure.

Production-style Terraform, multiple cloud environments, continuous canaries, paid security scanning, and 24/7 on-call tooling are deferred until use justifies them. Deployment configuration stays versioned in `infra/cloud-run-service.yaml` and `infra/deploy.ps1`.

## Proposed service objectives

These are pilot targets, not measured performance:

- at least 98% of authenticated plan requests receive a valid plan or correct typed no-plan response each pilot week, excluding user validation errors;
- p50 end-to-end plan latency <=8 seconds and p95 <=12 seconds;
- provider/schema failure rate <2% over 20 or more attempts;
- zero provider calls after the hard per-plan, per-session, or global quota;
- zero confirmed credential or precise-location leaks;
- RTO four hours for a bad release or provider integration failure;
- RPO zero for source/configuration and up to 24 hours for pseudonymous pilot feedback; active plans remain local to users.

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

Do not record addresses, coordinates, place names, route descriptions, geometry, full request/response bodies, exact trip timestamps, invite codes, session cookies, or feedback text in telemetry.

Proposed alerts:

- provider call volume reaches 50%, 80%, or 100% of the daily cap;
- estimated spend reaches CA$1, CA$5, or CA$10 in the pilot month;
- three consecutive provider failures;
- API 5xx rate above 10% over five minutes when at least five requests occurred;
- any plan exceeds 24 route estimates;
- redaction canary or credential appears in logs;
- Cloud Run revision fails readiness or repeated cold starts exceed the 20-second request deadline.

Billing alerts alone do not stop spending. The Maps quota, session quotas, provider kill switch, maximum instance count, and an eligible spend cap are enforcement controls. [Google Cloud budget guidance](https://docs.cloud.google.com/billing/docs/how-to/budgets).

## Runbooks

### Provider outage or malformed responses

1. Enable the provider kill switch.
2. Preserve draft/old result as stale and show maintenance copy.
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

The source repository and immutable container are the recovery source. A Cloud Run loss is recovered by redeploying the last known-good digest. Firestore holds no unique trip plan; lost invite/session/quota data can be recreated and all users re-invited. This permits a cheap recovery posture without paid database backups during the pilot.

Proposed retention:

- session: 14 days after last pilot use;
- idempotency metadata: 24 hours, with no location fields;
- quota counters: 35 days;
- structured operational logs: 14 days;
- voluntary outcomes: 90 days or earlier deletion request;
- precise trip requests/provider responses: never persisted by the application;
- browser plan: until the user deletes it or local storage is cleared.

Retention and deletion behavior must be verified before the first external tester.

## Pilot release checklist

- [ ] All acceptance IDs pass in CI.
- [ ] Worst oracle-regret fixtures reviewed and production search stays within 24 estimates.
- [ ] No false-feasible or missing-leg recommendation in tests.
- [ ] Provider credential absent from code, image history, web bundle and browser requests.
- [ ] Session, CSRF, quota, idempotency and kill-switch tests pass.
- [ ] Location redaction canary absent from logs and analytics.
- [ ] Android Chrome and iPhone Safari complete the full journey; desktop Edge and Firefox smoke pass.
- [ ] Keyboard, screen reader, 200% zoom and 360px layout checks pass.
- [ ] Maps handoff and copy fallback pass.
- [ ] Cloud Run min `0`, max `1`; Maps project daily quota configured.
- [ ] Billing alerts/spend cap status recorded; owner knows alerts alone do not cap spend.
- [ ] Rollback and provider-outage runbooks exercised once.
- [ ] Consent, outcome collection, retention and deletion copy approved.
- [ ] First five invite codes issued individually; public signup remains disabled.

