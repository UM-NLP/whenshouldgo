# Web-first delivery roadmap

Status: proposed execution roadmap for one founder/developer working with the nine-role AI product team. Ranges are planning estimates, not delivery promises. No external tester recruitment or paid service purchase has been executed.

## Operating constraints

- Pre-seed budget: no paid acquisition, no app-store work, no subscription system, no custom domain requirement, and no always-on compute.
- One complete web journey before companion features.
- One Cloud Run service, minimum instances zero, maximum one during the pilot; atomic Firestore quotas remain authoritative because the instance maximum may be exceeded briefly.
- Hard cap of 24 paid route estimates per full plan, 1,200 Routes units for the whole pilot, 650 Routes units per day, and 100 paid location-resolution calls.
- Invite-only cohort of 5–8 users and at least 20 real plan attempts.
- Native Android, widget, voice, embedded navigation, and iOS remain gated.
- Old one-way exact-departure work stays On Hold as historical evidence.

## Stage plan

| Stage | Effort assumption | Deliverable | Exit gate |
|---|---:|---|---|
| 0. Interview/product contract | 3–4 focused days | Typed intent/constraint state, versioned question policy, acceptance IDs, search/cost budgets, pilot consent and stop/go thresholds | Every supported phrase maps to confirmed timeline semantics; the stopping rule and minimal-question benchmark are frozen |
| 1. Interview and planner core | 6–9 days | Rule parser, deterministic next-question engine, generalized Python planner, fake provider, interview oracle, route-search oracle and typed explanations | Zero missed hard constraints or false-feasible fixtures; interview p95 question regret <=1; route p95 regret <=5 road min; production <=24 estimates |
| 2. Provider and API | 4–6 days | Interview endpoint, Google adapter, location confirmation, session/quota/idempotency middleware, OpenAPI and fake/live modes | Intake makes no route call; missing leg fails closed; credentials server-side; bounded live smoke passes |
| 3. Responsive web journey | 6–9 days | Adaptive composer/questions, editable timeline, in-memory result, refresh, local input recovery, Maps handoff and all recovery states | Typical saved-home scenario reaches review in <=3 follow-ups; full fake journey passes at 360px, keyboard-only and selected browsers |
| 4. Cheap pilot deployment | 2–3 days | One scale-to-zero Cloud Run service, invite access, kill switch, metrics, quotas, alerts and rollback | Release checklist passes; first five plans reconciled with billing data |
| 5. Assisted web pilot | About 2 weeks elapsed | 5–8 invited users, 20+ real plan attempts, interviews, outcome check-ins and evidence report | Adoption, repeat-use, deadline, accuracy, latency and cost gates reported with counts |
| 6. Decision and focused iteration | 1–3 days | Explicit stop, revise, or continue decision; prioritized evidence gaps | No expansion without a passed value/cost gate |
| 7. PWA or Android expansion | Gated | Installability/offline shell first; native Android only if native capabilities are demanded | Surface-specific interest and retention gate met |

The stages are sequential where evidence is required. UX and fake-provider frontend work can overlap with planner-core work after the API contract is frozen.

## Stage 0 — product and evaluation contract

### Work

- Freeze the MVP and non-goals in [web-product-plan.md](web-product-plan.md).
- Define `InterviewState`, `InterviewDecision`, `TripRequest`, `TimeConstraint`, `DurationConstraint`, `PlanResult`, `Alternative`, `NoPlanReason`, and error schemas.
- Freeze stable question IDs, prerequisites, reasons, quick replies, default provenance, ambiguity branches, and the `READY_TO_PLAN` stopping rule in `contracts/interview-policy.yaml`.
- Author gold dialogues for all supported intents, ambiguous “by/at/closing” wording, several simultaneous constraints, corrections, unsupported requests, and fully specified requests.
- Assign acceptance IDs to every P0 requirement.
- Freeze baseline: leave as soon as ready under the same provider and route policy.
- Freeze exact-second ranking, the deterministic search schedule and required samples, independent-oracle quality thresholds, a reserved leave-when-ready baseline, and the 24-estimate production cap.
- Define invite consent, prohibited analytics fields, outcome fields, retention, and deletion.
- Freeze pilot denominators for eligible requests, acceptance, adherence, deadline success, forecast error, repeat use, and cost per successful task.
- Resolve provider storage, attribution, Terms, Privacy, location-resolution, and workload-identity requirements.
- Verify current provider pricing/free caps immediately before live deployment.

### Exit evidence

- Reviewed schema examples for next-question, contradiction, ready-to-confirm, feasible, impossible, provider failure, incomplete search, quota, and validation cases.
- Human-authored minimum-safe question counts for the gold dialogue suite.
- Cost worksheet with pricing-table date and a 20-plan worst-case pilot estimate.
- Written stop conditions.

## Stage 1 — interview and planner core

### Work

- Create the new `backend/src/when_shouldgo` package without importing old scripts.
- Implement a bounded rule parser that proposes facts with source spans and never confirms them.
- Implement the deterministic question policy: contradictions, hard anchors, universally required facts, feasibility-changing ambiguity, then grouped assumptions.
- Implement interval propagation and arithmetic contradiction checks before provider access.
- Build an independent breadth-first/dynamic-programming interview oracle that shares schemas but no production completeness or selection helpers.
- Implement typed domain models and timezone-safe schedule arithmetic.
- Implement the `RouteProvider` protocol and deterministic fake provider.
- Implement global-anchor and local-refinement search for nonmonotonic traffic.
- Support outbound-only and return-to-origin plans with fixed starts, latest arrival/finish/departure/home constraints, windows, and exact/minimum/range durations.
- Implement exact-second objective ranking, exact-tie handling, maximum-wait/early-arrival bounds, alternatives, binding-constraint explanations, and typed no-plan explanations.
- Build at least 1,000 versioned synthetic surfaces and an independent exhaustive one-minute oracle that shares no feasibility or scoring helpers with production code.
- Reserve one candidate chain for the leave-when-ready baseline and return `SEARCH_INCOMPLETE` only when a required sample fails or the fixed search schedule cannot finish within its cap.
- Preserve existing scripts under `experiments/arrival_deadline` only after their current checks are frozen.

### Exit evidence

- 100% arithmetic/timezone assertions pass.
- 100% of curated P0 dialogues produce the correct confirmed `TripRequest`; generated supported cases reach at least 99%.
- Deterministic question order, no repeated question, guaranteed termination, median question regret `0`, p95 <=`+1`, and maximum `+2` versus the interview oracle.
- Median follow-ups <=3 and p90 <=5 after the initial description; fully specified requests need only grouped confirmation.
- Zero interview-triggered route call before `READY_TO_PLAN` and zero unconfirmed hard candidate reaching the planner.
- No recommendation with a missing leg.
- No false-feasible result.
- Median regret <=2 road minutes, p95 <=5, p99 <=10.
- Deterministic results for identical inputs.
- Production search never exceeds 24 estimates including retries.

If quality misses the gate, do not hide it by increasing paid calls. Change the search, provider, objective, or product promise.

## Stage 2 — provider and API

### Work

- Implement the official Google Routes client with a dedicated least-privilege Cloud Run service identity and ADC/OAuth. Treat a key plus static egress/IP restriction as a separately approved fallback architecture.
- Implement address/current-coordinate resolution and explicit confirmation tokens.
- Build atomic single-use invite exchange, strict Origin/CSRF checks, payload validation, transactional quota reservation, idempotency leases, provider kill switches, and safe error middleware.
- Build the stateless `interviews:advance` endpoint before compute. Enforce bounded text, typed answer schemas, stable question IDs, no raw-text logging, and independent server revalidation of the final confirmed request.
- Build compute, refresh, leaving-now, structured pilot-outcome, and authenticated pilot-data deletion endpoints.
- Record redacted provider count, status, latency, retry, billable unit, and pricing version.
- Add fake-provider contract tests plus a minimal live smoke test.

### Exit evidence

- Browser bundle/network contains no provider credential.
- Invalid, unauthenticated, CSRF, or quota-exceeded requests make zero provider calls.
- Interview requests make zero route-provider calls and raw utterances, purpose text, locations, and exact times are absent from logs and analytics.
- Same-key duplicates across two processes make at most one computation attempt and return typed pending/already-computed responses; a crash conservatively spends its reservation.
- Fallback/malformed/missing provider responses fail closed.
- No canary address or coordinate appears in logs.

## Stage 3 — responsive web journey

### Work

- Build the opening trip composer, quick-start branches, one adaptive question card at a time, a collapsible **Trip so far** summary, and an editable confirmation timeline.
- Add **Why we need this**, Back, explicit Continue, and **Review all details** after five follow-ups; never show fixed step counts.
- Add manual locations first; foreground browser location is optional and contextual.
- Build loading, recommendation timeline, alternatives, freshness, refresh, and leaving-now behavior.
- Implement invalid, ambiguous, denied, loaded-tab-offline, provider-error, missing-leg, impossible, incomplete-search, stale, passed-departure, Maps-unavailable, storage-cleared/corrupt/quota-failed, and DST states.
- Persist only versioned user-entered constraints and permitted location identifiers in session-scoped IndexedDB; keep raw utterances/purpose in active memory, keep provider-derived results in memory, and expire local data within 24 hours after the final hard time anchor.
- Construct separate outbound and return Google Maps URLs.
- Add privacy-safe funnel events and voluntary outcomes.
- Add local share/copy; do not put precise trip data in URLs.
- Add public Terms and Privacy pages, accessible Google attribution, explicit handoff disclosure, `Cache-Control: no-store`, and a future-service-worker bypass for `/api/`.

### Exit evidence

- One complete journey at 360px, 200% text, and WCAG 2.2 AA reflow at 320 CSS pixels.
- Keyboard-only, focus restoration/live-region, and manual mobile/desktop screen-reader checks pass.
- The full fake-provider suite passes in frozen Playwright Chromium, Firefox, and WebKit versions; physical Android Chrome and iPhone Safari complete a handoff smoke test.
- Reload restores inputs and requires a new calculation; an already-loaded offline tab preserves its in-memory stale display, while offline reload waits for the PWA stage.
- Android Chrome and iPhone Safari open Maps or the browser fallback.
- Every accepted input maps to a visible review field and typed API field.
- Common paths work through quick replies without typing; a complete initial answer is never fragmented into redundant questions.

## Stage 4 — deployment within a small budget

### Work

- Build one container serving frontend and API from a clean commit, locked dependencies, and a pinned base image; deploy the recorded image digest.
- Deploy in Toronto with request-based Cloud Run, min `0`, max `1`, 512 MiB memory, one CPU, concurrency `8`, and a 20-second request timeout.
- Use the default `run.app` domain during the pilot.
- Create one Firestore database for pseudonymous invite/session/quota/idempotency and structured outcome data, with versioned schemas and TTL policies.
- Add `bootstrap.ps1` and `verify-infra.ps1` for APIs, IAM, Firestore, Artifact Registry, quotas, secrets, alerts, and log settings; use a dedicated pilot project without a paid staging project.
- Add explicit `.gcloudignore` and `.dockerignore`, inspect the upload manifest and image layers, and deploy a no-traffic tagged candidate before promotion.
- Set the 1,200-unit pilot reservation cap, 650-unit Routes daily quota, 100-call resolver cap, per-session quotas, CA$1/5/10 alerts, an eligible spend cap where available, and separately auditable Routes/resolver kill switches.
- Exercise provider-outage, runaway-cost, rollback, credential, and privacy runbooks.
- Use passive request/error monitoring plus a manual pre-session smoke test; `/healthz` must call neither Maps nor Firestore.

### Pilot cost envelope

| Item | Pilot choice | Expected cost behavior |
|---|---|---|
| Frontend/API compute | Cloud Run scale-to-zero, max one instance | Intended to fit current free tier at pilot volume; verify invoice |
| Session/quota/outcome data | Firestore free quota | Tiny volume; structured outcomes only, no raw trip history |
| Domain | Default `run.app` | No domain purchase |
| Routing | <=24 estimates per plan | Dominant marginal cost; monitor free cap and actual billing |
| Location resolution | 100-call whole-pilot cap | Track separately; avoid keystroke-by-keystroke paid calls initially |
| Analytics | Structured application metrics/logs | No paid product analytics vendor |
| CI/deployment | One digest-pinned image; no paid staging environment | Keep build minutes and Artifact Registry storage small; clean up old untagged images |
| Acquisition | Direct invitations and interviews | No paid campaign |

Twenty plans with one full refresh each have a hard maximum near 960 route estimates. The 1,200-unit whole-pilot reservation leaves 240 units for smoke and recovery checks and stays below the currently documented 5,000-event monthly Routes free cap if no other project on the billing account consumes it. Treat that as a planning estimate and verify the actual billing account.

At the currently documented first paid band for Compute Routes Pro, US$10 per 1,000 events, a 24-estimate full optimization has about US$0.24 marginal routing cost after the free cap; a second full optimization doubles that. This is why refreshes are explicit and bounded. Location resolution, hosting, tax and currency conversion are separate. Recheck the [official price list](https://developers.google.com/maps/billing-and-pricing/pricing) before every pilot or pricing decision.

## Stage 5 — assisted web pilot

“Assisted” means the product computes plans while the founder observes problems and helps users recover. It is not a manually operated route service.

### Cohort

- 5–8 GTA drivers with at least two eligible monthly outings;
- priority segment: parents/caregivers with a firm pickup or home deadline;
- exclude emergency, commercial, safety-critical medical, multi-stop, and unsupported-mode trips.

### Evidence collected

- question IDs/reasons, follow-up count, skips/edits, time to confirmation, and request-shape category without utterances, purpose text, exact times, or locations;
- hard-constraint capture checked against the user's confirmed timeline and post-task interview;
- constraints completed and result category;
- recommendation viewed and accepted/rejected reason;
- evaluated baseline and predicted saving;
- refresh/adherence behavior;
- voluntary adherence/deadline outcome and browser-derived rounded leg durations/error offsets; no absolute trip timestamps;
- provider drift, latency, calls, retries, and billed cost;
- second eligible plan and reuse intent;
- support incident and privacy/deletion request counts.

Preregister these denominators before the first invite:

- an eligible supported request is a confirmed compute attempt inside documented product bounds; provider, search, timeout, and app failures count, while invalid/unsupported inputs do not;
- an accepted recommendation is a viewed `FEASIBLE` result the user explicitly chooses;
- a followed plan leaves within plus or minus five minutes of the last accepted recommendation, with material stops, route changes, and visit changes classified separately;
- deadline success uses followed plans with a valid check-in;
- forecast error uses legs with valid opt-in timing and the frozen forecast snapshot;
- repeat use uses testers who report a second eligible opportunity, alongside the raw invited-user rate;
- estimated and invoice-reconciled cost per success divide all provider cost, including failures, retries, resolution, refresh, leaving-now, and smoke calls, by `FEASIBLE` plus correctly computed `NO_FEASIBLE_PLAN` tasks.

### Go/revise/stop thresholds

Continue toward PWA/retention work when:

- no missed or invented hard constraint appears in supported pilot requests;
- at least 80% of invited users complete a supported interview without founder intervention;
- median follow-ups are at most three for saved-home requests and fewer corrections occur than in the complete-input comparison;
- at least 18 of the first 20 eligible supported requests produce `FEASIBLE` or a correct `NO_FEASIBLE_PLAN`, with no app-caused failure;
- at least 50% of feasible recommendations are accepted;
- at least five of eight users create a second plan when another eligible trip occurs;
- median predicted saving among accepted plans is at least ten minutes;
- at least 90% of followed plans report returning by the entered deadline;
- median per-leg forecast error is at most ten minutes;
- median latency <8 seconds and p95 <12 seconds;
- median route estimates <=20 and p95 <=24;
- no severe privacy, credential, or false-feasible incident occurs.

Revise when users value an earlier finish more than minimum road time, waiting is unacceptable, error is segment-specific, or input burden prevents activation. Add an earliest-finish/maximum-wait objective before adding surfaces.

Stop this segment if fewer than four interviewees have recurring demand, repeat planning remains below 20% after one focused iteration, or marginal cost cannot support any plausible price.

## Stage 6 — evidence decision

Publish a sanitized pilot report containing counts, denominators, limitations, measured invoice cost, observed latency, provider failures, prediction error, adoption, repeat use, and unresolved risks. Keep raw locations out of Git.

Decision options:

- **Stop:** archive the web app and preserve the evidence.
- **Revise:** change customer segment, objective, constraints, provider, or search budget; run another bounded pilot.
- **Continue:** add the smallest retention feature users requested.

Do not implement payment until repeat use and willingness to pay are demonstrated. Do not implement native Android merely because the web pilot is technically successful.

## Stage 7 — gated expansion

### PWA first

Add a manifest, icons, service worker, install guidance, offline shell, update recovery, and install analytics. The service worker caches static assets only. This uses the existing codebase.

### Native Android later

Choose Android when evidence shows that at least one native capability materially affects retention:

- reliable time-sensitive reminders;
- share-sheet capture from Maps;
- home-screen widget;
- Assistant/App Actions;
- better background refresh;
- app-store discovery or trust.

Reuse the OpenAPI contract and backend. Embedded Navigation SDK remains a separate business case.

## GitHub issue plan

| Issue | Scope | Dependencies | Completion artifact |
|---|---|---|---|
| WEB-01 Finalize product, schema, acceptance and cost contract | Stage 0 | None | Versioned contract and reviewed thresholds |
| INTAKE-01 Build adaptive trip interview engine | Stage 1 | WEB-01 | Versioned question policy, rule parser, state machine, gold dialogues and minimal-question report |
| CORE-01 Build generalized deterministic planner and oracle evaluation | Stage 1 | WEB-01, confirmed TripRequest contract | Planner package, fixture report and call-budget result |
| API-01 Build interview/provider secure API | Stage 2 | WEB-01, INTAKE-01, CORE-01 interface | OpenAPI, fake/live contract evidence and redaction proof |
| WEB-02 Build responsive adaptive planning journey | Stage 3 | WEB-01, INTAKE-01; fake API available | Working browser journey and state matrix |
| OPS-01 Deploy invite-only, quota-capped pilot | Stage 4 | API-01, WEB-02, TEST-01 | Cloud Run revision, runbook exercise and cost controls |
| TEST-01 Verify interview minimality, correctness, browsers, accessibility and security | Cross-stage | INTAKE-01, CORE-01, API-01, WEB-02 | Acceptance report and release checklist |
| PILOT-01 Run assisted web validation | Stage 5 | OPS-01, TEST-01 | Sanitized 20-plan evidence report and decision |
| EXPAND-01 Gate PWA and Android surfaces | Stage 7 | PILOT-01 | Evidence-based build/defer decision per surface |

The coordinator owns integration and board status. Product, UI, AI, engineering, growth, security, testing, and operations findings are incorporated into the acceptance criteria for these issues.

## Nine-role review record

| Role | Review responsibility reflected in this plan |
|---|---|
| Coordinator | Integrated scope, dependencies, repository artifacts and board plan. |
| Product owner | Target user, costly problem, MVP boundaries, promise and gates. |
| UI designer | Adaptive composer/questions, quick replies, review/timeline journey, correction, mobile states and accessibility. |
| AI expert | Typed interview state, deterministic question policy, parser boundary, interview/route oracles and evaluation targets. |
| Web developer | React/FastAPI boundary, typed contracts, provider adapter and same-origin deployment. |
| Growth/monetization | Interviews, assisted pilot, adoption/retention evidence and contribution-cost measures. |
| Security reviewer | Identity, data minimization, provider policy, Maps disclosure, atomic spend controls and deletion. |
| Tester | Traceability, search completeness, multi-process races, browser/device matrix and pilot denominators. |
| Operations engineer | Reproducible pilot project, digest releases, quota envelope, monitoring, incidents and recovery. |

These are design reviews. Application behavior, live integration, accessibility on devices, deployment, billing and customer outcomes remain unexecuted until their roadmap stages.

