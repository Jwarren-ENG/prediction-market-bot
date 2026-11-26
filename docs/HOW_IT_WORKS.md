# How Prediction Market Bot works

Prediction Market Bot (PMB) follows one series on one venue. Each Kalshi `KXHIGHNY` event asks which temperature range contains the highest temperature recorded for New York’s Central Park on a particular date. PMB collects the contract terms and weather reports, estimates the chance of a range, and simulates buying YES or NO when the visible order book offers a favorable price after costs. Every balance, position, fill and settlement in the app belongs to a local paper account. [Kalshi’s series and public data example](https://docs.kalshi.com/getting_started/quick_start_market_data)

## Follow one contract through the desk

| View | What to look for |
| --- | --- |
| **Markets** | Select a contract by date and temperature range. Read its posted YES bid and ask, see whether either moved since the previous comparable poll, and inspect actual observations over 15 minutes, one hour or four hours. |
| **Research** | Open a contract. **Decision** gives the current reason, modeled probability, expected costs and eligible size when available. **Evidence** shows sources available at the cutoff. **Order book** shows available price and quantity. **Settlement** shows the venue result and paper payout when known. |
| **Portfolio** | Read net gain or loss for the selected account, available and reserved cash, current marks, trading fees and open exposure by event. The forecast scorecard compares three probability estimates across the same dates. |
| **Activity** | See forward scan receipts, paper orders, fills, pauses and settlements. The forward **Market updates** section shows recent changes where two observations of the same side can be compared. |
| **Worker** | Select the visible account, grant public market collection permission, pause paper entries, review health, configure local time display or enable an optional research provider. |

**Replay and forward are two accounts.** Both begin with $10,000 of paper cash. Replay uses a versioned synthetic case with known forecast records, books and outcomes. Forward begins building its own history when its worker observes actual sources. Selecting an account changes the Research, Portfolio and Activity records on screen. The permission and entry switches determine which forward tasks an online worker may perform. The installation guide covers their defaults. [Setup and first session](SETUP.md)

## From public sources to the screen

The native Rust/Tauri shell starts or reconnects to a private Python worker on loopback. Both the desktop window and browser load the same local HTML, CSS and JavaScript. The worker owns the scheduler, permissions, SQLite databases and private HTTP controls. Closing a client leaves the worker free to keep collecting on an awake host. [Desktop entry point](../desktop/src/main.rs) · [Worker and HTTP routes](../worker/server.py)

| Task | Normal cadence | Output |
| --- | --- | --- |
| Current market display | Approximately every 15 seconds with permission | An active catalog of eligible Central Park listings, the reported YES bid and ask with positive posted size, and receipt-time samples in memory for one hour. |
| Historical market display | On first eligible catalog, as completed minutes arrive, on new listings and after an interruption | Up to 240 real one-minute YES bid and ask closing observations per active contract. A separate worker thread reads the batch history endpoint. |
| Forward assessment | Approximately every five minutes by default, or **Run scan** | A timestamped scan receipt, source health, weather evidence, permitted contract snapshots, modeled probabilities when available, and paper ledger decisions. |
| Contract resolution | During permitted forward scans | Recent official results plus bounded checks on previously observed, closed contracts that still need a final result. |
| Optional analyst review and email | Following a forward scan when each feature is enabled, or manual analyst review | Dated research notes and an owner-only operational mail outbox. |

A live chart point uses the worker’s actual HTTP receipt time. A historical point uses the venue’s end-of-minute timestamp and its separate `yes_bid.close_dollars` and `yes_ask.close_dollars` fields. Prices are converted from dollars to cents without dropping supported subcent precision. Trade prices, midpoints and the venue’s optional synthetic starting candle do not contribute to the lines. Historical and current requests have independent health and retry status. A price can hold steady across many successful polls. Sparse minute history, a missing side, delayed requests and a newly opened contract can all produce gaps. Restarting the worker clears the local 15-second tape and requests the rolling minute window again. [Quote collector](../worker/quotes.py) · [Minute history](../worker/candles.py) · [Kalshi candlestick fields](https://docs.kalshi.com/api-reference/market/batch-get-market-candlesticks)

The chart is a display of posted quotes. A paper trade requires a fresh snapshot of executable **depth** showing the price and quantity on each side. The same chart can therefore show active quotes on a day when every research decision says **Watch**. The display also uses the venue’s reported close, while the strategy observes the earlier of the reported close and its verified station-day cutoff. [Contract and order book checks](../worker/feeds.py)

## Which weather information counts

The collector reads public NOAA National Blend of Models (NBM) short-range station guidance, National Weather Service (NWS) daytime point forecasts, the KNYC station observation and New York Central Park climate bulletins (CLINYC). The station guidance contributes a high-temperature feature when its three-hour samples cover the event day. A sufficiently fresh NWS daytime high is the fallback forecast. An observation measures the temperature at one time. Preliminary and completed-day NWS bulletins receive different labels. These records let a reader see what forecasters and observers knew before a decision. [NWS API documentation](https://www.weather.gov/documentation/services-web-api) · [Weather ingestion and estimation](../worker/weather.py)

Each usable input has a source, an issue or observation time where available, the worker’s first availability time and a content digest. Published and retrieved times are kept separately. An observation with a known observation time and unknown publication time can provide dated context. A newly downloaded older bulletin counts as available when the worker acquired it. Reconstructing an earlier decision cannot use a correction, fee change or outcome first available later.

For a new entry, the collector verifies the active binary contract, the station and civil date, its exact integer Fahrenheit boundaries, payout, supported tick grid, current source hierarchy and trading cutoff. For this implementation the certified GLOBALTEMPERATURE terms and the market’s terms identify The Weather Company for the CLINYC event. If the underlying terms change, the worker requires a compatible rule check before trading that contract. An NWS reported temperature alone cannot settle the account. The venue’s final result and payout fields govern settlement. [Kalshi’s contract terms](https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf) · [Rule and settlement checks](../worker/feeds.py)

### How 30 comparable days become a probability

Suppose the current forecast gives a single temperature in °F. To estimate a probability for a bin, PMB measures how far comparable forecasts previously missed the final venue-aligned temperatures.

1. Select a fresh forecast first available by the decision cutoff, preferring a complete NBM station product. The implemented maximum ages are 42 hours for the NBM feature and 18 hours for the NWS daytime high.
2. Find preceding resolved station days within 45 calendar days of the target’s seasonal position, allowing the window to wrap across New Year. Their verified venue outcomes must have been available when the current decision was made. Conflicting settlement temperatures exclude a day.
3. Match each previous day to a forecast from the same source/version and the same 12-hour issuance-to-event-end band. That forecast had to be available at the corresponding point in the historical day. Record at most one error per day, calculated as actual high minus forecast high.
4. With at least 30 matched days, add each historical error to the current forecast. Count the adjusted temperatures that fall in the contract’s range. Their fraction estimates the probability of YES. PMB computes a two-sided 95% Wilson interval for that empirical proportion.
5. Compute a separate climatology probability from qualifying prior observed temperatures. The ledger requires a valid baseline and probability bounds. It uses the lower confidence bound for a YES purchase or one minus the upper bound for NO. These bounds reflect the observed sample and model assumptions. Forecast regime changes and correlated weather can still produce errors.

Before 30 matched days, the worker shows its available count and a **Watch** reason. The **Evidence for paper entries** panel in the forward Portfolio reports the best-covered open forecast’s sample count and how many open contracts with checked counts reach the minimum. Even a contract that clears this threshold still needs a favorable book, valid terms, verified fees and risk capacity. Thirty calendar days of uptime can yield fewer than 30 matches because issue times, seasons, source versions, venue outcomes and outages affect eligibility. A four-hour price chart supplies no historical forecast or resolved station day. The forward model grows from the records the worker actually collected. [Calibration algorithm](../worker/weather.py) · [Point-in-time feed tests](../tests/test_feeds.py)

## How a paper decision is priced

The full scan joins the model and contract to a two-sided exchange book. It verifies the fee model and applicable series or event changes, enforces fee expiry where supplied, and rejects stale, crossed or unsupported books. Market display can show fractions of a cent. The execution simulator supports whole-cent prices and whole-contract sizes. The collector’s book freshness check is 60 seconds and the ledger enforces a final 120-second ceiling at the decision clock. [Fee schedule](https://kalshi.com/fee-schedule) · [Collector and fee checks](../worker/feeds.py)

Kalshi reports bids for both outcomes. For a $1 binary payout, a NO bid of 48¢ corresponds to a YES ask of 52¢ for the same quantity. PMB reconstructs the price ladders from those complementary bids. A 51¢ ask for 45 contracts and a 52¢ ask for 70 more give a 100-contract buyer an average ask of 51.55¢. Paying 51¢ for all 100 contracts would overstate the return.

For each side, PMB walks the available price levels and evaluates every affordable whole-contract size up to 100. Its conservative expected payout in cents is the applicable probability bound multiplied by 100 and the quantity, rounded down to a whole cent. It subtracts the actual premium across the ladder, rounded taker fees and a 1¢ execution allowance per contract. For a supported quadratic fee, the underlying calculation is `rate × contracts × price × (1 − price)` with price expressed in dollars. The code handles fee precision and rounding across simulated fills. The feasible size with the best positive conservative total qualifies for a paper intent. The allowance is a decision and reserve buffer. Confirmed fill debits use the simulated premium and recorded fees. [Order evaluation and accounting](../worker/ledger.py)

| Decision | Meaning |
| --- | --- |
| **Watch** | A required input, permission-dependent assessment, risk clearance, fresh book or calibrated forecast is unavailable, or new paper entries are paused. Read the reason on the contract. |
| **Pass** | The assessment has enough information to price the trade, but costs use up the conservative expected edge, or account limits leave no affordable size. |
| **Paper buy** | A position on the simulated YES or NO side qualified against the recorded order book. The decision retains the original reference, size and assumptions. |
| **Held** | An existing paper entry remains open, is awaiting a venue result, or is under reconciliation. The strategy normally holds a filled entry through settlement. |
| **Settled** | The worker recorded a compatible final outcome and applied the paper payout. |

**Try the synthetic example.** In **Research → Price change replay → Open case**, the model estimates 60% YES with a lower bound of about 56.0%. At 55¢ for 100 contracts the conservative expected result is −$1.72. At an executable average of 51.55¢ it becomes +$1.72, and the simulator buys. The replay eventually has $46.70 realized gain, $3.50 in total account fees and one other open position. The +$1.72 shown at decision time is an expectation for that size. The $46.70 figure is a realized account result after the synthetic outcome became known. [Replay fixture](../worker/fixtures/central_park_replay.json) · [Replay runner](../worker/replay.py)

## Cash, risk and settlement

| Limit in each paper account | Value |
| --- | --- |
| Starting paper cash | $10,000 |
| Size of one new order | Up to 100 contracts |
| Cost budget for a new position | $100 |
| Gross exposure to one event date, including reserves | $200 |
| Open cost and reserves across the account | $500 |
| Daily marked loss that halts entries | $150 |
| Drawdown from peak marked equity that halts entries | $400 |

A paper intent reserves cash before a fill. The normal forward simulator fills against the recorded visible depth. The ledger also supports partial cumulative fill reports, unknown acknowledgments, cancellation and reconciliation. A repeated receipt or repeated settlement cannot produce an extra charge or payout. If an acknowledgment is uncertain, its outstanding reserve stays in place until reconciliation. A manual pause cancels eligible outstanding quantities and keeps filled positions open. Real-world queue priority, latency and other traders consuming depth can change the fills a funded order would receive. [Ledger](../worker/ledger.py) · [Ledger tests](../tests/test_ledger.py)

**Cash balance** starts from the initial cash, subtracts confirmed purchase premiums and fees, and adds posted payouts. **Available cash** subtracts outstanding reserves from the cash balance. **Marked equity** adds the estimated exit value of open positions to cash. **Net gain or loss** is marked equity minus starting cash. **Realized P&L** includes only the gains or losses posted at settlement. Multiple temperature ranges can share the same event date and the same underlying weather outcome. Multiple partial fills against a single intent count as one filled order.

To mark a held position, the worker walks current executable bids, deducts modeled exit fees and the execution allowance, and values any uncovered quantity at zero. A stale book produces a stale mark of zero. This conservative accounting can create a marked loss and pause entries while a weather outcome remains unresolved. The daily loss baseline follows the UTC ledger day. The drawdown baseline is peak marked equity. Resuming from a risk stop requires reconciled balances and a fresh check within limits.

An official matching YES/NO result pays $1 per winning filled contract and zero for a losing contract. The collector checks the published payout and tracks previously seen closed contracts across restarts. On a missing active-tier record it can check the venue’s historical route. The ledger can represent a documented refund void. The public collector currently sends verified binary results for automatic settlement and records exceptional or scalar payouts for review. A conflicting final value or an unknown paper order can keep the account pending. [Settlement handling](../worker/feeds.py) · [Ledger posting](../worker/ledger.py)

## Analysts and operational messages

A local analyst summarizes dated evidence. Opt-in external providers have specific roles. Gemini summarizes weather changes, Perplexity examines contradictions and missing publication times, and the compatible OmniRoute gateway critiques the thesis. An external request carries the station, event date, cutoff and up to eight eligible weather sources. The worker keeps private venue quotes, positions, cash and order controls outside this packet. Notes display which provider produced them. Paper entries and risk decisions continue to use the deterministic ledger and weather model. [Analyst packet and clients](../worker/research.py)

External requests default to a shared cap of three per UTC day across all providers. Each attempt reserves 25¢ of estimated usage and the worker stops at $15 of monthly reservations. Set the daily cap to zero for local-only notes. A provider bills at its own prices and may enforce additional quotas. An automatic review rotates through eligible open forward contracts. Duplicate evidence can reuse the existing review, and concurrent requests share the worker’s one-review-at-a-time guard.

Optional SMTP sends one owner a generic daily operational digest and incident/recovery updates. The outbox deduplicates messages, limits retries and flags unclear SMTP acknowledgments for owner review. A digest day follows New York time. No SMTP configuration is needed for ordinary practice. [Notification implementation](../worker/notifications.py) · [Provider and email setup](SETUP.md#optional-analysts-and-email)

## Reading performance over time

The **Forecast accuracy** scorecard selects the first assessable pre-close snapshot for each market with a compatible final result. It compares model, quoted market midpoint and climatology probabilities with the same binary outcome using the Brier score, `(probability − outcome)²`. Lower is better. It averages across the contracts on each event date first, then gives each resolved date equal weight. A day with many listed temperature ranges cannot dominate the sample. Fees, fills, realized results and drawdown are assessed separately from the forecast score. [Scorecard calculation](../worker/ledger.py)

Evaluate forward results on distinct dates. Compare model and baseline accuracy, net gains after fees, the largest drawdown, missing-data periods and outcomes under worse fills. Preserve the original evidence cutoff and separate the held-out period from any tuning. The project’s proposed first progression review calls for at least 90 distinct resolved dates, including at least 30 observed prospectively, with an uncertainty bound on net results and explicit execution stress. That is a review criterion, separate from the model’s 30-day calibration gate. Clearing a count does not trigger an operational mode change.

This release has one weather strategy and one public-data venue. **Paper auto trading → Yes** authorizes eligible simulated entries in the forward ledger. Exchange-account authentication, transfers, funded order routing, other venues and brokerage asset classes would require separate engineering and an explicit account-owner decision.
