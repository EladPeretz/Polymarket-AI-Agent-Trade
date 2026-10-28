# Daily report 2026-10-28

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:30:05 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 504.08 |
| Return | +0.82% |
| Gross PnL | +4.81 |
| Fees | -0.72 |
| Funding | +0.00 |
| Net PnL | +4.08 |
| Max drawdown | 1.57 (0.31%) |
| Trades | 4 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:39:20 | 10:18:03 | TSLA-USD | short | L2 | discretionary | 403.7344 | 399.17 | +2.38 | +1.13R | stop_loss |
| 10:18:31 | 10:57:06 | HOOD-USD | long | L2 | discretionary | 91.639 | 91.835 | +0.16 | +0.09R | Third no-volume poke above 92.16 sold back, flow now all sellers: the cover is not getting follow-through. Taking the small gain rather than waiting for the VWAP stop. |
| 11:06:29 | 11:13:07 | HOOD-USD | long | L2 | discretionary | 92.449 | 91.997 | -0.86 | -0.49R | The break above 92.24 printed on no volume and is back under 92.16: breakout_holds failed. Cutting at -0.55R instead of waiting for the stop. |
| 11:30:17 | 12:15:17 | WTIOIL-USD | long | L2 | discretionary | 102.63 | 103.315 | +2.41 | +0.98R | time stop |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 4 | 3 | +4.08 | +1.71R | +0.43R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 4 | 3 | +4.08 | +1.71R | +0.43R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| HOOD-USD | 2 | 1 | -0.70 | -0.40R | -0.20R |
| TSLA-USD | 1 | 1 | +2.38 | +1.13R | +1.13R |
| WTIOIL-USD | 1 | 1 | +2.41 | +0.98R | +0.98R |

## Decisions and risk

* Decisions: add 1, adjust 4, close 2, enter 14, note 1, reduce 1, stand_aside 2
* Risk verdicts: approved 13, rejected 2

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:43:34 | MSTR-USD | L2 | stop_volatility | stop is 2.75 away, inside the volatility floor 2.8800 (1 × the 15-minute range 2.88) |
| 09:59:12 | NVDA-USD | L2 | reward_risk | target 220.60 is worth 1.00R after costs, minimum 1.3R |

## Agent latency

* Events delivered: 99, answered: 97
* Pickup (event to delivery): median 0.0s, max 69.6s
* Answer (delivery to next call): median 3.3s, p90 9.9s, max 66.7s
