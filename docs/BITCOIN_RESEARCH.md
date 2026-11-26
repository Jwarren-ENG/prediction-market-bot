# Bitcoin seven-day spot momentum study

This study asks whether one fixed trading rule left a $10,000 paper account better off than either a single $500 Bitcoin purchase or simply holding dollars. It uses Bitstamp BTC/USD daily prices. The public repository contains the rule, calculation code and tests. Running the script produces a private results file on your own computer. [Study script](../scripts/bitcoin_study.py) · [Tests](../tests/test_bitcoin_study.py)

## What the rule knows when it trades

After a UTC day ends, compare its closing BTC price with the close seven calendar days earlier. Eight consecutive completed daily candles must be present, each with positive trading volume. A higher close calls for holding BTC. An equal or lower close calls for cash. A missing day breaks the sequence. For example, a decision made after April 8 compares the April 8 close with April 1. It can first change the position at the first observed April 9 trade represented by that day's opening price.

The account starts entirely in dollars. On a cash-to-BTC transition it spends the smaller of $500 or 5% of available account equity, including the purchase fee. An account with $8,000 in cash can therefore spend $400. While long, it holds the original quantity. A cash signal sells the entire quantity at the next available traded open. Cash earns zero interest, borrowing is disabled, and the model assumes no tax.

A missing target preserves the previous target. A pending transition waits for the next observed traded open. An unobserved date gets no price or mark. After a gap the rule needs eight consecutive closes to generate fresh decisions. The script rejects contradictory duplicate candles, invalid products or prices, and timestamps that fall outside UTC midnight. Both the first and last study dates need observed bars to produce a report.

## Dates and controls fixed in advance

| Record | Dates, inclusive | Opening balance and purpose |
| --- | --- | --- |
| Signal warmup | December 24–31, 2017 | Eight earlier closes for the January 1 opening decision. No study positions. |
| Exploratory | January 1, 2018–December 31, 2021 | Fresh $10,000 for each account. Inspect behavior across different price environments. |
| Historical holdout | January 1, 2022–November 25, 2026 | Fresh $10,000 for each account. Evaluate the already specified rule against an independently initialized later period. |
| Continuous | January 1, 2018–November 25, 2026 | One uninterrupted account per strategy. Calendar-year results carry the previous year's holdings and equity forward. |

The dates, rule, position size, costs and stress cases were written down before retrieving study prices. A review of the originally proposed provider's market-data terms led to one amendment at **2026-11-26 20:56:42 UTC**, before the first retrieval. Bitstamp replaced Coinbase as the common BTC/USD source for historical and forward paper use. The initial protocol's SHA-256 was `ddeddc38c539eb50cd8ee68a4d18660ba673b595a1748866bfc5355b01fe5c4d`. These are retrospective periods selected after the calendar dates had passed. Only a subsequent live observation period tests decisions as events unfold. [Coinbase Market Data Terms](https://www.coinbase.com/legal/market_data) · [Bitstamp public API and market-data use](https://www.bitstamp.net/api/)

The passive control spends $500 at the first observed open of each independently initialized period and holds that BTC quantity. Its position can grow beyond $500 or lose value. The cash control keeps all $10,000 in zero-yield dollars throughout. Each control uses the same calendar and valuation assumptions as the rule. The report also shows what happened during 2022 and during the incomplete 2026 calendar year. Percentages use the whole account as their denominator.

## What a historical fill costs

The base case charges a 0.60% taker fee on the modeled trade notional for each purchase and each sale. The execution price moves 0.05% against the account on each side. These are fixed study assumptions across the entire history. Account-specific fee tiers, historical spreads and available size at the daily open are unknown. Bitstamp publishes its current fees separately. [Bitstamp fee schedule](https://www.bitstamp.net/fee-schedule/)

For a gross purchase budget `B`, an observed opening price `P`, fee rate `f` and adverse execution fraction `s`, the BTC quantity is `B / ((1 + f) × P × (1 + s))`. Selling `q` BTC at a later opening price credits `q × P × (1 − s) × (1 − f)`. At each observed close the account values open BTC using the same net sale formula. Estimated exit costs on a terminal holding are included in ending equity. The paid-fee and fill counts only include trades actually simulated.

A daily open is the first recorded trade in that bucket. A continuously traded venue can print it before the previous candle is published and a local client can read it. The base case represents ideal timing. The timing sensitivity waits one additional UTC daily open with the same rule. The first delayed run has only the eight registered warmup closes available, so its first actionable signal can trade on January 2, 2018. The independent holdout already has earlier signal history.

The output repeats the continuous and later periods with double fees and adverse execution, the extra open of delay, and both changes together. A separate zero-cost calculation measures how much the modeled costs change the final account. It lets you identify a rule that appears attractive only with free execution. A cost change can also change the size of a later entry because losses reduce available equity.

## Reproduce the private study

Use Python 3.11 or later from the repository root. The script makes public HTTPS reads against Bitstamp's `/api/v2/ohlc/btcusd/` endpoint. It requests 86,400-second candles, excludes the currently open candle and clips every page to its requested UTC interval. A transport failure gets bounded retries. The tool prints JSON to standard output and errors to standard error. Choose a private output file outside the checkout. No exchange credentials are needed. [Bitstamp OHLC reference](https://www.bitstamp.net/api/) · [Study source](../scripts/bitcoin_study.py)

**macOS or Linux**

```sh
(umask 077 && python3 -I -B scripts/bitcoin_study.py --through 2026-11-25 > "$HOME/Downloads/bitcoin-study-2026-11-25.json")
```

**Windows PowerShell**

```powershell
py -3 -I -B scripts/bitcoin_study.py --through 2026-11-25 |
    Out-File -Encoding utf8 (Join-Path $env:USERPROFILE "Downloads\bitcoin-study-2026-11-25.json")
```

A successful run exits with status zero. Both examples overwrite the named output, so give a repeat run a new filename to preserve the first one. On a new run, omitting `--through` extends the evaluation to the last fully completed UTC date. Keep that extended record separate from the originally specified holdout. The report records its UTC retrieval time, coverage, missing date ranges and a SHA-256 of the normalized daily observations. Matching the date bounds and checksum allows two runs to establish that they used the same input. Exchange corrections can change the checksum on a later retrieval.

| JSON field | What to look for |
| --- | --- |
| `periods` | Ending marked equity, net gain or loss and return for momentum, passive BTC and cash in each independently initialized window and across the full run. |
| `max_drawdown_usd` and `max_drawdown_pct` | Largest decline from a prior equity peak on observed daily closes. Each maximum is tracked independently. Intraday losses can be larger. |
| `buy_count`, `sell_count`, `turnover_usd`, `paid_fees_usd` | Number of transitions, total buy plus sell notional, and cumulative fees paid. `modeled_adverse_execution_usd` measures the extra price cost on actual model fills. |
| `continuous_calendar_years` | Each account's net change from the prior available year-end mark. The holdings carry over. The final partial year ends at the report cutoff if the selected end date falls before December 31. |
| `stress` | The same signals under the alternate timing and cost assumptions. Compare these figures with the corresponding base period. |
| `observed_daily_bars` and `missing_date_ranges` | Count of traded bars including warmup and missing UTC date ranges. Explicitly check coverage and the number of observed dates in the evaluation period. |

## Why test this rule

Liu and Tsyvinski reported a relationship between past cryptocurrency returns and subsequent returns in their historical sample. This motivated testing a simple, stated-in-advance rule. Their sample, portfolio construction and costs differ from those in this experiment. Moskowitz, Ooi and Pedersen studied trend signals across 58 futures contracts and substantially longer horizons. A slower decision frequency is a plausible separate experiment with lower turnover. It would need its own rule and fresh evaluation window. [Risks and Returns of Cryptocurrency](https://doi.org/10.1093/rfs/hhaa113) · [Time Series Momentum](https://doi.org/10.1016/j.jfineco.2011.11.003)

Makarov and Schoar documented persistent price differences across exchanges in their research period. This is a reason to make the trade price, passive benchmark and forward book refer to the same BTC/USD market. A pooled price index could conceal the spread a specific account faced. Repeatedly selecting settings that look best on an already inspected holdout also weakens its evidentiary value. The report preserves the original settings and includes every prespecified sensitivity alongside the simple controls. [Trading and Arbitrage in Cryptocurrency Markets](https://doi.org/10.1016/j.jfineco.2019.07.001) · [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

## How the forward trial and weather desk differ

The Bitcoin paper worker evaluates the same eight daily bars and waits for a fresh timestamped Bitstamp bid or ask with sufficient displayed quantity. The entry uses the observed ask, the exit uses the observed bid, and the adverse execution assumption and fee apply on top. The worker rounds conservatively to cents and satoshis. The historical study uses fractional quantities and daily trades because it cannot reconstruct the order book or the delay between a candle closing and its publication. Missed reads, stale books, small quotes and outages can produce different results in the forward account. [Bitcoin paper worker](../worker/bitcoin.py)

The weather model estimates the probability of a station's daily high falling inside a particular range. It needs matched prior forecasts and verified outcomes, then compares a conservative probability bound with an executable contract price after fees. The BTC model estimates a direction from recent returns, carries spot exposure until a later signal, and measures dollar gains or losses against cash and a passive BTC purchase. In a prospective comparison, use the same start date, include idle cash, report quote coverage and count distinct weather dates separately from BTC transitions. A weather account with insufficient calibration belongs in the record as idle. [Weather decisions and account valuation](HOW_IT_WORKS.md)

## What an options study would require

A spot opening price supplies neither an option premium nor an executable spread. A separate paper project needs a named contract family, a legible reason to use it, and a dated entry and exit rule. For example, a call on a CME Bitcoin futures contract depends on that futures contract and its exercise and settlement rules. A U.S. listed ETF option has its own underlying, multiplier, exercise style and corporate actions. [CME cryptocurrency options FAQ](https://www.cmegroup.com/articles/faqs/frequently-asked-questions-options-on-cryptocurrency-futures.html) · [OCC options disclosure](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)

An initial study could follow one long-option contract at a time, reserving its entire premium and fees and specifying exactly when to close or how to process expiry. That requires timestamped, appropriately licensed bid/ask quotes and size for the eligible option chain, the underlying price at each decision, contract terms and trading calendars. Record all eligible strikes and expirations at decision time to avoid choosing surviving contracts after the fact. Test wide spreads and absent bids, volatility changes, expiry and any resulting settlement or margin obligations. A missing executable quote produces a skipped decision or unavailable exit mark. Compare the independently specified options trial with the simpler spot and cash controls on the same prospective dates.
