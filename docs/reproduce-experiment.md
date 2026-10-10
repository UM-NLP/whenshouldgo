# Reproduce the method experiment

Use Python 3 with the standard library. Optional JavaScript calculation checks require Node.js. Provider calls can incur charges; request caps are implemented but billed cost is not measured.

Create .env.local in the repository root with GOOGLE_MAPS_API_KEY and TOMTOM_API_KEY entries. Enable the provider services and restrict keys to the required APIs. Never put keys in commands, issues or Git. Store private input and responses only under .local/.

Create .local/trip.json with origin and destination address strings, deadline and searchStart ISO timestamps with explicit offsets, bufferMinutes (initially 0) and avoidTolls (true). Choose a future deadline and a search window of at most four hours. There is no checked-in private trip configuration.

```powershell
py tools/run_departure_test.py
py tools/check_at_departure.py --help
py tools/benchmark_providers.py --deadline "<future ISO timestamp with offset>"
```

The benchmark first previews geocodes. Review each match, then pass its saved path using --confirm-locations with the same --deadline. Alternatively create a reviewed locations JSON containing two entries with input matching the trip address strings and position objects containing lat and lon. Coordinates must be finite and within geographic bounds. Use --buffer to test an explicitly chosen allowance. Record it in the results.

Run the watch command printed by planning. Keep the computer awake; start before either target. The watcher checks both providers at both recommendations and saves cumulative observations locally. A target missed by more than 60 seconds stops calls. A live API estimate is not a Google Maps screen observation or an actual arrival measurement.

Offline checks (no provider calls):

```powershell
py -m unittest discover -s tools -p test_benchmark_providers.py -v
py tools/check_at_departure.py --self-test
node tools/departure-test.test.mjs
```

Before sharing results, remove addresses, coordinates, encoded polylines, raw provider responses, machine paths and credentials. Report duration revision, predicted deadline misses, request timing and latency separately from true journey accuracy.

