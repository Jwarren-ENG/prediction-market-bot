"""Venue-reported completed minute closes for a four-hour display window."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from . import feeds, weather

PERIOD = 60
MINUTES = 240
SPAN = PERIOD * MINUTES
SOURCE = feeds.API + "/markets/candlesticks"
MAX_PERIODS = 10_000
MISSING_RETRY = 5 * 60
OVERLAP = 2 * PERIOD


def end_minute(now: datetime) -> int:
    return int(now.timestamp()) // PERIOD * PERIOD


def _close(candle, name):
    values = candle.get(name)
    if not isinstance(values, dict):
        return None, True
    raw = values.get("close_dollars")
    if raw is None or raw == "":
        return None, False
    try:
        price = Decimal(str(raw))
        if not price.is_finite():
            raise ValueError("Non-finite quote close.")
        if (name == "yes_bid" and price == 0) or (name == "yes_ask" and price == 1):
            return None, False
        if not Decimal("0.0001") <= price <= Decimal("0.9999"):
            raise ValueError("Invalid quote close.")
        units = price * 10000
        if units != units.to_integral_value():
            raise ValueError("Unsupported quote precision.")
        cents = price * 100
        return (
            int(cents) if cents == cents.to_integral_value() else float(cents)
        ), False
    except (ValueError, InvalidOperation):
        return None, True


@dataclass
class Series:
    points: dict = field(default_factory=dict)
    through: int | None = None
    retry_full: bool = False
    repair_after: float = 0


@dataclass(frozen=True)
class Request:
    tickers: tuple
    start: int
    end: int

    def url(self):
        return (
            SOURCE
            + "?"
            + urlencode(
                {
                    "market_tickers": ",".join(self.tickers),
                    "start_ts": self.start,
                    "end_ts": self.end,
                    "period_interval": 1,
                }
            )
        )


def parse(payload, request, openings):
    """Drop the inclusive lower boundary, incomplete/out-of-window periods and trade prices."""
    groups = payload.get("markets")
    if not isinstance(groups, list):
        raise TypeError("Invalid venue history response.")
    requested = set(request.tickers)
    data = {}
    seen = set()
    issues = set()
    for group in groups:
        if not isinstance(group, dict):
            raise TypeError("Invalid venue history group.")
        ticker = group.get("market_ticker")
        if not isinstance(ticker, str) or ticker not in requested:
            continue
        if ticker in seen:
            data.pop(ticker, None)
            issues.add(ticker)
            continue
        seen.add(ticker)
        raw = group.get("candlesticks")
        if not isinstance(raw, list) or len(raw) > MINUTES + 2:
            issues.add(ticker)
            continue
        values = {}
        conflicts = set()
        for item in raw:
            if not isinstance(item, dict) or type(item.get("end_period_ts")) is not int:
                issues.add(ticker)
                continue
            stamp = item["end_period_ts"]
            if stamp <= request.start or stamp <= openings[ticker]:
                continue
            if stamp > request.end or stamp % PERIOD:
                issues.add(ticker)
                continue
            bid, bad_bid = _close(item, "yes_bid")
            ask, bad_ask = _close(item, "yes_ask")
            if bad_bid or bad_ask:
                issues.add(ticker)
            if bid is not None and ask is not None and bid >= ask:
                bid = ask = None
                issues.add(ticker)
            point = (bid, ask)
            if stamp in values and values[stamp] != point:
                conflicts.add(stamp)
                issues.add(ticker)
            else:
                values[stamp] = point
        for stamp in conflicts:
            values.pop(stamp, None)
        data[ticker] = values
    return data, issues | (requested - data.keys())


class Archive:
    def __init__(self):
        self.series = {}
        self.status = "waiting"
        self.message = "Waiting for an active public Kalshi catalog."
        self.checked = None
        self.success = None

    def retain(self, tickers, end):
        allowed = set(tickers)
        for ticker in list(self.series):
            if ticker not in allowed:
                del self.series[ticker]
                continue
            points = self.series[ticker].points
            for stamp in list(points):
                if not end - SPAN < stamp <= end:
                    del points[stamp]

    def plan(self, tickers, end, mono):
        groups = {"full": [], "recent": []}
        cutoff = end - SPAN
        incremental = end
        for ticker in sorted(tickers):
            series = self.series.setdefault(ticker, Series())
            if series.retry_full and mono < series.repair_after:
                continue
            if (
                series.retry_full
                or series.through is None
                or series.through <= cutoff
                or series.through > end
            ):
                groups["full"].append(ticker)
            elif series.through < end:
                incremental = min(incremental, max(cutoff, series.through - OVERLAP))
                groups["recent"].append(ticker)
        calls = []
        for name, start in (("full", cutoff), ("recent", incremental)):
            ids = groups[name]
            if not ids:
                continue
            # Kalshi can include candles at both query boundaries.
            periods = 1 + (end - start) // PERIOD
            batch = min(100, MAX_PERIODS // periods)
            for i in range(0, len(ids), batch):
                calls.append(Request(tuple(ids[i : i + batch]), start, end))
        return calls

    def merge(self, request, data, issues, active, mono):
        for ticker in request.tickers:
            if ticker not in active:
                continue
            series = self.series.setdefault(ticker, Series())
            if ticker in data:
                # A successful venue response is authoritative for its requested
                # interval. A minute not returned in it stays a gap.
                series.points = {
                    t: v
                    for t, v in series.points.items()
                    if t <= request.start or t > request.end
                }
                series.points.update(data[ticker])
                series.through = request.end
            series.retry_full = ticker in issues
            if series.retry_full:
                series.repair_after = mono + MISSING_RETRY
        self.retain(active, request.end)

    def view(self, ticker):
        saved = self.series.get(ticker)
        if not saved:
            return []
        return [
            {
                "at": weather.iso(datetime.fromtimestamp(stamp, weather.UTC)),
                "bid_cents": bid,
                "ask_cents": ask,
            }
            for stamp, (bid, ask) in sorted(saved.points.items())
        ]
