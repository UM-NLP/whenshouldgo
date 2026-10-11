# Web-first product plan

> Historical planner-centered plan. Superseded on 2026-10-10 by the [agent-first product plan](trip-assistant-product-plan.md). Preserve for reusable requirements and decision history.

Status: approved direction on 2026-10-10. Planning is complete; implementation has not started.

## Product decision

Build the first product as a **mobile-first responsive web app**, then make it installable as a Progressive Web App (PWA) after the core journey works. This is the cheapest useful way to validate the scheduling method and customer demand while preserving a clear path to Android.

The web app can already deliver the complete value loop:

1. understand the trip in the user's own words and ask only the missing questions that can change the plan;
2. calculate a round-trip schedule with server-side traffic data;
3. show when to start preparing and leave;
4. refresh the forecast;
5. open each leg in Google Maps.

One responsive codebase works on Android, iPhone, and desktop browsers. It avoids Play review, native SDK work, separate mobile releases, and native reminder complexity during the pre-seed validation stage. A web manifest and service worker can later make it installable, but installation is progressive enhancement; every core function must work in an ordinary mobile browser. See [PWA quality guidance](https://web.dev/articles/pwa-checklist) and [PWA installation](https://web.dev/learn/pwa/installation).

The first release will not modify the consumer Google Maps interface. It will accept normal inputs, optionally accept a pasted/shared Maps link after compatibility testing, and hand outbound and return navigation to Google Maps URLs. Trip data never enters this application's URLs; an explicit handoff sends the selected endpoints to Google and can place them in browser or Maps history.

## User promise

> Tell us what must happen on your trip: arrive, start, finish, leave, or get home by a certain time. We ask only what is needed to find the lowest-traffic feasible plan.

The interface uses **best forecasted option checked** or **best evaluated departure**. It never says exact, guaranteed, or always on time. Traffic forecasts can change.

## Evidence and assumptions

Verified:

- The earlier one-way experiment recorded an 11.53-minute forecast revision on one test and did not establish a reliable exact-departure method. See [experiment-results.md](experiment-results.md).
- Google Routes can return traffic-aware durations for specified departure times; these remain forecasts. See [traffic models](https://developers.google.com/maps/documentation/routes/traffic-model).
- Google Maps URLs can open directions or navigation from a web application and do not require a Maps URL API key. See [Maps URLs](https://developers.google.com/maps/documentation/urls/get-started).
- Browser geolocation requires HTTPS and user permission. Manual location entry must remain available. See [Geolocation API](https://developer.mozilla.org/en-US/docs/Web/API/Geolocation_API).

Hypotheses:

- Initial customer: GTA drivers making a same-day trip with at least one meaningful time constraint, such as a reservation, appointment, closing time, pickup, visit window, or return-home deadline.
- Parents/caregivers, appointment travelers, and time-limited errand drivers are candidate first segments; interviews must determine which has recurring demand.
- Users are willing to delay leaving when the predicted road-time saving is meaningful.
- The combined outbound, visit, and return decision is valuable often enough to create repeat use.

There is no verified demand, willingness-to-pay, retention, or market-size evidence yet.

## Job to be done

> When I have a same-day trip with one or more timing requirements, help me state those requirements naturally, ask only the questions needed to understand them, and find a feasible departure that minimizes forecasted driving time.

Primary value measure: forecasted driving minutes avoided versus leaving as soon as ready, using the same provider and route policy for both alternatives.

Guardrails:

- the complete visit fits before the safe return deadline under the forecast used;
- no result is shown when either leg is missing;
- actual forecast error and reported deadline success are measured separately;
- route calls, latency, and marginal cost are recorded for every plan;
- provider-estimated counterfactual savings are never described as actual saved time.

## Cheap MVP scope

The first pilot supports:

- mobile and desktop web browsers, designed for phone screens first;
- same-day driving plans;
- one start/return place and one destination;
- outbound-only and return-to-origin plans;
- fixed starts, latest arrivals, latest finishes, latest departures, home-return deadlines, flexible visit windows, and combinations of them;
- exact, minimum, or bounded activity duration when duration affects feasibility;
- manual address entry and optional foreground current location;
- an optional acceptable visit window;
- preparation, parking/walking, and safety allowances;
- toll, highway, and ferry preferences;
- one routing provider for all candidates in a plan;
- an adaptive conversational interview with quick replies, an editable timeline review, a recommendation, and two useful alternatives;
- manual refresh and freshness warnings;
- separate Google Maps handoffs for outbound and return legs;
- local browser storage for user-entered drafts and permitted location identifiers; route results remain in memory;
- private invite access and privacy-safe pilot analytics.

The pilot excludes accounts, payment, subscriptions, native push reminders, continuous background work, future-day monitoring, multiple stops, different return locations, other travel modes, autonomous LLM planning, a widget, voice assistants, embedded navigation, and a native Android build. The first parser is rule-based behind a typed interface; a later language-model parser may propose slots, but it cannot confirm constraints, decide feasibility, or calculate schedules.

## Adaptive interview contract

The opening question is deliberately broad:

> Where are you going, and what is fixed about the timing? You can mention a reservation, closing time, how long you need there, or when you must be home.

One answer may fill several facts. The app never asks again for a fact the user already supplied. It stores each fact as `UNKNOWN`, `CANDIDATE`, `CONFIRMED`, `DEFAULTED`, or `NOT_APPLICABLE`, together with its source. Hard constraints must be confirmed; a purpose label may choose a clarification branch but never creates a deadline by itself.

Supported timeline constraints are:

| User meaning | Typed constraint |
|---|---|
| â€œBe there by 2â€ | `READY_AT_DESTINATION <= 14:00` |
| â€œMy reservation is at 2â€ | `ACTIVITY_START = 14:00`, with a confirmed arrival allowance |
| â€œFinish before the store closes at 6â€ | `ACTIVITY_END <= 18:00` |
| â€œLeave there by 5â€ | `DEPART_DESTINATION <= 17:00` |
| â€œBe home by 6â€ | `HOME_ARRIVAL <= 18:00` |
| â€œVisit between 1 and 3â€ | Confirmed activity-start/end window |
| â€œStay at least one hourâ€ | `ACTIVITY_DURATION >= 60 minutes` |
| Several requirements | Intersection of every hard constraint; none is silently relaxed |

â€œArriveâ€ means parked and ready to begin after any confirmed access allowance. The review timeline makes this interpretation visible.

After each answer, the deterministic question policy chooses the highest-value unresolved item:

1. resolve a contradiction;
2. obtain a hard time anchor that bounds the search;
3. obtain a missing origin, destination, date/timezone, readiness, or duration required by the selected trip shape;
4. resolve language whose meaning changes feasibility, such as whether closing time means enter, start service, or finish;
5. ask about a default only when changing it could change the recommendation;
6. group remaining visible assumptions into one editable confirmation.

Trip purpose is optional and contextual. â€œRestaurant reservationâ€ usually leads to a fixed-start confirmation; â€œwalk-in restaurantâ€ may require a seating cutoff; â€œlunch at a friend's houseâ€ may be flexible. If the timing words are already clear, the app does not ask the purpose.

Visit duration is asked only when it affects a finish, return, or return leg. The app asks the earliest time the user can leave; preparation duration is requested only when the user wants a â€œstart getting readyâ€ time. Return questions are skipped for outbound-only plans. Route preferences are loaded from visible local defaults and asked only if missing or consequential.

The interview stops and enables calculation only when locations are confirmed, date/timezone/readiness are known, every required duration has known semantics, at least one hard time anchor bounds the search, all feasibility-changing ambiguity is resolved, constraints are consistent, and the user confirms one editable summary. Impossible arithmetic is rejected before any paid route call.

If the user selects **No fixed time**, the next question asks how late the outing may finish. â€œSometime todayâ€ never silently becomes midnight. If more than five follow-ups are required, the interface offers **Review all details** as a structured recovery path.

Safe visible defaults are today, the displayed local timezone, driving, return to origin when a return constraint is present, minimize total forecast driving seconds, saved route preferences, and ready now. The app never silently guesses duration, whether a stated time is exact, what a closing time means, business hours, or whether a preference is a hard requirement.

## Core journey

1. **Open link:** Show the value, forecast limitation, and **Plan a trip**. No permission prompt.
2. **Describe trip:** Accept one natural-language answer plus optional quick replies; parse all supported facts without making a route call.
3. **Clarify:** Ask one high-value question at a time. Explain why a purpose or timing distinction matters when it is not obvious.
4. **Confirm places:** Resolve the full address or pin. Request browser location only after **Use my location**.
5. **Review:** Show an editable â€œHereâ€™s what I understoodâ€ timeline with every hard constraint, timezone, allowance, and default. Edits invalidate affected results.
6. **Calculate:** Preserve inputs while the server checks the required leg or legs. Do not show a fake percentage.
7. **Recommend:** Lead with **Leave at [time]** and **Best forecasted option we checked**.
8. **Act:** Keep the input template locally, refresh, copy a privacy-safe summary, or explicitly open the relevant Maps URL.
9. **Return:** For round trips, offer **Leaving now** to obtain a current return ETA and warn about deadline risk.
10. **Learn:** Ask for a voluntary outcome and reuse response without collecting a GPS trace.

The result shows only relevant milestones: start preparing when requested, leave, road arrival, ready-at-destination, activity start/end, return departure/home arrival when applicable, slack for every hard deadline, leg durations, total road time, predicted saving against the evaluated leave-when-ready baseline, alternatives, provider, and calculation time.

### Example question paths

- **Friend visit:** â€œVisit a friend for an hour and be home by 6.â€ Ask only for missing destination and earliest departure; confirm the timeline.
- **Business closing:** â€œThe pharmacy closes at 6.â€ Ask whether the user must enter, start service, or finish by 6, then ask how long is needed if finish time matters.
- **Reservation:** â€œRestaurant reservation at 7.â€ Ask the destination and acceptable arrival allowance. Do not ask whether 7 is flexible.
- **Fully specified:** â€œReservation at 1, stay 90 minutes, home by 4, leaving from home after 11.â€ If places are known, proceed directly to one confirmation.

## Required states

| State | Required recovery |
|---|---|
| Empty | Ask for destination and show an example without pre-filling private data. |
| Unsupported or unclear wording | Highlight the unresolved timing meaning and offer short choices plus direct editing; never guess a hard constraint. |
| Contradictory constraints | Show the conflict on the timeline and ask which fact should change before calculation. |
| Invalid input | Inline error and focus on the first invalid field. |
| Ambiguous address | Candidate list or pin confirmation before calculation. |
| Location denied/unavailable | Continue with manual entry. |
| Loading | Preserve answers; allow cancel and retry. |
| Offline | Keep the input draft. A still-loaded tab may show its in-memory result as stale; an offline reload is unsupported until the PWA stage. Disable calculation and refresh until online. |
| Browser storage failure | Continue in memory and show **Not saved on this device**; allow retry or clear local data. |
| Provider failure | Show no new recommendation; keep any in-memory old result visibly stale. |
| Missing leg | Identify the failed leg and produce no recommendation. |
| Impossible | Show earliest forecasted home time, lateness, and explicit constraint changes. |
| Search incomplete | Explain that the app could not compare enough times reliably and offer retry. |
| Stale/passed departure | Recalculate from now before presenting a new action. |
| Maps unavailable | Open the universal Maps URL or let the user copy the destination. |
| Browser storage cleared | Start a new plan; the server has no trip-history dependency. |
| DST/timezone ambiguity | Display the zone and ask which repeated local time the user means. |

## Why the concierge stage changed

The original concierge proposal was a cost-control experiment: manually help a few users before paying to build a full native product. A thin web app is inexpensive enough to replace most of that manual work.

The new stage is an **assisted web pilot**:

- 5â€“8 invited users;
- at least 20 real plans over roughly two weeks;
- the working web app performs the calculation;
- the founder observes failures, interviews users, and manually fixes input or support problems;
- no paid ads, app-store launch, payment system, native notifications, or full account system.

This retains the learning value of concierge testing while producing reusable software.

## Validation gates

### Problem check

Conduct 8â€“12 interviews while the thin MVP is being built. A proposed gate is at least six people reporting two or more eligible trips per month and a concrete cost from mistiming. Ask directly whether they would wait 30â€“60 minutes to save 10â€“20 road minutes.

### Assisted web pilot

Proposed gates, to be agreed before collecting results:

- at least 20 completed real-plan attempts across at least five users;
- at least 90% of supported requests produce a valid plan or a correct no-plan explanation;
- zero missed or invented hard constraints in the authored interview benchmark;
- a fully specified request needs no clarification beyond location resolution and one grouped confirmation;
- at least 90% of supported benchmark conversations ask no more than one avoidable follow-up beyond a human-authored minimum;
- median follow-up count is at most three for saved-home scenarios and median interview completion is under 60 seconds;
- purpose is never requested when explicit timing semantics make it irrelevant;
- at least 50% of feasible recommendations are accepted;
- at least five of eight invited users create a second plan when another eligible trip occurs;
- median predicted saving is at least ten road minutes among accepted plans;
- at least 90% reported deadline success among followed plans with the selected cushion;
- median absolute per-leg forecast error is no more than ten minutes;
- median response is under eight seconds;
- p95 paid route estimates are at most 24 per successful plan;
- no exact location appears in analytics or logs.

These are proposed decisions, not existing results. Actual minimum-road-time saving remains counterfactual.

Pilot measurement definitions are frozen before invitations:

- **Eligible supported request:** a confirmed compute attempt inside the documented driving/date/location bounds. Schema-invalid or unsupported inputs are excluded; provider, search, timeout, and application failures remain in the denominator.
- **Accepted recommendation:** a `FEASIBLE` result the user views and explicitly chooses to follow.
- **Followed plan:** the user reports leaving within plus or minus five minutes of the last accepted recommendation. Material stops, route changes, or visit-duration changes are reported separately.
- **Deadline success denominator:** followed plans with a valid outcome check-in.
- **Forecast-error denominator:** legs with valid opt-in timing from which the browser submits only rounded durations/error offsets, not absolute timestamps.
- **Repeat-use denominator:** users who report a second eligible trip opportunity; also report the raw invited-user repeat rate.

Every pilot result publishes numerator, denominator, missing-data exclusions, and the forecast snapshot used for each scored leg.

## Surface roadmap

1. **Responsive web MVP:** complete planner, review, in-memory result, refresh, local input persistence, Maps handoff, and pilot analytics.
2. **PWA:** manifest, icons, service worker, install guidance, and an offline shell after the core online calculation is stable.
3. **Web retention features:** saved local templates, shareable constraint links without precise locations, and optional email/calendar reminder experiments.
4. **Android decision gate:** build native Android only if the pilot shows repeat use and users need reliable device reminders, share-sheet integration, widgets, or Assistant actions.
5. **Native expansion:** reuse the versioned planning API; add Android-specific capabilities incrementally.

## Product acceptance criteria

The private pilot can start when:

1. the full fake-provider journey works in the frozen Playwright Chromium, Firefox, and WebKit matrix; physical Android Chrome and iPhone Safari complete a manual journey and Maps handoff;
2. keyboard-only operation, visible focus, semantic labels, live loading/result announcements, focus recovery, 200% text, and WCAG 2.2 AA reflow at 320 CSS pixels work, with manual mobile and desktop screen-reader checks;
3. manual entry works without geolocation permission;
4. every user-supplied hard constraint is confirmed on the timeline, every default has visible provenance, and no paid route call occurs before the interview reaches `READY_TO_PLAN`;
5. the authored dialogue suite covers every supported intent, ambiguity branch, and multi-constraint combination with zero missed or invented hard constraints;
6. every recommendation satisfies readiness, access, activity, window, route, cushion, and deadline constraints under the returned estimates;
7. every round-trip candidate evaluates its return leg at the derived return departure time;
8. provider failures never fabricate a result;
9. a reload restores user-entered constraints and requires recalculation; provider-derived results are absent from IndexedDB, Cache Storage, browser URLs, and telemetry;
10. the Maps handoff works on Android Chrome and iPhone Safari, with a copy fallback;
11. all required error/recovery states pass acceptance tests;
12. provider credentials are absent from frontend bundles and browser network calls;
13. question ID/reason, answer/skip/edit, completion time, request shape, result category, and estimated cost are measurable without free text, purpose text, exact times, or location data;
14. copy contains no exact-arrival promise or unsupported confidence percentage;
15. `/privacy` and `/terms` are public, Google-derived results show required attribution, and Maps handoff discloses that selected endpoints go to Google and may enter browser or app history;
16. logout, session replacement, owner mismatch, pilot-data deletion, and the plan-expiry rule prevent another session from seeing local trip inputs.

See [web-architecture.md](web-architecture.md), [web-roadmap.md](web-roadmap.md), and [web-quality-operations.md](web-quality-operations.md).

