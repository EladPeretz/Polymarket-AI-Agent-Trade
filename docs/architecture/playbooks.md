# Playbooks (WP9)

The owner's six playbooks (`decisions/owner-inputs.md`, 2026-10-20) as coded in
`src/perps_agent/strategy/playbooks/`. They are starting points and will keep
changing. Each is a pure function of the feature snapshot, so the backtest
runner (WP13) screens exactly what paper trading runs.

Common rules:

* **Entry window only:** quiet before 09:30 and from the 12:15 entry cutoff.
* **Stops:** never inside the risk engine's volatility floor (at least the
  15-minute range, 20 bp absolute minimum, plus 1 bp for price rounding). No
  15-minute range, no setup.
* **Targets:** measured after fees and stop slippage, as the risk engine does,
  so a setup's `reward_risk` is the number the engine checks against its 2R
  minimum. Parameter floors are 2.1R to leave room for price rounding.
* **Holding time:** scalps at most 15 minutes, everything else at most 2 hours,
  and never past the 12:30 window end.
* **Order type:** limit at the current price, except the momentum scalp, which
  is marketable.
* **Versions:** a setup carries `<id>@<logic version>.<parameter hash>`.

## Parameter bounds (proposed, for owner approval)

The learning loop (WP14) may tune each parameter only inside these bounds
(ADR-0010). Changing a bound, or a playbook's logic, is an owner-reviewed PR.

### `vwap_reversion` (intraday, ≤ 2 h)

Declared regimes: volatility any; structure any.

Owner: general, not tied to one regime.

Trigger: price is at least ``stretch_range_60m`` × the 60-minute range from
VWAP, and the last completed bar turned back towards VWAP (closed against its
open and beyond the previous close). Entry: limit at the current price. Stop:
beyond the extreme of the last 15 minutes, at least the volatility floor.
Target: VWAP, at least ``min_target_r`` away.

| Parameter | Default | Min | Max | Meaning |
| - | - | - | - | - |
| `min_session_minutes` | 15 | 5 | 90 | minutes after the open before it may fire |
| `stretch_range_60m` | 0.6 | 0.25 | 1.5 | × 60-minute range |
| `min_target_r` | 2.2 | 2.1 | 5 | target at least this many R, net |
| `max_hold_minutes` | 60 | 1 | 120 |  |

### `momentum_scalp` (scalp, ≤ 15 min)

Declared regimes: volatility expansion or normal; structure trend_down or trend_up.

Owner: trending sessions, not compressed price action. Scalp, 1–15 minutes;
marketable entry, since getting in needs aggression.

Trigger: the session is trending, the last completed bar closed beyond the
high (low) of the ``breakout_lookback`` bars before it, on at least
``volume_multiple`` × their average volume, and price is on the trend side of
VWAP. Stop: beyond the breakout bar, at least the volatility floor. Target:
``target_r`` × the risk.

| Parameter | Default | Min | Max | Meaning |
| - | - | - | - | - |
| `breakout_lookback` | 10 | 3 | 30 | bars the breakout clears |
| `volume_multiple` | 1.5 | 1 | 4 | breakout bar volume × lookback average |
| `target_r` | 2.5 | 2.1 | 4 | R, net of costs |
| `max_hold_minutes` | 10 | 1 | 15 |  |

### `mean_reversion` (intraday, ≤ 2 h)

Declared regimes: volatility any; structure any.

Owner: compressed sessions, or fading recent breakouts that go against the
trend; breakout fades only with caution.

* ``value_area`` variant (compressed sessions only): price is outside the
  session's value area and the last completed bar turned back inside it.
* ``breakout_fade`` variant (any session, caution, at most ``fade_max_size``):
  price broke the opening range against the ticker's recent trend (its 5-session
  return) and the last completed bar closed back inside the range.

Entry: limit at the current price. Stop: beyond the extreme of the last
``stop_lookback`` bars, at least the volatility floor. Target: the session POC,
at least ``min_target_r`` away.

| Parameter | Default | Min | Max | Meaning |
| - | - | - | - | - |
| `min_session_minutes` | 30 | 15 | 120 |  |
| `stop_lookback` | 10 | 3 | 30 | bars whose extreme the stop clears |
| `min_target_r` | 2.2 | 2.1 | 5 | target at least this many R, net |
| `fade_max_size` | 0.5 | 0.1 | 1 | budget cap for breakout fades |
| `max_hold_minutes` | 90 | 1 | 120 |  |

### `liquidity_sweep` (intraday, ≤ 2 h)

Declared regimes: volatility any; structure any.

Owner: any session with a significant support or resistance level from the
last or current session. Price crosses the level and sweeps right back in;
higher conviction when the sweep comes on high volume.

Levels: the prior session's high, low, close and POC, today's opening range
(once complete), and today's high and low from before the sweep. Trigger:
within the last ``sweep_window`` completed bars a bar pierced a level by at
least ``min_pierce_bp``, price had been on the other side before, and the last
completed bar closed back there. Entry: limit at the current price. Stop:
beyond the sweep's extreme, at least the volatility floor. Target:
``target_r`` × the risk. When several levels were swept, the deepest pierce wins.

| Parameter | Default | Min | Max | Meaning |
| - | - | - | - | - |
| `sweep_window` | 5 | 1 | 15 | bars the sweep happened in |
| `min_pierce_bp` | 3 | 1 | 30 | how far past the level |
| `high_volume_multiple` | 2 | 1.2 | 5 | sweep volume × session average |
| `target_r` | 2.5 | 2.1 | 5 | R, net of costs |
| `max_hold_minutes` | 60 | 1 | 120 |  |

### `trend_following` (intraday, ≤ 2 h)

Declared regimes: volatility compressed or normal; structure trend_down or trend_up.

Owner: calm tickers that show consistent strength through the session.

Trigger: a trend session that is not in expansion, with at least
``min_efficiency`` net move, at least ``min_vwap_side_share`` of bars closing
on the trend side of VWAP, and price pulled back to within
``max_pullback_ranges`` × the 15-minute range of VWAP, still on the trend side.
Consistent weakness is traded short the same way. Entry: limit at the current
price. Stop: ``stop_beyond_vwap_ranges`` × the 15-minute range through VWAP,
at least the volatility floor. No target: a runner, managed by its exit plan.

| Parameter | Default | Min | Max | Meaning |
| - | - | - | - | - |
| `min_session_minutes` | 45 | 15 | 150 |  |
| `min_efficiency` | 0.5 | 0.3 | 0.9 |  |
| `min_vwap_side_share` | 0.8 | 0.6 | 1 | bars closing on the trend side |
| `max_pullback_ranges` | 0.5 | 0 | 2 | × 15-minute range from VWAP |
| `stop_beyond_vwap_ranges` | 0.5 | 0 | 2 |  |
| `max_hold_minutes` | 120 | 1 | 120 |  |

### `opening_flush` (intraday, ≤ 2 h, long only)

Declared regimes: volatility any; structure any.

Owner: beaten-down tickers that flush off the open. **Long only** for now.

Trigger: the ticker is beaten down (5-session return at most
``-beaten_down_bp``); within ``flush_window_minutes`` of the open it fell at
least ``min_flush_bp`` below the session open; price has reclaimed at least
``reclaim_fraction`` of the flush, and the last completed bar closed up and
above the previous bar's high. Valid until ``entry_deadline_minutes`` after
the open. Entry: limit at the current price. Stop: below the flush low, at
least the volatility floor. Target: ``target_r`` × the risk.

| Parameter | Default | Min | Max | Meaning |
| - | - | - | - | - |
| `beaten_down_bp` | 500 | 200 | 2000 | 5-session loss at least this |
| `flush_window_minutes` | 30 | 5 | 60 |  |
| `min_flush_bp` | 100 | 30 | 400 | below the open |
| `reclaim_fraction` | 0.3 | 0.1 | 0.8 | of the flush |
| `entry_deadline_minutes` | 90 | 15 | 150 |  |
| `target_r` | 3 | 2.1 | 6 | R, net of costs |
| `max_hold_minutes` | 120 | 1 | 120 |  |

## Not in this package

* The target-reachability check and any 1.5R exceptions for NVDA and MSFT wait
  for the WP13 backtests, as agreed.
* Tuning of the stop-floor multiple (`risk.stop_floor_range_multiple`, 1 for now,
  bounded 1–5) also waits for WP13.
