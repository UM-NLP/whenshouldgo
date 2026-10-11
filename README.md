# WhenShouldGo

WhenShouldGo is a pre-seed product experiment for same-day driving plans with different kinds of deadlines. A mobile-first adaptive interview identifies what the user must arrive, start, finish, leave, or return home by, asks only questions that can change feasibility or the recommendation, and compares forecasted departure windows to reduce total predicted road time.

**Current status: web-first plan approved and revised for adaptive intake; no application is deployed.** The first product will be a mobile-first responsive web app with a deterministic interview policy, deterministic planner, server-side traffic provider, and separate Google Maps handoffs. A PWA and native Android app are gated by pilot evidence.

The earlier one-way “arrive by” experiment is preserved but On Hold. Its precise zero-buffer recommendations did not remain feasible in later live-estimate checks. No provider is established best, and the product will not promise an exact or guaranteed arrival.

- [Web product plan](docs/web-product-plan.md)
- [Web architecture](docs/web-architecture.md)
- [Delivery roadmap](docs/web-roadmap.md)
- [Quality, security and operations](docs/web-quality-operations.md)
- [Backlog](docs/backlog.md)
- [Project board](docs/project-board.md)
- [Decision history](docs/decisions.md)
- [Historical experiment results](docs/experiment-results.md)

The existing scripts in `tools/` are historical research code until they are frozen and moved under `experiments/arrival_deadline/`. Keys, private trip inputs, raw responses and geometry stay outside Git in `.env.local` and `.local/`; explicit cloud/Docker ignore files also keep them out of build contexts. The web MVP persists input drafts locally but keeps provider-derived route results in memory. Provider calls can incur charges; every live stage must enforce atomic quotas and verify actual billing.

