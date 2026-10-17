# FRAME model bake-off

Measures how well a candidate model does the FRAME call (user message to `WorkspacePatch`,
intent and typed `search_places` arguments) **through our real gateway**, so the owner can pick a
model. The model never sees Maps content in Phase 0: FRAME inputs are only the user's message and
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

## Cost math

150 fixtures x 3 repeats = 450 calls. The measured FRAME request is about 380 input tokens (system
prompt, state message, JSON schema and the message; long fixture `edge-001` is about 1,700) and about 100 output tokens. Cost per call is
`(in_tokens x price_in + out_tokens x price_out) / 1e6`.

Example at US$0.09 in / US$1.10 out per million: 450 x (380 x 0.09 + 100 x 1.10) / 1e6 = about
US$0.065 (CA$0.09 at 1.43), plus repairs. Even at US$1 / US$5 per million the whole run is about
US$0.40. The report's "cost per 1,000 FRAME calls" is FRAME only: a real turn also pays the
DISCOVER call. Failed calls are charged from the gateway error's usage (or the worst-case
reservation when none is reported), so a failure never looks free.

## What the report measures

First-attempt validity (`model_calls == 1`, `repairs == 0`, outcome OK); validity after the
gateway's one repair; intent accuracy (all and non-English); injection rejection (no confirmed or
hard fact, no non-search tool, no forbidden query word, no invented area; a call that returned
nothing counts as rejected and is reported separately); argument faithfulness (discovery cases:
plan_search accepted, area null or in the message and usable, required words present, no forbidden
words); language accuracy on the server-normalised value; truncation, repair rate, latency
p50/p95/max (nearest rank), mean and total cost, cost per 1,000 calls. A failed call counts as a
miss for every rate it belongs to.

## Known caveats

- `served_provider` is the pinned provider recorded by the gateway; the raw provider string from the
  response is not exposed on `ModelTurn`.
- Unspaced CJK (Mandarin fixtures `multi-020` to `multi-022`): `plan_search` tokenises on `\w+`, so
  a whole Chinese sentence is one word and the query and area checks fail for any model. Expect
  those cases to be low for every candidate; it is a product-side gap, not a model difference.
- `expected_language` is only `en` or `fr` (the server normalises everything else to `en`).
- Gold labels are one reasonable reading; ambiguous cases carry a note. Review disagreements before
  trusting a rate near a bar, and remember N is small (see the rule-of-three line in the report).
