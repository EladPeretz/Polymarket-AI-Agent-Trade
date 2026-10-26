# Daily report 2026-10-26

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:23:35 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 500.10 |
| Return | +0.02% |
| Gross PnL | +1.06 |
| Fees | -0.96 |
| Funding | -0.00 |
| Net PnL | +0.10 |
| Max drawdown | 3.80 (0.76%) |
| Trades | 6 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:39:17 | 09:47:33 | WTIOIL-USD | short | L2 | discretionary | 101.34 | 101.59 | -1.22 | -0.58R | Thesis failed: price reclaimed the prior low 101.50 and sits 25bp above VWAP with buyers now aggressive; sellers who pressed the break were absorbed. Cutting before the 101.71 stop. |
| 09:47:10 | 09:51:15 | MSTR-USD | short | L2 | discretionary | 137.95 | 139.69 | -1.08 | -0.62R | Second sweep of the 137.5 low was bought and price is back 50bp above VWAP with 100% buy flow while HOOD bounces too: crypto proxies are bid, the gap-continuation read is wrong. Cut at -0.5R rather than wait for the stop. |
| 09:47:10 | 11:29:28 | TSLA-USD | long | L2 | discretionary | 394.409 | 395.705 | +0.84 | +0.40R | stop_loss |
| 10:06:53 | 10:08:37 | WTIOIL-USD | long | L2 | discretionary | 101.64 | 101.2095 | -1.29 | -0.74R | Prior low 101.50 lost again and price is back under VWAP within two minutes: crude is a two-sided rotation around 101.2-101.9, not a squeeze. Cutting; no more WTI reads from the level today. |
| 10:45:27 | 11:30:05 | HOOD-USD | long | L2 | discretionary | 93.006 | 94.176 | +1.54 | +0.88R | stop_loss |
| 11:43:19 | 12:10:11 | MSTR-USD | short | L2 | discretionary | 144.09 | 142.84 | +1.31 | +0.94R | stop_loss |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 6 | 3 | +0.10 | +0.28R | +0.05R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 6 | 3 | +0.10 | +0.28R | +0.05R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| HOOD-USD | 1 | 1 | +1.54 | +0.88R | +0.88R |
| MSTR-USD | 2 | 1 | +0.23 | +0.32R | +0.16R |
| TSLA-USD | 1 | 1 | +0.84 | +0.40R | +0.40R |
| WTIOIL-USD | 2 | 0 | -2.51 | -1.32R | -0.66R |

## Decisions and risk

* Decisions: add 3, adjust 4, close 3, enter 9, note 2, stand_aside 5
* Risk verdicts: approved 9, rejected 3

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:54:18 | WTIOIL-USD | L2 | reward_risk | target 102.40 is worth 0.94R after costs, minimum 1.3R |
| 09:57:18 | WTIOIL-USD | L2 | stop_volatility | stop is 0.46 away, inside the volatility floor 0.4800 (1 × the 15-minute range 0.48) |
| 11:19:16 | TSLA-USD | L2 | stop_volatility | stop is 1.90 away, inside the volatility floor 3.0700 (1 × the 15-minute range 3.07) |

## Agent latency

* Events delivered: 103, answered: 102
* Pickup (event to delivery): median 0.0s, max 12.9s
* Answer (delivery to next call): median 6.1s, p90 14.2s, max 26.3s
