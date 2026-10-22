# Daily report 2026-10-22

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:30:10 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 495.35 |
| Return | -0.93% |
| Gross PnL | -3.72 |
| Fees | -0.93 |
| Funding | +0.00 |
| Net PnL | -4.65 |
| Max drawdown | 4.69 (0.94%) |
| Trades | 6 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:36:12 | 09:46:09 | NVDA-USD | long | L2 | discretionary | 217.95 | 217.33 | -0.92 | -0.33R | Thesis broken: the prior-low 218.06 reclaim failed, price lost VWAP and sits on the session low with only 2% of flow buying while MSFT breaks down on 22x volume. vwap_holds_as_support failed; cut at -0.45R rather than wait for the stop. |
| 09:52:56 | 11:09:16 | MSTR-USD | long | L2 | discretionary | 148.9537 | 147.17 | -1.43 | -0.68R | time stop |
| 10:02:30 | 10:07:08 | HOOD-USD | long | L2 | discretionary | 101.16 | 100.46 | -0.98 | -0.40R | Breakout failed: swept the session high 101.40 and fell back under the opening-range high 101.05 (or_high_breakout_holds failed). The squeeze is not following through; cut at about -0.35R instead of waiting for the stop. |
| 10:09:53 | 11:24:53 | TSLA-USD | short | L2 | discretionary | 386.19 | 383.3755 | +1.24 | +0.44R | time stop |
| 10:15:25 | 10:54:43 | MSFT-USD | short | L2 | discretionary | 524.17 | 525.03 | -0.89 | -0.36R | stop_loss |
| 11:40:36 | 12:10:17 | WTIOIL-USD | short | L2 | discretionary | 98.28 | 98.783 | -1.66 | -0.79R | vwap_caps_bounces failed: oil reclaimed VWAP 98.7 and the prior high 98.6 and has held above for 8 minutes. The read is wrong; close now instead of waiting for the stop 6 cents away. |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 6 | 1 | -4.65 | -2.13R | -0.35R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 6 | 1 | -4.65 | -2.13R | -0.35R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| HOOD-USD | 1 | 0 | -0.98 | -0.40R | -0.40R |
| MSFT-USD | 1 | 0 | -0.89 | -0.36R | -0.36R |
| MSTR-USD | 1 | 0 | -1.43 | -0.68R | -0.68R |
| NVDA-USD | 1 | 0 | -0.92 | -0.33R | -0.33R |
| TSLA-USD | 1 | 1 | +1.24 | +0.44R | +0.44R |
| WTIOIL-USD | 1 | 0 | -1.66 | -0.79R | -0.79R |

## Decisions and risk

* Decisions: add 2, adjust 2, close 3, enter 17, note 5, stand_aside 2
* Risk verdicts: approved 10, rejected 7, resized 2

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:34:29 | HOOD-USD | L2 | spread | spread 9.47 bp > 6 bp |
| 09:43:13 | WTIOIL-USD | L2 | max_positions, stop_volatility | 3 positions open; stop is 0.570 away, inside the volatility floor 0.6190 (1 × the 15-minute range 0.619) |
| 09:45:47 | WTIOIL-USD | L2 | max_positions, stop_volatility | 3 positions open; stop is 0.670 away, inside the volatility floor 0.6800 (1 × the 15-minute range 0.68) |
| 09:46:45 | MSFT-USD | L2 | stop_volatility | stop is 4.50 away, inside the volatility floor 4.7200 (1 × the 15-minute range 4.72) |
| 10:02:10 | HOOD-USD | L2 | stop_volatility | stop is 1.590 away, inside the volatility floor 1.6400 (1 × the 15-minute range 1.64) |
| 10:02:21 | HOOD-USD | L2 | stop_volatility | stop is 1.560 away, inside the volatility floor 1.6400 (1 × the 15-minute range 1.64) |
| 10:09:44 | TSLA-USD | L2 | stop_volatility | stop is 4.54 away, inside the volatility floor 4.7800 (1 × the 15-minute range 4.78) |

### Resizes

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 10:45:17 | MSTR-USD | L2 | add_risk | budget cut to the position's remaining 2.098462135 |
| 10:45:34 | MSTR-USD | L2 | add_risk | budget cut to the position's remaining 2.098462135 |

## Agent latency

* Events delivered: 101, answered: 100
* Pickup (event to delivery): median 0.0s, max 41.0s
* Answer (delivery to next call): median 6.3s, p90 10.6s, max 16.6s
