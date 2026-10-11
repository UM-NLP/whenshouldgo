# Full Trip Assistant Product Plan

Updated 2026-10-10. This is the long-term product direction. The existing adaptive trip interview and deterministic round-trip planner remain the first capability inside the assistant.

## Product promise

WhenShouldGo helps a user decide when to prepare, leave, visit, continue to the next stop, and return. It can start from a typed or spoken request, a place shared from Google Maps, an approved calendar event, or a saved routine. It explains the constraints it used, refreshes active plans when conditions materially change, and hands each navigation leg to Google Maps.

The assistant is persistent because scheduled events wake it. It is not continuously listening, tracking location, or running a model. Calendar change notifications, scheduled route checks, and user actions trigger bounded work.

## Target customers and jobs

The initial customer hypothesis is a GTA Android driver with at least two time-sensitive trips per week and some flexibility about departure time. Likely examples are parents, caregivers, hybrid workers, and people coordinating appointments and errands. This segment remains unvalidated.

The product grows through four jobs:

1. **Flexible local visit:** spend a required duration at one place and meet a destination or return deadline while reducing forecast road time.
2. **Calendar-aware day:** turn selected events into complete preparation, travel, visit, and return plans; warn when adjacent commitments cannot both be reached.
3. **Multi-stop day:** sequence fixed and flexible stops with dwell times, time windows, priorities, and a latest finish.
4. **Multi-day road trip:** divide a trip into safe daily stages around bookings, breaks, lodging, charging or fuel, weather, and time zones.

## Product principles

- The language model interprets, asks, and explains. Typed server tools and deterministic solvers calculate feasibility, schedules, and rankings.
- Every hard constraint has a source and confirmation state. Calendar titles, memories, and model guesses never silently become facts.
- Ask only questions that can change feasibility, safety margin, route choice, or the user's objective.
- A proactive update is a visible proposal with its reason and data age. It never silently rewrites a confirmed plan.
- Remember structured defaults only after the user chooses what to remember. Learned routine candidates require confirmation.
- Forecasts are estimates. The product never promises an exact or guaranteed arrival.
- Request calendar, notification, microphone, and location permissions only when the related feature is invoked.
- Keep Google Maps as the initial navigation surface. When a long itinerary exceeds a single handoff's waypoint limits, open one leg or day at a time.

## Core product object

All surfaces operate on one versioned `TripPlan`:

```text
TripPlan
  participants and timezone
  places and activities
  legs and travel modes
  hard constraints and soft preferences
  preparation, dwell, parking, walking and safety allowances
  objective and evaluated alternatives
  source/provenance and confirmation for every fact
  forecast timestamps and provider
  reminders and monitoring policy
  locked, completed and optional items
  current version and superseded versions
```

Round trips, multi-stop days, routines, and multi-day plans are planning strategies over this shared contract.

## User journeys

### One-off trip

The user says, â€œI need one hour at Costco and must be home by 6.â€ The assistant resolves the destination, asks only for missing origin/readiness and relevant allowances, confirms an editable timeline, computes feasible alternatives, and shows when to start preparing, leave, leave the destination, and expect to return. The user can set reminders or open the selected leg in Maps.

### Calendar event

The user selects **Plan travel for an event**, chooses which calendar to expose, and selects an event. The assistant confirms the event location, whether its time means arrival or activity start, preparation, early-arrival tolerance, and any return requirement. A calendar change marks the derived plan stale and proposes a refreshed version.

### Saved routine

After a useful plan the assistant offers **Remember this routine** and lets the user choose the retained fields. Each occurrence shows reused values and current exceptions. Editing one occurrence changes the routine only after a separate save action.

### Proactive change

An opted-in route refresh detects a material shift. A notification states what changed, when the data was fetched, and the proposed new action. The user can accept, snooze, refresh, dismiss, or open the previous plan.

### Multi-stop day

The user provides required and optional stops, dwell times, fixed or flexible order, and any opening or appointment windows. The planner returns a feasible ordered timeline, explains dropped optional stops and infeasible constraints, and replans only unfinished, unlocked stops after disruption.

### Multi-day road trip

The user builds the trip on web with dates, daily driving limit, rest policy, locked bookings, must-see and optional stops, lodging, and vehicle needs. The app partitions the trip into days. Android shows today's offline-readable plan and opens each navigation leg. Day-of changes preserve future locked commitments.

### Voice and Maps entry

Android accepts a Google Maps share, a home-screen shortcut, push-to-talk, or an App Action. Each produces candidate facts for the same adaptive interview. A true always-listening hotword experience is a later Android assistant-role project because the selected `VoiceInteractionService` has platform, battery, privacy, and distribution implications.

## Capability boundaries

The first releases exclude continuous background location, continuous microphone access, emergency response, commercial fleet dispatch, regulated driver-hours compliance, autonomous bookings or purchases, and exact-arrival claims. Calendar writes, messages, bookings, and any future paid action require a preview and explicit confirmation.

## Success metrics

Activation requires a valid plan plus a commitment action: save it, schedule a reminder, or launch navigation. The main product metric is successful assisted trips per retained user. Supporting measures are constraint accuracy, infeasibility detection, forecast error by horizon, recommendation adoption, recovery after a change, second planning day within 14 days, routine reuse, calendar-event conversion, notification mute rate, provider cost per activated plan, and contribution margin.

Actual time saved cannot be claimed from an unobserved counterfactual. The app reports forecast comparisons and observed outcomes separately.

## Validation sequence

The first experiment remains the smallest complete job: one flexible local visit, one return deadline, a required visit duration, a preparation allowance, a result timeline, Maps handoff, and voluntary outcome check-in. Calendar, agent chat, multi-stop, and road trips enter only after the preceding behavior and quality gate passes. This ordering validates value before adding sensitive access and recurring operating cost.


