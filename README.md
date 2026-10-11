# WhenShouldGo

WhenShouldGo is a planned agent-first personal trip assistant. It can begin with a vague desire, named destination, shared place, Calendar event or active disruption; discover and compare possibilities; compose an editable trip; coordinate tools; monitor accepted commitments; and help the user recover when conditions change.

**Current status: the product was redesigned around a Trip Workspace and evidence-grounded agent; no application is deployed.** The cheapest validation is a mobile web agent for vague or named same-day local outings. Android is the first intended product after that gate because reminders, Maps sharing, widgets, offline state and voice entry are native capabilities. Typed tools provide discovery, routes, Calendar, weather, memory and actions; optional solvers validate hard schedules only when a goal needs them.

The validation product starts without a login screen. A silent anonymous identity gives each guest 50 successful user turns or 10 activated trips, whichever comes first; a free account is then required for further computation and adds recovery, sync and later account-only capabilities. These allowances are configurable pilot hypotheses. See [access tiers](docs/access-tiers.md).

The earlier one-way “arrive by” experiment is preserved but On Hold. Its precise zero-buffer recommendations did not remain feasible in later live-estimate checks. No provider is established best, and the product will not promise an exact or guaranteed arrival.

- [Web product plan](docs/web-product-plan.md)
- [Web architecture](docs/web-architecture.md)
- [Delivery roadmap](docs/web-roadmap.md)
- [Quality, security and operations](docs/web-quality-operations.md)
- [Full trip assistant product](docs/trip-assistant-product-plan.md)
- [Full trip assistant architecture](docs/trip-assistant-architecture.md)
- [Anonymous access and free account tiers](docs/access-tiers.md)
- [Conversation and decision policy](docs/agent-conversation-policy.md)
- [Agent evaluation plan](docs/agent-evaluation-plan.md)
- [Model and hosting decision](docs/model-hosting-decision.md)
- [Assistant privacy and security](docs/trip-assistant-security.md)
- [Full assistant roadmap](docs/trip-assistant-roadmap.md)
- [Backlog](docs/backlog.md)
- [Project board](docs/project-board.md)
- [Decision history](docs/decisions.md)
- [Historical experiment results](docs/experiment-results.md)

The existing scripts in `tools/` are historical research code until they are frozen and moved under `experiments/arrival_deadline/`. Keys, private trip inputs, raw responses and geometry stay outside Git in `.env.local` and `.local/`; explicit cloud/Docker ignore files also keep them out of build contexts. The web MVP persists input drafts locally but keeps provider-derived route results in memory. Provider calls can incur charges; every live stage must enforce atomic quotas and verify actual billing.

