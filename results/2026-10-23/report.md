# Daily report 2026-10-23

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:18:47 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 498.02 |
| Return | -0.40% |
| Gross PnL | -0.90 |
| Fees | -1.08 |
| Funding | -0.00 |
| Net PnL | -1.98 |
| Max drawdown | 3.31 (0.66%) |
| Trades | 7 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:45:38 | 09:58:07 | WTIOIL-USD | long | L2 | discretionary | 102.46 | 102.18 | -0.84 | -0.30R | Assumption vwap_holds_as_support failed: after a high at 102.62 it fell back below VWAP 102.29 and out of the value area with sellers at 63% of flow. The breakout buyers are now the trapped side; cut at -0.3R rather than wait for the stop. |
| 09:48:52 | 10:48:12 | NVDA-USD | long | L2 | discretionary | 212.6 | 213.13 | +0.04 | +0.02R | time stop |
| 10:02:13 | 10:10:07 | MSTR-USD | long | L2 | discretionary | 149.34 | 148.3 | -1.22 | -0.58R | Breakout failed: the push above the prior high only swept 149.72 on no volume and was sold back under 149.36 to 148.5. Assumption prior_high_breaks_with_volume did not hold; failed_breakout scenario is playing out, so cut at -0.5R before VWAP. |
| 10:31:16 | 11:03:26 | MSFT-USD | long | L2 | discretionary | 523.06 | 524.76 | +1.03 | +0.49R | stop_loss |
| 11:02:01 | 11:18:11 | MSTR-USD | short | L2 | discretionary | 148.73 | 147.94 | +0.60 | +0.35R | stop_loss |
| 11:16:20 | 11:42:56 | TSLA-USD | long | L2 | discretionary | 376.5 | 374.26 | -1.53 | -0.73R | Assumption prior_close_holds failed: TSLA lost 375.08 and the prior low 374.48, so the gap-fill reclaim is being rejected. Cut before the stop. |
| 11:43:31 | 12:18:31 | WTIOIL-USD | long | L2 | discretionary | 103.1 | 103.14 | -0.07 | -0.03R | time stop |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 7 | 3 | -1.98 | -0.78R | -0.11R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 7 | 3 | -1.98 | -0.78R | -0.11R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| MSFT-USD | 1 | 1 | +1.03 | +0.49R | +0.49R |
| MSTR-USD | 2 | 1 | -0.62 | -0.24R | -0.12R |
| NVDA-USD | 1 | 1 | +0.04 | +0.02R | +0.02R |
| TSLA-USD | 1 | 0 | -1.53 | -0.73R | -0.73R |
| WTIOIL-USD | 2 | 0 | -0.91 | -0.33R | -0.17R |

## Decisions and risk

* Decisions: cancel 1, close 3, enter 16, note 1, stand_aside 1
* Risk verdicts: approved 14, rejected 2

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:38:20 | MSTR-USD | L2 | spread | spread 9.51 bp > 6 bp |
| 10:50:11 | WTIOIL-USD | L2 | reward_risk | target 101.80 is worth 0.59R after costs, minimum 1.3R |

## Agent latency

* Events delivered: 100, answered: 100
* Pickup (event to delivery): median 0.0s, max 14.9s
* Answer (delivery to next call): median 4.1s, p90 11.4s, max 54.6s
