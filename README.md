# WhenShouldGo

An experiment to recommend when a driver should leave to reach a destination by a chosen deadline.

**Status: incomplete and unvalidated.** Early zero-buffer recommendations failed the later live-estimate checks. A precise departure time does not guarantee an on-time arrival. No actual journeys were measured and no provider is established best.

The prototype searches Google departure forecasts, compares TomTom arrive-by recommendations and schedules fresh API estimates at departure. Full app development and platform choice are deferred.

- [Method and recorded results](docs/experiment-results.md)
- [Reproduction and offline checks](docs/reproduce-experiment.md)
- [Project board](docs/project-board.md)
- [Backlog](docs/backlog.md)
- [Decision history](docs/decisions.md)

Python scripts and isolated JavaScript calculation checks are in tools/. Keys, private trip inputs, raw responses and geometry stay outside Git in .env.local and .local/. Provider calls can incur charges; billing remains unverified.

