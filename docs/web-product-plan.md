# Web-first product plan

Status: approved direction on 2026-10-10. Planning is complete; implementation has not started.

## Product decision

Build the first product as a **mobile-first responsive web app**, then make it installable as a Progressive Web App (PWA) after the core journey works. This is the cheapest useful way to validate the scheduling method and customer demand while preserving a clear path to Android.

The web app can already deliver the complete value loop:

1. ask for every trip constraint;
2. calculate a round-trip schedule with server-side traffic data;
3. show when to start preparing and leave;
4. refresh the forecast;
5. open each leg in Google Maps.

One responsive codebase works on Android, iPhone, and desktop browsers. It avoids Play review, native SDK work, separate mobile releases, and native reminder complexity during the pre-seed validation stage. A web manifest and service worker can later make it installable, but installation is progressive enhancement; every core function must work in an ordinary mobile browser. See [PWA quality guidance](https://web.dev/articles/pwa-checklist) and [PWA installation](https://web.dev/learn/pwa/installation).

The first release will not modify the consumer Google Maps interface. It will accept normal inputs, optionally accept a pasted/shared Maps link after compatibility testing, and hand outbound and return navigation to Google Maps URLs. Trip data never enters this application's URLs; an explicit handoff sends the selected endpoints to Google and can place them in browser or Maps history.

## User promise

> Find the best forecasted time to leave for a round trip, spend the time you need there, and plan to return by your deadline.

The interface uses **best forecasted option checked** or **best evaluated departure**. It never says exact, guaranteed, or always on time. Traffic forecasts can change.

## Evidence and assumptions

Verified:

- The earlier one-way experiment recorded an 11.53-minute forecast revision on one test and did not establish a reliable exact-departure method. See [experiment-results.md](experiment-results.md).
- Google Routes can return traffic-aware durations for specified departure times; these remain forecasts. See [traffic models](https://developers.google.com/maps/documentation/routes/traffic-model).
- Google Maps URLs can open directions or navigation from a web application and do not require a Maps URL API key. See [Maps URLs](https://developers.google.com/maps/documentation/urls/get-started).
- Browser geolocation requires HTTPS and user permission. Manual location entry must remain available. See [Geolocation API](https://developer.mozilla.org/en-US/docs/Web/API/Geolocation_API).

Hypotheses:

- Initial customer: GTA drivers with a flexible same-day errand or visit and a firm return obligation.
- Parents and caregivers are the first segment to test because a pickup or return deadline makes the cost concrete.
- Users are willing to delay leaving when the predicted road-time saving is meaningful.
- The combined outbound, visit, and return decision is valuable often enough to create repeat use.

There is no verified demand, willingness-to-pay, retention, or market-size evidence yet.

## Job to be done

> When I have a flexible visit today but must be home by a firm time, help me decide when to start getting ready and when to leave so I spend as little forecasted time driving as possible, complete my visit, and keep a safety margin for returning.

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
- an exact visit duration;
- manual address entry and optional foreground current location;
- an optional acceptable visit window;
- preparation, parking/walking, and safety allowances;
- toll, highway, and ferry preferences;
- one routing provider for all candidates in a plan;
- a guided form, editable review, recommendation, and two useful alternatives;
- manual refresh and freshness warnings;
- separate Google Maps handoffs for outbound and return legs;
- local browser storage for user-entered drafts and permitted location identifiers; route results remain in memory;
- private invite access and privacy-safe pilot analytics.

The pilot excludes accounts, payment, subscriptions, native push reminders, continuous background work, future-day monitoring, multiple stops, different return locations, other travel modes, an LLM, a widget, voice assistants, embedded navigation, and a native Android build.

## Inputs

The app asks one question at a time and shows all answers on an editable review screen.

| Input | Meaning |
|---|---|
| Start and return place | Confirmed address, browser location, or pin; both are the same in the MVP. |
| Destination | Confirmed address or pin. |
| Trip date and timezone | Today in the trip's displayed IANA zone. |
| Earliest preparation start | Earliest time the user can begin getting ready; default is now. |
| Preparation duration | Uninterrupted time needed before leaving home. |
| Walk to vehicle | Time between leaving home and starting the outbound route. |
| Walk/park in | Time between road arrival and visit start. |
| Time at destination | Exact visit duration. |
| Walk/park out | Time between visit end and return-drive start. |
| Walk from vehicle home | Time between road arrival and being home. |
| Back by | Latest acceptable time at home. |
| Return cushion | Margin subtracted from the back-by deadline; offer 0, 10, 20, and custom minutes. |
| Visit window | Optional earliest visit start and latest visit end. |
| Route preferences | Avoid tolls, highways, and ferries. |
| Maximum wait | Optional maximum time the user is willing to delay leaving to save road time. Test this in the pilot. |

## Core journey

1. **Open link:** Show the value, forecast limitation, and **Plan a trip**. No permission prompt.
2. **Enter trip:** Ask structured questions with address fields, duration controls, date/time controls, and preference chips.
3. **Confirm places:** Resolve the full address or pin. Request browser location only after **Use my location**.
4. **Review:** Show every input, timezone, default, and derived earliest departure. Edits invalidate old results.
5. **Calculate:** Preserve inputs while the server checks traffic in both directions. Do not show a fake percentage.
6. **Recommend:** Lead with **Leave at [time]** and **Best forecasted option we checked**.
7. **Act:** Keep the input template locally, refresh, copy a privacy-safe summary, or explicitly open the outbound Maps URL.
8. **Return:** Offer **Leaving now** to obtain a current return ETA and warn about deadline risk.
9. **Learn:** Ask for a voluntary outcome and reuse response without collecting a GPS trace.

The result shows start preparing, leave home, start driving, destination road arrival, visit start/end, return drive start, home arrival, deadline slack, both leg durations, total road time, predicted saving against the evaluated leave-when-ready baseline, two alternatives, provider, and calculation time.

## Required states

| State | Required recovery |
|---|---|
| Empty | Ask for destination and show an example without pre-filling private data. |
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

- 5–8 invited users;
- at least 20 real plans over roughly two weeks;
- the working web app performs the calculation;
- the founder observes failures, interviews users, and manually fixes input or support problems;
- no paid ads, app-store launch, payment system, native notifications, or full account system.

This retains the learning value of concierge testing while producing reusable software.

## Validation gates

### Problem check

Conduct 8–12 interviews while the thin MVP is being built. A proposed gate is at least six people reporting two or more eligible trips per month and a concrete cost from mistiming. Ask directly whether they would wait 30–60 minutes to save 10–20 road minutes.

### Assisted web pilot

Proposed gates, to be agreed before collecting results:

- at least 20 completed real-plan attempts across at least five users;
- at least 90% of supported requests produce a valid plan or a correct no-plan explanation;
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
4. every recommendation satisfies preparation, access, visit, window, route, cushion, and deadline constraints under the returned estimates;
5. every candidate evaluates its return leg at the derived return departure time;
6. provider failures never fabricate a result;
7. a reload restores user-entered constraints and requires recalculation; provider-derived results are absent from IndexedDB, Cache Storage, browser URLs, and telemetry;
8. the Maps handoff works on Android Chrome and iPhone Safari, with a copy fallback;
9. all required error/recovery states pass acceptance tests;
10. provider credentials are absent from frontend bundles and browser network calls;
11. request counts, latency, result category, and estimated cost are measurable without location data;
12. copy contains no exact-arrival promise or unsupported confidence percentage;
13. `/privacy` and `/terms` are public, Google-derived results show required attribution, and Maps handoff discloses that selected endpoints go to Google and may enter browser or app history;
14. logout, session replacement, owner mismatch, pilot-data deletion, and the 24-hour post-`homeBy` expiry prevent another session from seeing local trip inputs.

See [web-architecture.md](web-architecture.md), [web-roadmap.md](web-roadmap.md), and [web-quality-operations.md](web-quality-operations.md).

