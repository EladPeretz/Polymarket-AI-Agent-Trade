# Venue contract-test fixtures

* `rest/` holds raw public REST responses recorded from
  `https://api.perpetuals.polymarket.com` by `scripts/record_venue_fixtures.py`
  (see `rest/manifest.json` for when, the engine version and the SDK version).
  They are trimmed to NVDA-USD, BTC-USD and SP500-USD. SP500-USD (instrument 1)
  is the first ticker row, which the ticker regression test relies on.
* `ws/frames.jsonl` holds real WebSocket frames for NVDA-USD (`--ws-seconds`),
  the first three per channel plus the subscribe ack. No trade printed during
  the recording.
* `ws/synthetic.jsonl` holds hand-written frames for what the recording lacked:
  a trades frame (shape from the AsyncAPI example), a ticker for another
  instrument (to test filtering), and a deliberately malformed BBO frame.

Re-record after an SDK upgrade or a venue API change, then run
`pytest tests/contract`. Recording is read-only: public endpoints only, no
credentials, no orders.
