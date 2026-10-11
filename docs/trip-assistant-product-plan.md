# Agent-First Personal Trip Assistant

Updated 2026-10-11. This document defines the current product.

## Product promise

WhenShouldGo accompanies a user from an uncertain desire to a completed trip. It can start with “I want to eat something,” “find something to do with the kids,” “can I fit groceries in before pickup?”, a known appointment, a Calendar event, a multi-stop day, or a multi-day road trip.

The assistant discovers intent, researches possibilities, recommends a small number of meaningful options, turns the selected option into an editable plan, monitors accepted commitments, and helps recover when conditions change. Routing, traffic, place search, Calendar, weather, reminders, reservations and schedule solvers are tools; none is the product by itself.

## User jobs

1. **Explore:** find an activity or destination from a vague goal, mood, available time, companions, budget, accessibility, dietary needs or location.
2. **Decide:** compare where, when and how to travel, including uncertainty and tradeoffs.
3. **Compose:** combine activities, travel, preparation, stays and commitments into a coherent option.
4. **Commit:** confirm material assumptions, create reminders and prepare navigation or other actions.
5. **Execute:** show the next useful action and current evidence freshness.
6. **Recover:** respond to delay, closure, weather, traffic, price, availability or a changed preference.
7. **Remember:** reuse user-approved places, preferences, companions, buffers and routines.
8. **Coordinate complex travel:** handle multi-stop days and multi-day itineraries after the local assistant proves repeat value.

## Primary artifact: Trip Workspace

The core object is a living workspace rather than a completed route:

```text
TripWorkspace
  goal hypotheses and desired outcomes
  travelers and mobility context
  confirmed facts, preferences and assumptions
  open decision variables
  candidate places and activities
  evidence with source and freshness
  candidate and selected plan versions
  permissions and pending actions
  monitoring policy
  cost ledger
```

Unknown destination, activity, date or time is valid state. For example, destination may mean “an inexpensive vegetarian restaurant within 20 minutes,” and date may mean “one weekend in November.” The agent progressively narrows these domains instead of treating them as missing form fields.

Each fact records its value or range, source, confidence, confirmation state, whether it is hard or soft, scope, freshness and dependencies. Unconfirmed inferences may diversify exploration; they cannot become hard commitments.

## Experience

The main interface combines conversation with an editable Trip Board.

Android voice input is push-to-talk: press and hold the in-app microphone button, release to submit, or cancel to discard. The microphone is otherwise off. Wake words, background listening and hardware-key interception are excluded from the MVP.

Conversation handles vague goals, comparison, explanation and changes. The Trip Board exposes the current goal, visible assumptions, confirmed constraints, candidates, timeline or day plan, evidence freshness, warnings, pending actions and monitoring controls.

Each assistant response should normally contain:

1. its current understanding;
2. a useful proposal, shortlist or next action;
3. the main reason and uncertainty;
4. at most one high-value question or a few action chips.

The assistant should produce value early. Vague and reversible requests should often receive a diverse shortlist before detailed questioning. It asks first when the missing answer affects safety, feasibility, accessibility, dietary needs, a fixed commitment, money or a large part of the search space.

## Lifecycle

```text
CAPTURED -> FRAMING -> EXPLORING -> PROPOSED -> CONFIRMED
CONFIRMED -> ARMED -> ACTIVE -> ADAPTING -> COMPLETED
```

Other final states are `CANCELLED`, `EXPIRED` and `UNSUPPORTED`. Expired or changed evidence invalidates dependent proposals. Approved and active plans are immutable versions; a change creates a proposal that explains what changed and why.

## Example: “I want to eat something”

1. Use the current area and likely time only with permission and show those assumptions.
2. Check whether a known hard need exists, such as an allergy, wheelchair access or a fixed return time.
3. If no blocker exists, run a cheap broad discovery search rather than starting an interview.
4. Fetch details only for promising candidates and route only finalists.
5. Present up to three materially different options: for example quickest, best preference match and lowest cost.
6. Show why each fits, travel/outing time, opening status, cost category, uncertainty, freshness and Maps source links.
7. Let the user choose, refine or change one assumption.
8. Build an actionable outing only after selection; validate any deadline or required duration with appropriate tools.

## Recommendation policy

Hard requirements filter candidates. Accessibility, dietary, visa, age and safety requirements cannot be averaged away. Rank remaining options on a Pareto frontier across travel time, total time away, cost, preference fit, schedule slack, uncertainty and group fairness.

For groups, protect the least-served member before maximizing average satisfaction. When preferences conflict, show a compromise plus contrasting alternatives. Sponsored or affiliate influence must be labelled and cannot be hidden in organic ranking.

Every option contains the reason it fits, its main tradeoff, ranges rather than false precision, unsupported or unknown facts, source/freshness and assumptions requiring confirmation.

## MVP wedge

The validating product is an **agent-assisted same-day local outing** for Toronto/GTA consumers.

It supports:

- a vague category or a named destination;
- one local destination with optional return;
- same-day driving;
- current or saved starting area;
- companions, simple preferences and hard needs;
- preparation, visit duration, arrive/home deadlines and allowances when relevant;
- grounded place discovery;
- one recommendation and up to two alternatives;
- an editable Trip Board;
- manual refresh, reminder proposal, outcome check-in and Google Maps handoff.

It excludes Calendar sync, inferred routines, continuous background location, autonomous booking/purchase, multi-stop and multi-day planning. These enter through evidence-gated phases.

## Access model

First use requires no visible login or personal information. The client silently establishes an anonymous tenant identity so the service can isolate data and control provider cost. A guest receives 50 successful user turns or 10 activated trips over the lifetime of that guest identity, whichever is reached first. The current response always finishes; the next metered action asks the person to create a free Google or email-link account.

Registration remains free. It preserves the current identity when possible and adds account recovery, cross-device sync, opted-in cloud history and, after their own release gates, Calendar, durable memory and bounded proactive monitoring. Guest use stays local-first and excludes those account capabilities. Existing plans remain viewable, exportable, deletable and usable for Maps handoff after the gate. The exact counting, retention and merge rules are defined in [access-tiers.md](access-tiers.md).

## Success

The north-star candidate is **completed assisted travel goals per retained user**: the user accepted a grounded recommendation and took an execution action.

Measure goal success by initial vagueness, turns to first useful proposal, corrections, grounded-claim rate, hard-constraint confirmation, tool recovery, plan freshness, recommendation acceptance, execution action, second goal on another day, guest-to-free-account conversion, provider/model cost by tier and user-rated control.

The assistant succeeds when it reduces decision effort and earns action and repeat use. A technically feasible route alone is not success.

## Conversation boundary

WhenShouldGo is a travel-planning product rather than a general chatbot. It may discover, compare, compose, execute and recover trips and outings, plus explain this app's controls, privacy, account and usage. Unrelated research, coding, schoolwork, creative writing and general advice receive a fixed redirection without planning tools. Mixed requests keep only their travel-planning portion. Server-side classification, capability allowlists and output validation enforce this boundary; the prompt is one layer only. Exact rules and token ceilings are in [voice-and-domain-controls.md](voice-and-domain-controls.md).

## Product boundaries

The product is not a general-purpose life assistant, emergency service, commercial fleet system or autonomous purchaser. It does not guarantee arrival, silently change schedules, infer sensitive routines without consent, require continuous background location, or replace turn-by-turn navigation in early releases.

