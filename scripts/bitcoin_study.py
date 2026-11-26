"""Reproduce a fixed, private BTC-USD spot paper study from completed UTC daily bars."""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

DAY = timedelta(days=1)
EPOCH = date(1970, 1, 1)
START = date(2018, 1, 1)
SPLIT = date(2022, 1, 1)
WARMUP = START - 8 * DAY
SOURCE = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
FEE = Decimal("0.006")
SLIP = Decimal("0.0005")
INITIAL = Decimal(10_000)
CAP = Decimal(500)
FRACTION = Decimal("0.05")
ZERO = Decimal(0)


@dataclass(frozen=True)
class Bar:
    low: Decimal
    high: Decimal
    opening: Decimal
    close: Decimal
    volume: Decimal


def valid_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise TypeError("Expected a numeric price or volume")
    if len(str(value)) > 45:
        raise ValueError("Invalid numeric field length")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid price or volume") from None
    if not n.is_finite() or not 0 <= n <= 10**12 or n.as_tuple().exponent < -18:
        raise ValueError("Invalid price or volume")
    return n


def parse_page(payload, start, stop):
    """Validate the returned pair and retain completed, traded UTC daily bars only."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("pair") != "BTC/USD":
        raise ValueError("Expected Bitstamp BTC/USD data")
    rows = data.get("ohlc")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Unexpected Bitstamp OHLC response")
    found = {}
    first_epoch = (start - EPOCH).days * 86400
    stop_epoch = (stop - EPOCH).days * 86400
    for raw in rows:
        timestamp = raw.get("timestamp") if isinstance(raw, dict) else None
        if (
            not isinstance(timestamp, (int, str))
            or len(str(timestamp)) > 12
            or not str(timestamp).isdecimal()
        ):
            raise ValueError("Invalid OHLC timestamp")
        stamp = int(timestamp)
        if stamp % 86400:
            raise ValueError("Misaligned UTC daily candle")
        if not first_epoch <= stamp < stop_epoch:
            continue
        day = datetime.fromtimestamp(stamp, UTC).date()
        bar = Bar(
            *(
                valid_number(raw.get(k))
                for k in ("low", "high", "open", "close", "volume")
            )
        )
        if not (
            0
            < bar.low
            <= min(bar.opening, bar.close)
            <= max(bar.opening, bar.close)
            <= bar.high
            and bar.volume >= 0
        ):
            raise ValueError(f"Invalid OHLCV on {day}")
        if day in found and found[day] != bar:
            raise ValueError(f"Conflicting duplicate candle on {day}")
        found[day] = bar
    return {d: b for d, b in found.items() if b.volume > 0}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Exchange data endpoint redirected")


def read_page(opener, start, stop):
    # Bitstamp gives end precedence when both timestamps are present. Ask
    # for two extra buckets and clip them deterministically to [start, stop).
    params = urlencode(
        {
            "start": (start - EPOCH).days * 86400,
            "end": (stop - EPOCH).days * 86400,
            "step": 86400,
            "limit": (stop - start).days + 2,
            "exclude_current_candle": "true",
        }
    )
    req = Request(
        SOURCE + "?" + params,
        headers={"Accept": "application/json", "User-Agent": "PMB-private-research/1"},
    )
    for attempt in range(4):
        try:
            with opener.open(req, timeout=20) as response:
                raw = response.read(1_000_001)
                if response.status != 200 or len(raw) > 1_000_000:
                    raise ValueError("Unexpected exchange response or size")
                return parse_page(json.loads(raw, parse_float=Decimal), start, stop)
        except HTTPError as exc:
            exc.close()
            if attempt == 3 or exc.code not in {429, 500, 502, 503, 504}:
                raise
            hint = exc.headers.get("Retry-After")
            if hint and (not hint.isdecimal() or int(hint) > 30):
                raise RuntimeError(
                    "Venue requested a longer wait. Retry the study later."
                ) from exc
            delay = max(float(hint or 0), 2**attempt)
        except (URLError, TimeoutError) as exc:
            if attempt == 3:
                raise RuntimeError(
                    "Venue data unavailable after bounded retries"
                ) from exc
            delay = 2**attempt
        time.sleep(delay)
    raise RuntimeError("Exchange page could not be retrieved")


def fetch_daily(through):
    # Keep headroom under the per-request 1000-period ceiling, including endpoints.
    ctx = ssl.create_default_context()
    certs = Path("/etc/ssl/cert.pem")
    if certs.is_file():
        ctx.load_verify_locations(str(certs))
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ctx))
    bars = {}
    start = WARMUP
    while start <= through:
        stop = min(start + 900 * DAY, through + DAY)
        bars.update(read_page(opener, start, stop))
        start = stop
        if start <= through:
            time.sleep(0.2)
    if through not in bars or START not in bars:
        raise ValueError(
            "Required boundary candle absent. No results reported for the requested window."
        )
    return bars


def signals(bars):
    """Target on the day of the last completed signal bar; gaps restart the 8-day window."""
    window = deque(maxlen=8)
    targets = {}
    previous = None
    for day, bar in sorted(bars.items()):
        if previous is not None and day - previous != DAY:
            window.clear()
        window.append(bar.close)
        if len(window) == 8:
            targets[day] = window[-1] > window[0]
        previous = day
    return targets


def digest(bars):
    h = hashlib.sha256()
    for day, bar in sorted(bars.items()):
        values = (bar.low, bar.high, bar.opening, bar.close, bar.volume)
        fields = [day.isoformat(), *(format(v.normalize(), "f") for v in values)]
        h.update((",".join(fields) + "\n").encode())
    return h.hexdigest()


def ranges(days):
    result = []
    for day in sorted(days):
        if result and result[-1][1] + DAY == day:
            result[-1][1] = day
        else:
            result.append([day, day])
    return [[str(a), str(b)] for a, b in result]


@dataclass(frozen=True)
class Trade:
    side: str
    notional: Decimal  # At modeled execution price, excluding fee.
    fee: Decimal
    adverse: Decimal


@dataclass
class Account:
    cash: Decimal = INITIAL
    btc: Decimal = ZERO

    def rebalance(self, long, open_price, fee, slip):
        if long == bool(self.btc):
            return None
        if long:
            budget = min(CAP, self.cash * FRACTION)
            if budget <= 0:
                return None
            ask = open_price * (1 + slip)
            notional = budget / (1 + fee)
            self.btc = notional / ask
            self.cash -= budget
            return Trade("buy", notional, notional * fee, self.btc * (ask - open_price))
        bid = open_price * (1 - slip)
        notional = self.btc * bid
        adverse = self.btc * (open_price - bid)
        self.cash += notional * (1 - fee)
        self.btc = ZERO
        return Trade("sell", notional, notional * fee, adverse)

    def equity(self, close, fee, slip):
        return self.cash + self.btc * close * (1 - slip) * (1 - fee)


def rounded(x, places=2):
    return float(round(x, places))


class Metrics:
    def __init__(self, start):
        self.start = self.peak = self.end = start
        self.drawdown = self.drawdown_pct = ZERO
        self.turnover = self.fees = self.adverse = ZERO
        self.buys = self.sells = self.in_market = self.observed = 0

    def add(self, equity, trade, held):
        self.end = equity
        self.peak = max(self.peak, equity)
        loss = self.peak - equity
        self.drawdown = max(self.drawdown, loss)
        self.drawdown_pct = max(self.drawdown_pct, loss / self.peak * 100)
        if trade:
            self.buys += int(trade.side == "buy")
            self.sells += int(trade.side == "sell")
            self.turnover += trade.notional
            self.fees += trade.fee
            self.adverse += trade.adverse
        self.in_market += int(held)
        self.observed += 1

    def summary(self):
        return {
            "opening_equity_usd": rounded(self.start),
            "ending_equity_usd": rounded(self.end),
            "net_usd": rounded(self.end - self.start),
            "return_pct": rounded((self.end / self.start - 1) * 100, 3),
            "max_drawdown_usd": rounded(self.drawdown),
            "max_drawdown_pct": rounded(self.drawdown_pct, 3),
            "buy_count": self.buys,
            "sell_count": self.sells,
            "turnover_usd": rounded(self.turnover),
            "turnover_x_initial_equity": rounded(self.turnover / self.start, 3),
            "paid_fees_usd": rounded(self.fees),
            "modeled_adverse_execution_usd": rounded(self.adverse),
            "observed_days": self.observed,
            "days_held_at_close": self.in_market,
        }


def simulate(
    bars,
    targets,
    start,
    through,
    strategy="momentum",
    fee=FEE,
    slip=SLIP,
    extra_delay=0,
):
    """Actual trade times are observed bar opens. A signal cannot use that bar's close."""
    account = Account()
    stats = Metrics(INITIAL)
    years = {}
    long = strategy == "passive"
    day = start
    last_close = None
    while day <= through:
        signal_day = day - (1 + extra_delay) * DAY
        if strategy == "momentum" and signal_day in targets:
            long = targets[signal_day]
        bar = bars.get(day)
        if bar is not None:
            if day.year not in years:
                years[day.year] = Metrics(stats.end)
            trade = account.rebalance(long, bar.opening, fee, slip)
            equity = account.equity(bar.close, fee, slip)
            held = bool(account.btc)
            stats.add(equity, trade, held)
            years[day.year].add(equity, trade, held)
            last_close = bar.close
        day += DAY
    if last_close is None:
        raise ValueError("No observed trades in the requested evaluation period")
    exit_cost = account.btc * last_close * (1 - (1 - slip) * (1 - fee))
    result = {
        **stats.summary(),
        "ending_cash_usd": rounded(account.cash),
        "open_terminal_position": bool(account.btc),
        "terminal_estimated_exit_cost_usd": rounded(exit_cost),
    }
    return result, {year: row.summary() for year, row in years.items()}


def study(bars, through):
    if through < SPLIT:
        raise ValueError(
            "The retrospective holdout requires an end date in 2022 or later"
        )
    targets = signals(bars)
    periods = {
        "continuous": (START, through),
        "exploratory": (START, SPLIT - DAY),
        "holdout": (SPLIT, through),
    }
    observed = {k: v for k, v in bars.items() if WARMUP <= k <= through}
    span = (through - WARMUP).days + 1
    missing = {WARMUP + i * DAY for i in range(span)} - set(observed)
    result = {
        "product": "BTC/USD",
        "venue": "Bitstamp",
        "source": SOURCE,
        "granularity_seconds": 86400,
        "warmup_start_utc": str(WARMUP),
        "start_utc": str(START),
        "holdout_start_utc": str(SPLIT),
        "complete_through_utc": str(through),
        "expected_utc_days_including_warmup": span,
        "observed_daily_bars": len(observed),
        "missing_utc_days": len(missing),
        "missing_date_ranges": ranges(missing),
        "canonical_ohlcv_sha256": digest(observed),
        "base_taker_pct": float(FEE * 100),
        "base_adverse_pct": float(SLIP * 100),
        "periods": {},
        "continuous_calendar_years": {},
        "stress": {},
    }
    for name, (a, b) in periods.items():
        comparisons = {}
        for mode in ("momentum", "passive", "cash"):
            summary, yearly = simulate(bars, targets, a, b, mode)
            comparisons[mode] = summary
            if name == "continuous":
                for year, row in yearly.items():
                    result["continuous_calendar_years"].setdefault(str(year), {})[
                        mode
                    ] = row
        result["periods"][name] = {"start": str(a), "end": str(b), **comparisons}
        if name == "exploratory":
            continue
        stress = {}
        for case, fees, buffer, lag in (
            ("double_cost", FEE * 2, SLIP * 2, 0),
            ("extra_daily_open", FEE, SLIP, 1),
            ("double_cost_and_delay", FEE * 2, SLIP * 2, 1),
            ("frictionless_diagnostic", ZERO, ZERO, 0),
        ):
            stress[case] = simulate(
                bars, targets, a, b, fee=fees, slip=buffer, extra_delay=lag
            )[0]
        result["stress"][name] = stress
    return result


def main():
    last = datetime.now(UTC).date() - DAY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--through",
        type=date.fromisoformat,
        default=last,
        help="last completed UTC date, inclusive (default yesterday)",
    )
    args = parser.parse_args()
    if not SPLIT <= args.through <= last:
        parser.error(f"Choose a completed UTC date between {SPLIT} and {last}.")
    try:
        bars = fetch_daily(args.through)
        out = study(bars, args.through)
        out["retrieved_at_utc"] = datetime.now(UTC).isoformat()
        print(json.dumps(out, indent=2, allow_nan=False))
    except (OSError, RuntimeError, TypeError, ValueError, InvalidOperation) as exc:
        print("Bitcoin study unavailable: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
