# Decisions

2026-10-10: Specialist security, testing, and operations reviews tightened the web plan before implementation. Rank exact provider seconds and reserve the leave-when-ready baseline; successful output means best evaluated only. Persist user-entered inputs and permitted identifiers locally, but keep provider-derived route results in memory pending explicit provider-policy approval. Use workload identity, transactional quota reservation and idempotency leases across workers, structured outcomes without free text or absolute timestamps, public Terms/Privacy and Google attribution, and explicit Maps-handoff disclosure. A crash conservatively consumes its quota reservation; duplicate suppression does not promise replay of a lost response.

2026-10-10: Fixed the cheap pilot envelope at 24 Routes units per full computation, 1,200 across the pilot including 240 for smoke/recovery, 650 per day, and 100 paid location-resolution calls. Use a dedicated pilot project, scale-to-zero Cloud Run, one digest-pinned container, versioned bootstrap/verification scripts, passive monitoring, and no paid staging project or database backup. Pseudonymous research records are disposable during this pilot; source/configuration/image digests have RPO zero.

2026-10-10: The user allowed a web start when it reduces pre-seed cost. Chose a mobile-first responsive web application before native Android. The pilot will use one React/FastAPI container on scale-to-zero Cloud Run, session-scoped local input storage, a small Firestore footprint for invite sessions/quotas/idempotency/structured outcomes, server-side routing credentials, and Google Maps handoff. PWA installability follows the core journey; native Android is gated by measured demand for reminders, sharing, widgets, Assistant actions, or app-store distribution. No cloud resource has been deployed.

2026-10-10: Replaced the manual concierge stage with an assisted web pilot. The working web app will perform calculations for 5–8 invited users and at least 20 plan attempts; founder assistance is limited to observing and recovering failures. This preserves cheap behavioral validation without building native surfaces or operating a manual route service.

2026-10-10: The planner remains deterministic. A future language model may parse chat/voice into a typed request but cannot decide feasibility or calculate schedules. Production search is capped at 24 route estimates per plan; live expansion stops if the quality gate cannot fit that cost budget. Result language is “best evaluated” or “best forecasted option checked,” never exact or guaranteed.

2026-10-10: User paused the arrival-deadline work and requested a plan for a different feature: choose a same-day home departure for a fixed-duration visit while returning home by a deadline and minimizing total predicted driving time. Project 2 now has an On Hold column containing issues #1–#3. No new feature issue or implementation was created pending user approval of the plan and defaults.

Record date, decision, evidence, alternatives and consequences.

Initial: project-scoped Codex agents; inherit parent model and permissions; at most three concurrent subagents. Application stack and product idea remain undecided.

2026-10-06: Verified all nine custom agent definition files exist and their names are exposed as callable roles in this session. Main agent acts as coordinator; activate specialists only as needed, with at most three concurrent subagents. Individual agent execution has not been smoke-tested. Product/customer/MVP remain undecided pending user discovery.

2026-10-06: User proposed arrival-deadline commute planning. Draft recommends one-city driving MVP, departure-specific traffic forecasts and explicit allowances. Google Maps already has Arrive by; accuracy superiority and willingness to pay remain unvalidated. No exact-arrival guarantee; LLM optional for input, traffic provider required for estimates. Await user confirmation of geography and recurring-alert scope.

2026-10-06: User chose method validation before full application. Platform choice and application build deferred. Separate calculation checks from prospective real-trip accuracy; see docs/method-test-plan.md. No API calls or real-trip measurements executed.

2026-10-07: User supplied first Markham-to-Mississauga test route with arrival deadline 2026-10-07 09:30 EDT. Deadline confirmed future at initial check. Provider access remains to establish; no prediction calculated. Home address omitted from planning files.

2026-10-07: Local key presence verified without output. Built bounded JS calculation module and Python local runner; seven JS synthetic assertions passed. Live request attempt failed because network access returned EACCES; no provider prediction retrieved and billed cost unknown. Shell execution also failed to start, so Python runner execution remains unverified. Local user execution is the next step. Trip and forecast files are in ignored .local/.

2026-10-07: User executed Python runner successfully; local saved forecast reviewed. 47 route requests returned; latest evaluated feasible departure 08:29 EDT, predicted driving arrival 09:29:48 EDT for 09:30 deadline, no buffer, toll avoidance requested, route ON 401 Express. This verifies live forecast retrieval and recommendation output, not actual journey accuracy. Actual departure/arrival and billing remain unmeasured. Prediction frozen for prospective comparison.

2026-10-07: User requested scheduled forecast comparison instead of real driving. Added tools/check_at_departure.py: waits for saved 08:29 EDT departure, opens Maps route and makes one live Routes API request, stores timing/duration comparison in ignored .local/. UI observation remains manual; API result is clearly identified. Six Python self-tests passed via local process execution. No live scheduled run started. Computer must remain awake; stale starts and >60-second missed targets stop without API call. This measures forecast agreement, not actual journey accuracy.

2026-10-07: Reviewed user live check: forecast increased 692 seconds (~11.53min), exceeding zero-buffer deadline slack. Researched conservative Google traffic model, adaptive advance rechecks, empirically calibrated margins, and HERE arrivalTime. Recommend testing conservative + adaptive planning before custom prediction. One sample does not establish universal correction or provider superiority. No-driving validation remains forecast stability only. See docs/departure-method-alternatives.md.

2026-10-07: User requested another-provider benchmark and challenged HERE ranking. No best provider established. Verified HERE arrivalTime and TomTom arriveAt plus diagnostic travel-time fields. Proposed TomTom as next diagnostic benchmark; credential/setup and future test date pending. Cross-provider later estimates remain forecasts, not ground truth.

2026-10-07: TomTom key presence verified. Benchmark implementation uses TomTom-resolved coordinates for both providers to align endpoints, user reviews geocode results, then explicit future deadline planning and live checks at both nominees. Next proposed test 2026-10-08 09:30 EDT; original October 7 config preserved. Live agent network attempt failed; user-local execution required.

2026-10-07: Coordinator reran 10 offline benchmark tests successfully and verified CLI help. Planning caps 50 Google/6 TomTom; watch up to4calls. No successful live provider response yet; prepare geocode preview locally first.

2026-10-07: User supplied explicit origin/destination map coordinates. Saved only in ignored .local/benchmark-locations-user.json, labeled user-selected points; benchmark now bypasses geocoding and passes identical points to both providers. Ten offline tests rerun successfully. Future October 8 09:30 EDT live plan remains to execute locally. Printed watch command made independent of current working directory.

2026-10-10: Both Oct 8 live checks completed. Early zero-buffer recommendations failed later estimated deadline feasibility; actual journey accuracy remains unmeasured. Preserve incomplete prototype in existing private UM-NLP/whenshouldgo with sanitized results and issue-linked repo board. Native Projects operations unavailable. Full app deferred. Ten benchmark tests, six scheduled comparison checks and JavaScript suite passed during documentation preparation. No new paid calls made. Earlier entries are historical snapshots; experiment-results.md and backlog.md hold current status.

2026-10-10: User designated GitHub user Project 2 as authoritative board. Repository snapshot now links it. Native Project synchronization remains pending: no Projects connector, no connected browser, Windows native pipe unavailable. Issues #1–#3 exist but Project membership/statuses are unverified.

