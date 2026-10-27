# Daily report 2026-10-27

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:15:15 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 496.61 |
| Return | -0.68% |
| Gross PnL | -2.24 |
| Fees | -1.15 |
| Funding | -0.00 |
| Net PnL | -3.39 |
| Max drawdown | 6.10 (1.22%) |
| Trades | 7 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:38:11 | 10:25:59 | TSLA-USD | long | L2 | discretionary | 400.39 | 401.51 | +0.34 | +0.14R | TSLA regime flipped from trend_up to rotation and price is now under VWAP (401.84) with no aggressive buyers; the balance scenario is playing out, not the continuation. Take the small profit. |
| 09:45:30 | 10:25:58 | MSFT-USD | short | L2 | discretionary | 531.19 | 530.6126 | +0.07 | +0.04R | MSFT is back above VWAP (530.34) with 9 VWAP crosses: the session turned into a rotation and the trapped-longs unwind has run its course at the 528.6 low. Take the small profit rather than wait for the stop. |
| 09:47:17 | 10:13:08 | MSTR-USD | short | L2 | discretionary | 140.38 | 140.55 | -0.19 | -0.09R | MSTR made a new low at 138.85 and was bought straight back above VWAP (140.18). My read needed VWAP to cap rallies; it did not, and the low sweep was reclaimed. This is a two-way rotation, not a trapped-longs unwind. Exit near flat. |
| 10:13:32 | 10:27:35 | NVDA-USD | short | L2 | discretionary | 216.12 | 217.0413 | -1.39 | -0.79R | NVDA reclaimed VWAP and the prior close; the vwap_caps_rallies assumption failed and the whole tech group has turned to rotation. Cut before the stop. |
| 10:30:22 | 11:09:58 | MSTR-USD | long | L2 | discretionary | 140.83 | 139.87 | -1.23 | -0.71R | MSTR lost VWAP (140.36) and is back at the open/POC 139.9 after failing to retest 141.9. The vwap_holds_as_support assumption failed and the OI build did not produce follow-through: pure rotation. Cut before the stop. |
| 11:15:23 | 11:55:16 | WTIOIL-USD | short | L2 | discretionary | 99.988 | 100.35 | -1.37 | -0.79R | stop_loss |
| 11:30:20 | 12:06:34 | TSLA-USD | long | L2 | discretionary | 405.25 | 406.37 | +0.39 | +0.22R | stop_loss |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 7 | 3 | -3.39 | -1.98R | -0.28R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 7 | 3 | -3.39 | -1.98R | -0.28R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| MSFT-USD | 1 | 1 | +0.07 | +0.04R | +0.04R |
| MSTR-USD | 2 | 0 | -1.42 | -0.80R | -0.40R |
| NVDA-USD | 1 | 0 | -1.39 | -0.79R | -0.79R |
| TSLA-USD | 2 | 2 | +0.73 | +0.36R | +0.18R |
| WTIOIL-USD | 1 | 0 | -1.37 | -0.79R | -0.79R |

## Decisions and risk

* Decisions: adjust 4, close 5, enter 9, note 3, stand_aside 3
* Risk verdicts: approved 7, rejected 2

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:45:22 | MSFT-USD | L2 | reward_risk | target 525.40 is worth 1.08R after costs, minimum 1.3R |
| 11:10:16 | MSFT-USD | L2 | reward_risk | target 531.45 is worth 0.57R after costs, minimum 1.3R |

## Agent latency

* Events delivered: 63, answered: 63
* Pickup (event to delivery): median 0.0s, max 14.5s
* Answer (delivery to next call): median 5.1s, p90 14.1s, max 16.6s
