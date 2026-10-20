# Venue contract-test fixtures

* `rest/` holds raw public REST responses recorded from
  `https://api.perpetuals.polymarket.com` by `scripts/record_venue_fixtures.py`
  (see `rest/manifest.json` for when, the engine version and the SDK version).
  They are trimmed to NVDA-USD, BTC-USD and SP500-USD. SP500-USD (instrument 1)
  is the first ticker row, which the ticker regression test relies on.
* `ws/frames.jsonl` holds WebSocket frames, one JSON object per line. The cloud
  development container cannot open WebSocket connections, so these were
  **written by hand from the AsyncAPI examples** (`/asyncapi-perps.json`) with
  NVDA-USD values taken from the REST recording. Replace them with a real
  recording (`--ws-seconds 10`) from a machine that can reach the WebSocket.
  The last line is deliberately malformed (a BBO frame without the ask side).

Re-record after an SDK upgrade or a venue API change, then run
`pytest tests/contract`. Recording is read-only: public endpoints only, no
credentials, no orders.
