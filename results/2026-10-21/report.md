# Daily report 2026-10-21

mode paper · symbols NVDA-USD, TSLA-USD, MSFT-USD, HOOD-USD, MSTR-USD, WTIOIL-USD · generated 12:34:13 New York

## Incidents

None.

## Result

|  |  |
| - | - |
| Start equity | 500.00 |
| End equity | 498.85 |
| Return | -0.23% |
| Gross PnL | -1.06 |
| Fees | -0.10 |
| Funding | +0.00 |
| Net PnL | -1.15 |
| Max drawdown | 1.15 (0.23%) |
| Trades | 1 |

## Trades

| Open | Close | Symbol | Side | Level | Source | Entry | Exit | Net | R | Exit reason |
| - | - | - | - | - | - | - | - | - | - | - |
| 09:42:38 | 09:47:59 | TSLA-USD | short | L2 | discretionary | 383.37 | 386.68 | -1.15 | -0.66R | Thesis invalidated: price broke above the opening high 385.88 to 387 with 0.99 efficiency, regime now expansion/trend_up, 75 bp above VWAP. vwap_caps_bounce and gap_fades both failed; cutting at -0.75R instead of waiting for the stop. |

## By autonomy level

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| L2 | 1 | 0 | -1.15 | -0.66R | -0.66R |

## By source

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| discretionary | 1 | 0 | -1.15 | -0.66R | -0.66R |

## By symbol

|  | Trades | Wins | Net | Total R | Avg R |
| - | - | - | - | - | - |
| TSLA-USD | 1 | 0 | -1.15 | -0.66R | -0.66R |

## Decisions and risk

* Decisions: close 1, enter 7, note 1
* Risk verdicts: approved 4, rejected 3

### Rejections

| Time | Symbol | Level | Checks | Detail |
| - | - | - | - | - |
| 09:39:27 | TSLA-USD | L2 | depth | top of book 257.25 < 2000 on one side |
| 09:42:02 | TSLA-USD | L2 | spread | spread 7.81 bp > 6 bp |
| 09:47:18 | WTIOIL-USD | L2 | depth | top of book 257.21 < 2000 on one side |

## Agent latency

* Events delivered: 131, answered: 131
* Pickup (event to delivery): median 0.0s, max 45.3s
* Answer (delivery to next call): median 0.9s, p90 8.9s, max 17.6s
