# Experiment results â€” 8 October 2026

Status: incomplete research prototype. The tested zero-buffer, early-forecast method did not retain an on-time estimated arrival when checked at departure. No actual journeys were measured; these results measure forecast revision, not observed travel accuracy.

Target customer: a recurring Greater Toronto Area driver who must reach work by a fixed deadline. Proposed value: a clear departure recommendation that updates as traffic changes. Willingness to pay and dependable arrival performance remain unvalidated. The Android app, Maps extension and chatbot are deferred until the method passes evaluation.

## Method tested

For Google, request departure-specific traffic forecasts on a five-minute grid, refine the last feasible coarse interval at one-minute resolution, and choose the latest evaluated departure for which departure + predicted duration + buffer <= deadline. This is a bounded search; narrow feasible windows outside the refined interval may be missed. For TomTom, request a traffic-aware arriveAt route. Use identical user-confirmed endpoints, request toll avoidance and store provider-selected geometry locally. Forecast both providers at both nominated departures, then request fresh estimates near-simultaneously at each departure.

The recommendation is precise in format but uncertain in outcome. Neither API result guarantees an arrival time. Routes can differ across providers or requests; geometry was retained but equivalence has not been established. The test does not automatically read the Google Maps application.

## Recorded evidence

All times below are Toronto EDT (UTCâˆ’04:00). Deadline: 09:30. Buffer: zero. Private endpoints, raw responses and geometry are excluded from Git.

| Date | Provider | Check time | Early duration (min) | Live duration (min) | Revision (min) | Live estimated arrival |
|---|---|---|---:|---:|---:|---|
| Oct 7 | Google | 08:29:00 | 60.80 | 72.33 | +11.53 | 09:41:20 |
| Oct 8 | Google | 08:32:00 | 57.35 | 68.62 | +11.27 | 09:40:37 |
| Oct 8 | TomTom | 08:32:00 | 53.93 | 66.12 | +12.18 | 09:38:07 |
| Oct 8 | Google | 08:36:35 | 56.73 | 69.05 | +12.32 | 09:45:38 |
| Oct 8 | TomTom | 08:36:35 | 53.43 | 67.78 | +14.35 | 09:44:22 |

Oct 7 recommendation: Google 08:29, estimated arrival 09:29:48; 47 planning requests. Oct 8 recommendations: Google 08:32, estimated arrival 09:29:21; TomTom 08:36:35, estimated arrival 09:30:01. The implementation accepts one second of tolerance for the TomTom arrival response; this is a known issue for strict deadlines.

Oct 8 watcher made two Google and two TomTom requests. Request starts were within 0.037 seconds of target. Observed request latency was 197â€“565 ms. Actual billed cost was not verified. These are two days on one corridor, not independent population samples. No provider is established best.

## Next experiment and acceptance criteria

Freeze an evaluation protocol before more tests. Compare the current baseline with conservative forecasts, explicit buffers and morning rechecks on at least ten distinct weekdays. Separate night-before and morning lead times and compare both providers at the same check times. Record route changes and failed requests separately. Proposed feasibility gate, subject to user agreement: at least 90% of departure-time live estimates meet the deadline and median added early-arrival allowance is at most 15 minutes. This gate measures forecast stability only.

Calibrate any margin on training days and evaluate it on held-out days; the observed +11â€“14 minutes is not a justified universal buffer. Actual arrival reliability requires measured journeys or independent observed route travel times. Keep that validation unresolved under the current no-driving preference. Define cost per planning session from billing before expanding. No paid campaign or full app build is planned.

Evidence provenance: user-executed local forecasts and scheduled checks, including the cumulative Oct 8 benchmark-live JSON. Raw evidence stays in ignored .local/. Numeric values above are sanitized derived summaries.

