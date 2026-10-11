# Agent-First Personal Trip Assistant

Updated 2026-10-10. This document replaces the earlier planner-centered product definition.

## Product promise

WhenShouldGo accompanies a user from an uncertain desire to a completed trip. It can start with â€œI want to eat something,â€ â€œfind something to do with the kids,â€ â€œcan I fit groceries in before pickup?â€, a known appointment, a Calendar event, a multi-stop day, or a multi-day road trip.

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

Unknown destination, activity, date or time is valid state. For example, destination may mean â€œan inexpensive vegetarian restaurant within 20 minutes,â€ and date may mean â€œone weekend in November.â€ The agent progressively narrows these domains instead of treating them as missing form fields.

Each fact records its value or range, source, confidence, confirmation state, whether it is hard or soft, scope, freshness and dependencies. Unconfirmed inferences may diversify exploration; they cannot become hard commitments.

## Experience

The main interface combines conversation with an editable Trip Board.

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

## Example: â€œI want to eat somethingâ€

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

## Success

The north-star candidate is **completed assisted travel goals per retained user**: the user accepted a grounded recommendation and took an execution action.

Measure goal success by initial vagueness, turns to first useful proposal, corrections, grounded-claim rate, hard-constraint confirmation, tool recovery, plan freshness, recommendation acceptance, execution action, second goal on another day, provider/model cost and user-rated control.

The assistant succeeds when it reduces decision effort and earns action and repeat use. A technically feasible route alone is not success.

## Product boundaries

The product is not a general-purpose life assistant, emergency service, commercial fleet system or autonomous purchaser. It does not guarantee arrival, silently change schedules, infer sensitive routines without consent, require continuous background location, or replace turn-by-turn navigation in early releases.


