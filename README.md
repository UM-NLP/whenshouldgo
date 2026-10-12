# WhenShouldGo

WhenShouldGo is an agent-first personal trip assistant. It can start from a vague desire, a named destination, a shared place, a Calendar event, or a disruption; discover and compare options; build an editable trip; coordinate travel tools; and help the user recover when conditions change.

**Current status:** architecture and validation planning are complete. No application is deployed. The first validation release is a low-cost mobile web agent for same-day local outings. Android follows only after the web pilot proves repeat value.

The service starts without a visible login. A silent guest identity receives 50 successful user turns or 10 activated trips, whichever comes first. Continued computation requires a free account. These limits are configurable validation hypotheses.

The assistant is limited to trip and outing planning plus app help. The server owns domain classification, tool permissions, quotas, state transitions, and action approval. Android voice starts with an in-app press-and-hold button; there is no background microphone or hardware-key interception.

## Current documentation

- [Product plan](docs/trip-assistant-product-plan.md)
- [Architecture](docs/trip-assistant-architecture.md)
- [Conversation and decision policy](docs/agent-conversation-policy.md)
- [Push-to-talk, travel scope, and token controls](docs/voice-and-domain-controls.md)
- [Anonymous access and free accounts](docs/access-tiers.md)
- [Model and hosting decision](docs/model-hosting-decision.md)
- [Privacy and security](docs/trip-assistant-security.md)
- [Evaluation plan](docs/agent-evaluation-plan.md)
- [Delivery roadmap](docs/trip-assistant-roadmap.md)
- [Active backlog](docs/backlog.md)
- [Project board snapshot](docs/project-board.md)
- [Active decisions](docs/decisions.md)

Credentials, private trip data, provider responses, precise locations, and route geometry must remain outside Git. No client may call model or travel providers directly; the backend enforces identity, authorization, quotas, token budgets, and provider kill switches.

## Development workspace

The repository now includes a Phase 0 foundation in `app/`: a FastAPI modular
monolith with a deterministic travel-domain gate, token limits, in-memory
development quota ledger, typed workspace state, and the start of the agent turn
orchestrator. It intentionally has no live model, Firebase, Maps, or provider
credentials; those integrations must preserve the server-side controls described
in the documents above.

Install the development dependencies and run the test suite:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

Start the local API with:

```powershell
python -m uvicorn app.main:app --reload
```

Codex role briefs live under `.codex/agents/`. `AGENTS.md` is the shared source
of engineering instructions and directs every coding task to the relevant product
and safety specifications.
