# WhenShouldGo

WhenShouldGo is a planned personal trip assistant. Its long-term job is to turn a conversation, shared map place, approved calendar event, or saved routine into an explainable travel plan; handle one-stop, multi-stop, and multi-day trips; and send opt-in reminders when preparation or departure should change.

**Current status: full product direction is documented; no application is deployed.** The cheapest validation remains a mobile-first responsive web app for one flexible local visit. Android is the first intended product after that gate because reminders, Maps sharing, widgets, and voice entry are native capabilities. The language model will interpret and explain; deterministic server tools remain responsible for constraints, feasibility, route calculations, and side effects.

The earlier one-way â€œarrive byâ€ experiment is preserved but On Hold. Its precise zero-buffer recommendations did not remain feasible in later live-estimate checks. No provider is established best, and the product will not promise an exact or guaranteed arrival.

- [Web product plan](docs/web-product-plan.md)
- [Web architecture](docs/web-architecture.md)
- [Delivery roadmap](docs/web-roadmap.md)
- [Quality, security and operations](docs/web-quality-operations.md)
- [Full trip assistant product](docs/trip-assistant-product-plan.md)
- [Full trip assistant architecture](docs/trip-assistant-architecture.md)
- [Model and hosting decision](docs/model-hosting-decision.md)
- [Assistant privacy and security](docs/trip-assistant-security.md)
- [Full assistant roadmap](docs/trip-assistant-roadmap.md)
- [Backlog](docs/backlog.md)
- [Project board](docs/project-board.md)
- [Decision history](docs/decisions.md)
- [Historical experiment results](docs/experiment-results.md)

The existing scripts in `tools/` are historical research code until they are frozen and moved under `experiments/arrival_deadline/`. Keys, private trip inputs, raw responses and geometry stay outside Git in `.env.local` and `.local/`; explicit cloud/Docker ignore files also keep them out of build contexts. The web MVP persists input drafts locally but keeps provider-derived route results in memory. Provider calls can incur charges; every live stage must enforce atomic quotas and verify actual billing.

