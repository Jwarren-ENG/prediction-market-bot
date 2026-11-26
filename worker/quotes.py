"""Bounded, display-only public YES quote history for the private KXHIGHNY desk."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import candles, feeds, weather

INTERVAL = 15
WINDOW = timedelta(hours=1)
STALE_AFTER = 2 * INTERVAL
MAX_POINTS = int(WINDOW.total_seconds() / INTERVAL) + 1
MAX_MARKETS = 100
MAX_RETRY_SECONDS = 300


def _number(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TypeError("Missing numeric quote field.")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Invalid numeric quote field.")
    return number


def _side(market, side):
    """A positive posted size is required. Prices use dollars, including sub-cent ticks."""
    try:
        raw = market.get("yes_" + side + "_dollars")
        size = _number(market.get("yes_" + side + "_size_fp"))
        if not 0 <= size <= 1_000_000_000:
            raise ValueError("Invalid posted size.")
        if size == 0:
            return None, False
        price = _number(raw)
        if (
            not Decimal("0.0001") <= price <= Decimal("0.9999")
            or price * 10000 != (price * 10000).to_integral_value()
        ):
            raise ValueError("Invalid posted price.")
        cents = price * 100
        return (
            int(cents) if cents == cents.to_integral_value() else float(cents)
        ), False
    except (InvalidOperation, ValueError, TypeError):
        return None, True


@dataclass
class Listing:
    ticker: str
    label: str
    day: str
    opened: datetime
    close: datetime
    last_trading: datetime | None
    observed: datetime
    bid: int | float | None
    ask: int | float | None
    history: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))


def parse_catalog(payload, seen):
    if (
        not isinstance(payload.get("markets"), list)
        or len(payload["markets"]) > MAX_MARKETS
    ):
        raise ValueError("Unexpected public market catalog.")
    if payload.get("cursor") != "":
        raise ValueError("The public series catalog was incomplete.")
    listings = {}
    issue_count = 0
    seen_ids = set()
    for market in payload["markets"]:
        if not isinstance(market, dict):
            raise TypeError("Invalid entry in the public market catalog.")
        ticker = market.get("ticker")
        match = feeds.TICKER.fullmatch(ticker) if isinstance(ticker, str) else None
        if not match or match[1] != market.get("event_ticker"):
            continue
        if ticker in seen_ids:
            raise ValueError("Duplicate listing in the public market catalog.")
        seen_ids.add(ticker)
        if market.get("status") != "active":
            continue
        try:
            if (
                market.get("market_type") != "binary"
                or _number(market.get("notional_value_dollars")) != 1
            ):
                continue
            if "maximum temperature recorded at new york city (clinyc)" not in " ".join(
                str(market.get("rules_primary", "")).lower().split()
            ):
                continue
            day, last_trading, _, label, _, _, rule_issues = feeds.rule_data(
                market, terms_ok=False
            )
            if "Exceptional trading cutoff unverified." in rule_issues:
                last_trading = None
            close = weather.instant(market["close_time"])
            opened = weather.instant(market["open_time"])
            if not opened <= seen < close:
                continue
            if (
                market.get("updated_time")
                and weather.instant(market["updated_time"]) > seen
            ):
                raise ValueError("Market metadata is from the future.")
        except feeds.SAFE_ERRORS:
            issue_count += 1
            continue
        bid, bad_bid = _side(market, "bid")
        ask, bad_ask = _side(market, "ask")
        if bid is not None and ask is not None and bid >= ask:
            bid = ask = None
            bad_bid = bad_ask = True
        issue_count += int(bad_bid or bad_ask)
        listings[ticker] = Listing(
            ticker, label, str(day), opened, close, last_trading, seen, bid, ask
        )
    if issue_count and not listings:
        raise ValueError("No readable active listing in the public catalog.")
    return listings, issue_count


class CatalogChanged(Exception):
    """A scoped market disappeared before its venue backfill could commit."""


class HistoryReader:
    def __init__(self, window):
        self.archive = candles.Archive(window)
        self.wake = threading.Event()
        self.busy = False
        self.next_at = 0.0
        self.retry_at = 0.0
        self.failures = 0

    def failed(self, error, mono):
        self.failures += 1
        if isinstance(error, feeds.Unavailable) and error.code == 429:
            delay = max(INTERVAL, min(366 * 86400, error.retry or 60))
            self.archive.status = "rate_limited"
            self.archive.message = f"Kalshi rate limited historical requests. Next attempt in at least {delay} seconds."
        else:
            delay = min(MAX_RETRY_SECONDS, INTERVAL * 2 ** min(self.failures - 1, 5))
            self.archive.status = "error"
            self.archive.message = f"Kalshi {self.archive.window.label} history could not be refreshed. Earlier venue closes retain their timestamps."
        self.retry_at = mono + delay

    def metadata(self, prefix, now, listings, live_status, live_message):
        archive = self.archive
        window = archive.window
        status, message = archive.status, archive.message
        end = window.completed_at(now)
        eligible = [t for t, row in listings.items() if row.opened.timestamp() < end]
        if live_status in ("disabled", "offline"):
            status, message = live_status, live_message
        elif live_status == "empty":
            status, message = "empty", "No active Central Park contracts to retrieve."
        elif listings and not eligible:
            status, message = (
                "empty",
                f"No active listing has completed a {window.label} period since opening.",
            )
        elif status in ("ready", "empty"):
            if not listings:
                status, message = "waiting", "Waiting for a fresh active catalog."
            elif any(
                t not in archive.series or archive.series[t].through is None
                for t in eligible
            ):
                status, message = (
                    "partial",
                    "Waiting for the newly listed contract history.",
                )
            elif archive.success:
                age = (now - archive.success).total_seconds()
                max_age = max(candles.MISSING_RETRY, window.period + STALE_AFTER)
                past_due = now.timestamp() - end > candles.MISSING_RETRY and any(
                    archive.series[t].through < end for t in eligible
                )
                if not 0 <= age <= max_age or past_due:
                    status, message = (
                        "partial",
                        "The historical source has not refreshed recently.",
                    )
        return {
            prefix + "_status": status,
            prefix + "_message": message,
            prefix + "_last_checked_at": weather.iso(archive.checked)
            if archive.checked and archive.checked <= now
            else None,
            prefix + "_last_success_at": weather.iso(archive.success)
            if archive.success and archive.success <= now
            else None,
            prefix + "_window_minutes": window.span // 60,
            prefix + "_period_seconds": window.period,
            prefix + "_source_url": candles.SOURCE,
        }


class QuoteTape:
    """Guard all licensed data with the same short lock used to change owner permission."""

    def __init__(self, access, lock, *, fetch=None, now=None, monotonic=None):
        # access() returns (offline|disabled|enabled, permission generation).
        self._access = access
        self._lock = lock
        self._fetch = fetch or feeds.download
        self._now = now or feeds.clock
        self._mono = monotonic or time.monotonic
        self.wake = threading.Event()
        self._readers = {
            window: HistoryReader(window) for window in (candles.MINUTE, candles.HOURLY)
        }
        self._scope = None
        self._busy = False
        self._next = 0.0
        self._retry_at = 0.0
        self._failures = 0
        self._clear()

    def _clear(self):
        self._listings = {}
        for reader in self._readers.values():
            reader.archive.clear()
        self._checked = None
        self._success = None
        self._http_date = None
        self._status = "waiting"
        self._message = "Waiting for the first public Kalshi quote sample."

    def _synchronize(self):
        scope = self._access()
        if scope != self._scope:
            self._clear()
            self._scope = scope
        return scope

    def permission_changed(self):
        with self._lock:
            self._synchronize()
            self.wake.set()
            for reader in self._readers.values():
                reader.wake.set()

    def _prune(self, now):
        cutoff = now - WINDOW
        for ticker, row in list(self._listings.items()):
            if row.close <= now or row.observed < cutoff or row.observed > now:
                del self._listings[ticker]
                continue
            while row.history and row.history[0][0] < cutoff:
                row.history.popleft()
        for window, reader in self._readers.items():
            reader.archive.retain(self._listings, window.completed_at(now))

    def _gate(self, expected):
        with self._lock:
            if self._synchronize() != expected:
                raise feeds.PermissionRevoked("Public quote permission changed.")

    def _capture(self, scope, started):
        self._gate(scope)
        reply = self._fetch(
            feeds.OPEN, max_bytes=1_000_000, permission_guard=lambda: self._gate(scope)
        )
        self._gate(scope)
        received = reply.seen
        if (
            reply.status != 200
            or not reply.origin_date
            or reply.age < 0
            or reply.age > 5
            or not -3 <= (received - reply.origin_date).total_seconds() <= 5
            or received < started
            or not 0 <= (self._now() - received).total_seconds() <= INTERVAL
        ):
            raise ValueError("The venue returned a stale or undated snapshot.")
        rows, issues = parse_catalog(reply.json(), received)
        with self._lock:
            self._gate(scope)
            if self._success and received <= self._success:
                raise ValueError("The quote receipt clock did not advance.")
            for ticker, row in rows.items():
                old = self._listings.get(ticker)
                if old and (old.opened, old.close, old.label) == (
                    row.opened,
                    row.close,
                    row.label,
                ):
                    row.history = old.history
                elif old:
                    for reader in self._readers.values():
                        reader.archive.series.pop(ticker, None)
                row.history.append((received, row.bid, row.ask))
            self._listings = rows  # Complete catalog removes disappeared contracts.
            self._success = received
            self._http_date = reply.origin_date
            self._prune(self._now())
            self._failures = 0
            self._retry_at = 0.0
            self._status = "live" if rows else "empty"
            if not rows:
                self._message = "Kalshi returned no active Central Park contracts."
            elif issues:
                self._message = f"Public quote snapshot received. {issues} listing(s) had incomplete or invalid fields."
            else:
                self._message = "Latest posted YES bids and asks from Kalshi."
            for reader in self._readers.values():
                reader.wake.set()

    def _failed(self, scope, error):
        with self._lock:
            if self._synchronize() != scope:
                return
            now = self._now()
            self._prune(now)
            self._failures += 1
            limited = isinstance(error, feeds.Unavailable) and error.code == 429
            if limited:
                delay = max(INTERVAL, min(366 * 86400, error.retry or 60))
                self._status = "rate_limited"
                self._message = f"Kalshi rate limited quote requests. Next attempt in at least {delay} seconds."
            else:
                delay = min(
                    MAX_RETRY_SECONDS, INTERVAL * 2 ** min(self._failures - 1, 5)
                )
                self._status = "error"
                self._message = (
                    f"Kalshi quote request failed (HTTP {error.code})."
                    if isinstance(error, feeds.Unavailable) and error.code
                    else "A fresh, complete Kalshi quote snapshot is unavailable."
                )
            self._retry_at = max(self._retry_at, self._mono() + delay)
            for row in self._listings.values():
                if not row.history or now > row.history[-1][0]:
                    row.history.append((now, None, None))

    def poll(self):
        """Attempt one due read. Simultaneous triggers share a single in-flight request."""
        with self._lock:
            scope = self._synchronize()
            current = self._mono()
            if (
                scope[0] != "enabled"
                or self._busy
                or current < max(self._next, self._retry_at)
            ):
                return False
            self._busy = True
            started = self._now()
            self._checked = started
            self._next = current + INTERVAL
        try:
            self._capture(scope, started)
        except feeds.PermissionRevoked:
            pass  # Scope synchronization has already discarded the old tape.
        except (feeds.Unavailable, *feeds.SAFE_ERRORS) as exc:
            self._failed(scope, exc)
        finally:
            with self._lock:
                self._busy = False
        return True

    def _history_scope(self, scope, tickers, lifetimes=None):
        with self._lock:
            self._gate(scope)
            now = self._now()
            if (
                self._status != "live"
                or not self._success
                or not 0 <= (now - self._success).total_seconds() <= STALE_AFTER
            ):
                raise CatalogChanged()
            if any(
                t not in self._listings
                or self._listings[t].close <= now
                or (
                    lifetimes is not None
                    and (self._listings[t].opened, self._listings[t].close)
                    != lifetimes[t]
                )
                for t in tickers
            ):
                raise CatalogChanged()

    def poll_history(self, window=candles.MINUTE):
        """Fetch completed venue periods for this resolution, independently of all other readers."""
        reader = self._readers[window]
        archive = reader.archive
        with self._lock:
            scope = self._synchronize()
            now = self._now()
            mono = self._mono()
            if (
                scope[0] != "enabled"
                or self._status != "live"
                or not self._success
                or not 0 <= (now - self._success).total_seconds() <= STALE_AFTER
                or reader.busy
                or mono < max(reader.next_at, reader.retry_at)
            ):
                return False
            self._prune(now)
            openings = {t: row.opened.timestamp() for t, row in self._listings.items()}
            calls = archive.plan(
                self._listings, window.completed_at(now), mono, openings=openings
            )
            if not calls:
                return False
            reader.busy = True
            reader.next_at = mono + INTERVAL
            archive.checked = now
            if archive.success is None:
                archive.status = "loading"
                archive.message = (
                    f"Retrieving completed Kalshi {window.label} quote closes."
                )
        try:
            for request in calls:
                with self._lock:
                    self._history_scope(scope, request.tickers)
                    lifetimes = {
                        t: (self._listings[t].opened, self._listings[t].close)
                        for t in request.tickers
                    }
                    openings = {
                        t: opened.timestamp() for t, (opened, _) in lifetimes.items()
                    }

                def guard(tickers=request.tickers, expected=lifetimes):
                    self._history_scope(scope, tickers, expected)

                reply = self._fetch(
                    request.url(), max_bytes=8_000_000, permission_guard=guard
                )
                guard()
                max_http_age = max(120, window.period + 3)
                if (
                    reply.status != 200
                    or not reply.origin_date
                    or not 0 <= reply.age <= max_http_age
                    or not -3
                    <= (reply.seen - reply.origin_date).total_seconds()
                    <= max_http_age
                    or reply.seen < now
                    or request.end > window.completed_at(reply.seen)
                    or request.end
                    > (reply.origin_date + timedelta(seconds=3)).timestamp()
                    or not 0 <= (self._now() - reply.seen).total_seconds() <= 120
                ):
                    raise ValueError("Invalid venue history timestamp.")
                groups, issues = candles.parse(reply.json(), request, openings)
                with self._lock:
                    guard()
                    archive.merge(request, groups, issues, self._listings, self._mono())
                    if groups:
                        archive.success = reply.seen
            with self._lock:
                self._gate(scope)
                reader.failures = 0
                reader.retry_at = 0.0
                series = archive.series.values()
                if any(s.retry_full or s.through is None for s in series):
                    archive.status = "partial"
                    archive.message = "Some requested contract history was missing or invalid. Available venue closes are shown."
                elif any(s.points for s in series):
                    archive.status = "ready"
                    archive.message = f"Completed Kalshi {window.label} YES bid and ask closes. Gaps reflect periods without a venue candle."
                else:
                    archive.status = "empty"
                    archive.message = f"Kalshi returned no completed {window.label} closes in this window."
        except feeds.PermissionRevoked:
            pass
        except CatalogChanged:
            with self._lock:
                if self._synchronize() == scope:
                    archive.status = "partial" if archive.success else "waiting"
                    archive.message = (
                        "The active catalog changed during the history request."
                    )
        except (feeds.Unavailable, *feeds.SAFE_ERRORS) as error:
            with self._lock:
                if self._synchronize() == scope:
                    reader.failed(error, self._mono())
        finally:
            with self._lock:
                reader.busy = False
        return True

    def _history_loop(self, stop, window):
        wake = self._readers[window].wake
        while not stop.is_set():
            wake.clear()
            self.poll_history(window)
            if not stop.is_set():
                wake.wait(60)

    def run(self, stop):
        """Independent history readers keep slow backfills off the live sampling clock."""
        threads = [
            threading.Thread(
                target=self._history_loop,
                args=(stop, window),
                name=f"public-{window.label}-candles",
                daemon=True,
            )
            for window in self._readers
        ]
        for thread in threads:
            thread.start()
        try:
            while not stop.is_set():
                self.wake.clear()
                self.poll()
                with self._lock:
                    mode, _ = self._synchronize()
                    remaining = max(self._next, self._retry_at) - self._mono()
                if not stop.is_set():
                    self.wake.wait(
                        max(0.1, min(60, remaining)) if mode == "enabled" else 60
                    )
        finally:
            for reader in self._readers.values():
                reader.wake.set()
            for thread in threads:
                thread.join(timeout=1)

    def state(self):
        with self._lock:
            mode, _ = self._synchronize()
            now = self._now()
            self._prune(now)
            status, message = self._status, self._message
            if mode == "offline":
                status, message = "offline", "The worker is offline."
            elif mode == "disabled":
                status, message = "disabled", "Public Kalshi quote access is off."
            elif (
                status in ("live", "empty")
                and self._success
                and not 0 <= (now - self._success).total_seconds() <= STALE_AFTER
            ):
                status, message = (
                    "stale",
                    "The last successful Kalshi quote sample is stale.",
                )
            elif status == "live" and not self._listings:
                status, message = (
                    "empty",
                    "All contracts in the last snapshot reached their trading cutoff.",
                )
            minute = self._readers[candles.MINUTE]
            hourly = self._readers[candles.HOURLY]
            rows = []
            if mode == "enabled":
                for row in sorted(
                    self._listings.values(), key=lambda item: (item.day, item.ticker)
                ):
                    rows.append(
                        {
                            "id": row.ticker,
                            "label": row.label,
                            "event_date": row.day,
                            "open_at": weather.iso(row.opened),
                            "close_at": weather.iso(row.close),
                            "last_trading_at": weather.iso(row.last_trading)
                            if row.last_trading
                            else None,
                            "observed_at": weather.iso(row.observed),
                            "bid_cents": row.bid if status == "live" else None,
                            "ask_cents": row.ask if status == "live" else None,
                            "candles": minute.archive.view(row.ticker),
                            "hourly_candles": hourly.archive.view(row.ticker),
                            "history": [
                                {
                                    "at": weather.iso(at),
                                    "bid_cents": bid,
                                    "ask_cents": ask,
                                }
                                for at, bid, ask in row.history
                                if at <= now
                            ],
                        }
                    )
            return {
                "status": status,
                "message": message,
                "interval_seconds": INTERVAL,
                "last_success_at": (
                    weather.iso(self._success)
                    if self._success and self._success <= now
                    else None
                ),
                "last_checked_at": (
                    weather.iso(self._checked)
                    if self._checked and self._checked <= now
                    else None
                ),
                "http_date": weather.iso(self._http_date)
                if self._http_date and self._http_date <= now + timedelta(seconds=3)
                else None,
                "source_url": feeds.OPEN,
                **minute.metadata("history", now, self._listings, status, message),
                **hourly.metadata(
                    "hourly_history", now, self._listings, status, message
                ),
                "markets": rows,
            }
