# FRAME model bake-off

Measures how well a candidate model does the FRAME call (user message to a `FrameProposal`:
intent, typed `search_places` arguments and soft facts) **through our real gateway**, and then
scores **what production would do with the reply**, so the owner can pick a model. The model never sees Maps content in Phase 0: FRAME inputs are only the user's message and
user-derived facts.

Files:

| File | Purpose |
|---|---|
| `fixtures.jsonl` | 150 synthetic, hand-written cases with gold labels |
| `fixtures_schema.json`, `fixtures.py` | format, validator and loader (`python -m evals.frame_bakeoff.fixtures`) |
| `run.py` | CLI harness: real stack, one result line per (fixture, repeat) |
| `dryrun.py` | scripted fake model so the harness runs offline (`--dry-run`) |
| `score.py` | report, PASS/FAIL against proposed bars, multi-model comparison |
| `results/` | your output (git-ignored; never commit) |

Run everything from `backend/`.

## Safety rules

- **Synthetic data only** (docs/agent-evaluation-plan.md, "Evaluation data rule"). Fixtures hold no
  real person, address, phone number or Maps content; the validator rejects phone numbers, emails
  and URLs outside reserved `.invalid`/`.example` hosts. Do not add recorded Maps results.
- **Spend caps.** Always pass `--max-spend-usd` (and `--max-calls`) on a live run. The cap is checked
  before every call and aborts the run (exit 3) once cumulative cost reaches it, counting what a
  resumed file already cost. With `--concurrency N` in-flight calls can overshoot by up to N calls.
- **Never commit results.** `backend/evals/frame_bakeoff/results/` is git-ignored; keep result files
  there or outside the repo. A result line holds the fixture id, metrics, the model's search fields
  and fact keys/flags; never the prompt, the message text, fact values or the key.
- **The API key** comes from `OPENROUTER_API_KEY` or `--env-file PATH` (only that variable is read).
  It is never printed or written; the run refuses to start without it. `--dry-run` needs no key.
- Not a gate: the PASS/FAIL bars in `score.py` are **proposals** for the owner to accept or change.

## 1. Dry run (offline, no key, no network)

```
python -m evals.frame_bakeoff.run --dry-run --out evals/frame_bakeoff/results/dry.jsonl
python -m evals.frame_bakeoff.score evals/frame_bakeoff/results/dry.jsonl
```

The scripted model follows the gold labels with deterministic faults (a malformed first reply, a
truncation, a flipped intent) and two planted failures (`inj-001` confirmed hard fact, `edge-006`
duration as area). It proves the harness and scorer work; it says nothing about any real model.

## 2. One-fixture smoke test (live)

```
set OPENROUTER_API_KEY=...           (PowerShell: $env:OPENROUTER_API_KEY="...")
python -m evals.frame_bakeoff.run --model vendor/model-name --provider slug ^
  --price-in 0.09 --price-out 1.10 --limit 1 --repeats 1 --max-spend-usd 0.05 ^
  --out evals/frame_bakeoff/results/smoke.jsonl
```

Use the model id, provider slug and USD-per-million prices from the OpenRouter page of the exact
(model, provider) pair; they must be pinned and the provider lower case (the gateway refuses
routers and floating ids). Behind a corporate TLS inspection proxy add
`--ssl-mode os-store-nonstrict`: it uses the OS trust store with only the X509 *strict* flag
relaxed; certificate chain and hostname verification stay on. The default `strict` uses the
gateway's own client untouched.

## 3. Full run

```
python -m evals.frame_bakeoff.run --model vendor/model-name --provider slug ^
  --price-in 0.09 --price-out 1.10 --repeats 3 --max-spend-usd 1.00 --max-calls 600 ^
  --out evals/frame_bakeoff/results/vendor-model-name.jsonl
python -m evals.frame_bakeoff.score evals/frame_bakeoff/results/*.jsonl
```

Useful flags: `--categories injection,edge`, `--limit N` (seeded selection, `--seed`),
`--concurrency 2`, `--resume` (skip pairs already in `--out`; a run killed mid-write is safe to
resume), `--cad-rate 1.43`, `--report-every 25`. Ctrl-C flushes results and prints the totals
(exit 130). One results file per model; passing several to `score` adds a comparison table and the
10 worst failures per model.

## Production replay

Scoring the model output alone hid a design flaw (the model set status CONFIRMED on most calls and
production would have rejected it). Every OK result is therefore replayed offline, inside the
harness, with the real functions and a fresh `TripWorkspace`, in the pipeline's order:
`plan_search` (discovery only; an OTHER reply's search is ignored), then `build_patch` and
`apply_patch_checked`. The record gets `plan`, `patch` (applied or rejected with reason,
`facts_dropped`, fact **keys** only), `requirements` (required and forbidden words, area checks on
the replayed search arguments) and a derived `turn_outcome`:

| turn_outcome | production does |
|---|---|
| `DISCOVERY_SEARCH` | plan accepted and patch applied: calls the search with those arguments |
| `CLARIFY_OTHER` | intent OTHER, patch applied: fixed reply |
| `CLARIFY_QUERY_REJECTED` | discovery proposed but the query failed (or no search): fixed clarification |
| `PATCH_REJECTED` | the patch validator refused the proposal |
| `MALFORMED` | the gateway returned a content or structure failure (a 502 in production), incl. unknown keys |
| `PROVIDER_ERROR` | timeout, provider error or other infrastructure failure (no content) |

FRAME has no repair call (`allow_repair=False`), so `repairs` is always 0 and a malformed or
truncated reply costs one call and is MALFORMED.

## Cost math

150 fixtures x 3 repeats = 450 calls. After the redesign a FRAME request is about 250 input tokens
(system prompt, state message, JSON schema and the message; long fixture `edge-001` is about
1,700) and about 55 output tokens; the run before the redesign measured 247 in / 98 out. Cost per
call is `(in_tokens x price_in + out_tokens x price_out) / 1e6`.

Example at US$0.09 in / US$1.10 out per million: 450 x (250 x 0.09 + 55 x 1.10) / 1e6 = about
US$0.037 (CA$0.05 at 1.43). Even at US$1 / US$5 per million the whole run is about US$0.23. The
report's "cost per 1,000 FRAME turns" is FRAME only: a real turn also pays the DISCOVER call.
Failed calls are charged from the gateway error's usage (or the worst-case reservation when none is
reported), so a failure never looks free.

## What the report measures (all bars are PROPOSALS)

- **Production turn success**: share of calls whose `turn_outcome` is `DISCOVERY_SEARCH` for a gold
  DISCOVERY fixture or `CLARIFY_OTHER` for a gold OTHER one. **Degraded**: a gold DISCOVERY fixture
  answered by a fixed clarification (a missed search). **Wrong action**: a gold OTHER fixture where
  production tries to search. **Hard failures**: MALFORMED + PATCH_REJECTED (proposed bar <= 0.5%).
- Parseable proposal in one call; intent accuracy (all and non-English); argument faithfulness
  (discovery cases: plan accepted, required words present, no forbidden word in the replayed query,
  no invented or unwanted area that reaches the tool arguments); language accuracy on the
  server-normalised value; truncation, latency p50/p95/max (nearest rank), mean and total cost,
  cost per 1,000 turns.
- **Two kinds of misbehaviour are kept apart.** (a) *Production-effective violations* are what
  production would actually do wrong: a search executed for a gold-OTHER fixture, a query with a
  forbidden word that passed `plan_search`, an invented or unwanted area that survived
  `plan_search`, a rejected patch (and a confirmed/hard fact, if it ever became expressible).
  (b) *Neutralised model slips* are bad output the server absorbed: a search attached to an OTHER
  intent (ignored), an invented or unwanted area that `plan_search` dropped, a forbidden word in a
  query that was rejected, facts dropped by the patch validator. **The injection-rejection and
  faithfulness bars use (a) only.** (b) is reported on its own line with counts per fixture; it
  indicates prompt/model quality but had no production effect. The old combined number is kept as
  an informational "raw model slips" row, and the worst-failures list labels both kinds. A call
  that returned nothing counts as rejected and is reported separately.
- A confusion matrix of gold action against `turn_outcome`, per-category and overall tables, the 10
  worst failures and, across several results files, a comparison table with the new metrics.
- A failed call counts as a miss for every rate it belongs to. The report keeps the rule-of-three
  note: a 100% rate over N only bounds the true failure rate near 3/N.
- A user-written word that is also forbidden (for example `password` inside the injection text
  itself) passes `plan_search`'s overlap rule; the replay shows it as a search with a forbidden word.

## Known caveats

- `served_provider` is the pinned provider recorded by the gateway; the raw provider string from the
  response is not exposed on `ModelTurn`.
- Unspaced CJK (Mandarin fixtures `multi-020` to `multi-022`): `plan_search` tokenises on `\w+`, so
  a whole Chinese sentence is one word and the query and area checks fail for any model. They are
  kept, but reported on a separate "known product gap" line and excluded from every rate and from
  the model comparison so they do not distort it.
- `expected_language` is only `en` or `fr` (the server normalises everything else to `en`).
- Gold labels are one reasonable reading; ambiguous cases carry a note. Review disagreements before
  trusting a rate near a bar, and remember N is small (see the rule-of-three line in the report).
