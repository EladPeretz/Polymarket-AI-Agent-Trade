# Daily report 2026-10-26

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:30:09 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 497.37 |
| Return | -0.53% |
| Gross PnL | -1.95 |
| Fees | -0.68 |
| Funding | -0.00 |
| Net PnL | -2.63 |
| Max drawdown | 2.95 (0.59%) |
| Trades | 5 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:39:22 | 09:50:11 | WTIOIL-USD | short | L2 | discretionary | 101.35 | 101.58 | -1.12 | -0.53R | Prior low 101.50 reclaimed and held, price 20bp over VWAP, flow 100% buying: the trapped-buyer read is wrong, buyers are absorbing the gap. Cut at -0.6R rather than wait for the stop. |
| 09:59:04 | 11:51:55 | TSLA-USD | long | L2 | discretionary | 393.9141 | 394.7749 | +0.43 | +0.17R | stop_loss |
| 10:38:22 | 10:42:43 | HOOD-USD | short | L2 | discretionary | 92.45 | 93.013 | -0.07 | -0.04R | The assumption this trade rested on has failed: HOOD is 40bp above VWAP with 7 crosses and efficiency down to 0.51, so the falling VWAP is not capping the bounce and the balance is resolving up, not down. Cut the stub fill at -0.4R. |
| 10:48:46 | 11:00:17 | MSTR-USD | short | L2 | discretionary | 142.1 | 142.93 | -0.57 | -0.40R | The extension I faded keeps extending: new session high 143.38, now 184bp above VWAP and 176bp above the POC with buyers the aggressors. The market has accepted the move away from value, so my revert_to_value read is wrong. Cut at -0.36R. |
| 11:12:56 | 11:50:48 | NVDA-USD | long | L2 | discretionary | 217.42 | 216 | -1.29 | -0.74R | The assumption behind paying the spread was that pullbacks keep being bought before VWAP. NVDA has now given the whole move back to VWAP on falling efficiency, so the read is invalidated. Cut at -0.76R instead of waiting for the stop 40bp lower. |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 5 | 1 | -2.63 | -1.55R | -0.31R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 5 | 1 | -2.63 | -1.55R | -0.31R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| HOOD-USD | 1 | 0 | -0.07 | -0.04R | -0.04R |
| MSTR-USD | 1 | 0 | -0.57 | -0.40R | -0.40R |
| NVDA-USD | 1 | 0 | -1.29 | -0.74R | -0.74R |
| TSLA-USD | 1 | 1 | +0.43 | +0.17R | +0.17R |
| WTIOIL-USD | 1 | 0 | -1.12 | -0.53R | -0.53R |

## Decisions and risk

* Decisions: add 5, adjust 2, cancel 2, close 4, enter 11, reduce 1, stand_aside 6
* Risk verdicts: approved 12, rejected 4

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:39:14 | WTIOIL-USD | L2 | reward_risk | target 100.62 is worth 0.91R after costs, minimum 1.3R |
| 10:21:34 | TSLA-USD | L2 | stop_volatility | stop is 3.41 away, inside the volatility floor 4.1500 (1 × the 15-minute range 4.15) |
| 10:21:47 | TSLA-USD | L2 | stop_volatility | stop is 3.87 away, inside the volatility floor 4.1500 (1 × the 15-minute range 4.15) |
| 10:53:16 | TSLA-USD | L2 | stop_volatility | stop is 2.25 away, inside the volatility floor 2.5100 (1 × the 15-minute range 2.51) |

## Agent latency

* Events delivered: 79, answered: 78
* Pickup (event to delivery): median 0.0s, max 14.5s
* Answer (delivery to next call): median 4.8s, p90 16.3s, max 34.3s
