# Evaluation agent

Use this role for tests, fixtures, telemetry schemas, and release checks (#21).

Read `AGENTS.md` and `docs/agent-evaluation-plan.md`. Evaluate an end-to-end travel
goal, not only a model response. A scenario holds the initial context, latent goal,
gold hard constraints, expected patches, a set-valued acceptable next-action oracle,
prohibited questions and tools, simulated user answers, tool fixtures (freshness,
failures, injected text) and required confirmation points.

Test policy boundaries end to end: travel classification, mixed requests, quota
races, malformed or truncated proposals, prompt injection in tool data,
hard-constraint preservation, and confirmation gates (altered, expired or replayed
approvals). Use independent arithmetic, temporal and budget checks for feasibility.

Quality gates to report against: zero unconfirmed hard constraints in a committed
plan, zero injection-driven tool or state violations, zero unauthorized actions,
zero state change from `MAX_TOKENS` or malformed output, zero general answers and
zero planning-tool calls on rejected requests, at least 99% routing accuracy with
under 1% false refusals.

Unit and boundary tests live in `backend/tests/`; scenario fixtures go in
`backend/tests/eval/`. Traces store prompt, model and tool versions, transitions,
evidence IDs, tokens, latency and cost, never raw locations, utterances or Maps
content. A known gap is recorded as a strict `xfail` with the backlog item that
fixes it.

Tests are deterministic and offline: no network, no provider credentials. Fixtures
are synthetic and free of precise locations or user data. Every release report
separates measured results, assumptions and unexecuted checks.
