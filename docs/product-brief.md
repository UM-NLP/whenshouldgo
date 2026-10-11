# Arrival-deadline commute planner — draft

This document is a historical draft for the one-way feature, which is On Hold. The active scope is the [web-first round-trip product plan](web-product-plan.md).

## User intent
Enter an arrival deadline and receive a recommended departure time that accounts for travel time changing with departure. Example supplied by user: leaving at 08:00 yields 90 minutes of travel and arrival at 09:30; leaving at 07:45 yields 75 minutes and arrival at 09:00. These are illustrative values, not measured traffic.

## Proposed customer and value — hypotheses
Start with drivers commuting to a fixed workplace in one city. Reduce daily planning effort and late arrivals without routinely leaving excessively early. City, customer segment, willingness to pay and notification needs await user confirmation.

## Evidence
Google Maps already supports Depart at / Arrive by: https://support.google.com/maps/answer/144339
Google Routes supports departure-time-specific traffic forecasts, with historical traffic more influential farther ahead: https://developers.google.com/maps/documentation/routes/config_trade_offs
Google Routes arrivalTime is ignored for driving: https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes
No live route calls, accuracy measurements, latency measurements or cost measurements have been executed. Provider choice, pricing, usage terms and regional coverage remain to evaluate.

## Proposed smallest useful journey
Input origin, destination, arrival date/time and an optional parking/walking/safety allowance. Compare candidate departure times using a traffic provider; return the latest evaluated time forecast to reach by the deadline after allowances. Show estimated arrival, allowance and last update; allow refresh and navigation handoff. Recalculate travel duration for every candidate; do not subtract a single fixed duration. Handle ambiguous addresses, no route, API failure, loading and deadlines that appear unreachable. Start with driving and one destination. A later reminder pilot requires a deployed scheduler and permission to notify.

## Product boundary
Give an actionable recommendation while explaining that arrival is estimated. Do not guarantee exact arrivals or display an on-time probability until calibrated against representative observations. A language model may parse requests later; route estimates must come from traffic data. Better accuracy than Google Maps is an unverified hypothesis, not a promise.

## Proposed validation
Recruit 10 consenting commuters in the selected city for 2 weeks; recruitment is a proposed user action, not executed contact. Record actual departure/arrival, predicted arrival, selected allowance, and planning effort; compare against their existing planning method. Proposed targets: 90% on-time for trips following the recommendation, median absolute arrival prediction error <=10 minutes, and >=6/10 wanting continued use. Also measure early-arrival minutes, repeat use, route API calls/cost per plan and response latency. Report sample limitations and do not infer 90% confidence from a chosen buffer.

## Next decisions
Choose launch city/country, confirm driving-only scope, choose one-off planning versus recurring commute alerts, and agree prototype budget.

