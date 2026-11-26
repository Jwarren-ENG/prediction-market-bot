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
        self._history_wake = threading.Event()
        self._history_busy = False
        self._history_next = 0.0
        self._history_retry = 0.0
        self._history_failures = 0
        self._scope = None
        self._busy = False
        self._next = 0.0
        self._retry_at = 0.0
        self._failures = 0
        self._clear()

    def _clear(self):
        self._listings = {}
        self._archive = candles.Archive()
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
            self._history_wake.set()

    def _prune(self, now):
        cutoff = now - WINDOW
        for ticker, row in list(self._listings.items()):
            if row.close <= now or row.observed < cutoff or row.observed > now:
                del self._listings[ticker]
                continue
            while row.history and row.history[0][0] < cutoff:
                row.history.popleft()
        self._archive.retain(self._listings, candles.end_minute(now))

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
                if old and old.close == row.close and old.label == row.label:
                    row.history = old.history
                elif old:
                    self._archive.series.pop(ticker, None)
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
            self._history_wake.set()

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

    def _history_scope(self, scope, tickers):
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
                t not in self._listings or self._listings[t].close <= now
                for t in tickers
            ):
                raise CatalogChanged()

    def poll_history(self):
        """Read closed venue minute periods without blocking or updating the live tape."""
        with self._lock:
            scope = self._synchronize()
            now = self._now()
            mono = self._mono()
            if (
                scope[0] != "enabled"
                or self._status != "live"
                or not self._success
                or not 0 <= (now - self._success).total_seconds() <= STALE_AFTER
                or self._history_busy
                or mono < max(self._history_next, self._history_retry)
            ):
                return False
            self._prune(now)
            calls = self._archive.plan(self._listings, candles.end_minute(now), mono)
            if not calls:
                return False
            self._history_busy = True
            self._history_next = mono + INTERVAL
            self._archive.checked = now
            if self._archive.success is None:
                self._archive.status = "loading"
                self._archive.message = (
                    "Retrieving the past four hours of Kalshi minute closes."
                )
        try:
            for request in calls:
                with self._lock:
                    self._history_scope(scope, request.tickers)
                    openings = {
                        t: self._listings[t].opened.timestamp() for t in request.tickers
                    }

                def guard(tickers=request.tickers):
                    self._history_scope(scope, tickers)

                reply = self._fetch(
                    request.url(), max_bytes=8_000_000, permission_guard=guard
                )
                guard()
                if (
                    reply.status != 200
                    or not reply.origin_date
                    or not 0 <= reply.age <= 120
                    or not -3 <= (reply.seen - reply.origin_date).total_seconds() <= 120
                    or reply.seen < now
                    or request.end > candles.end_minute(reply.seen)
                    or request.end
                    > (reply.origin_date + timedelta(seconds=3)).timestamp()
                    or not 0 <= (self._now() - reply.seen).total_seconds() <= 120
                ):
                    raise ValueError("Invalid venue history timestamp.")
                groups, issues = candles.parse(reply.json(), request, openings)
                with self._lock:
                    guard()
                    self._archive.merge(
                        request, groups, issues, self._listings, self._mono()
                    )
                    if groups:
                        self._archive.success = reply.seen
            with self._lock:
                self._gate(scope)
                self._history_failures = 0
                self._history_retry = 0.0
                series = self._archive.series
                count = sum(len(v.points) for v in series.values())
                if any(s.retry_full or s.through is None for s in series.values()):
                    self._archive.status = "partial"
                    self._archive.message = "Some requested contract history was missing or invalid. Available venue closes are shown."
                elif count:
                    self._archive.status = "ready"
                    self._archive.message = "Completed Kalshi one-minute YES quote closes. Minutes without a venue candle are left empty."
                else:
                    self._archive.status = "empty"
                    self._archive.message = (
                        "Kalshi returned no completed minute closes in this window."
                    )
        except feeds.PermissionRevoked:
            pass
        except CatalogChanged:
            with self._lock:
                if self._synchronize() == scope:
                    self._archive.status = (
                        "partial" if self._archive.success else "waiting"
                    )
                    self._archive.message = (
                        "The active catalog changed during the history request."
                    )
        except (feeds.Unavailable, *feeds.SAFE_ERRORS) as error:
            with self._lock:
                if self._synchronize() == scope:
                    self._history_failures += 1
                    if isinstance(error, feeds.Unavailable) and error.code == 429:
                        delay = max(INTERVAL, min(366 * 86400, error.retry or 60))
                        self._archive.status = "rate_limited"
                        self._archive.message = f"Kalshi rate limited historical requests. Next attempt in at least {delay} seconds."
                    else:
                        delay = min(
                            MAX_RETRY_SECONDS,
                            INTERVAL * 2 ** min(self._history_failures - 1, 5),
                        )
                        self._archive.status = "error"
                        self._archive.message = "Kalshi minute history could not be refreshed. Earlier venue closes retain their timestamps."
                    self._history_retry = self._mono() + delay
        finally:
            with self._lock:
                self._history_busy = False
        return True

    def _history_loop(self, stop):
        while not stop.is_set():
            self._history_wake.clear()
            self.poll_history()
            if not stop.is_set():
                self._history_wake.wait(60)

    def run(self, stop):
        """A separate history reader keeps slow backfills off the live sampling clock."""
        history = threading.Thread(
            target=self._history_loop,
            args=(stop,),
            name="public-candle-reader",
            daemon=True,
        )
        history.start()
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
            self._history_wake.set()
            history.join(timeout=1)

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
            archive = self._archive
            history_status, history_message = archive.status, archive.message
            if mode in ("disabled", "offline"):
                history_status, history_message = status, message
            elif status == "empty":
                history_status, history_message = (
                    "empty",
                    "No active Central Park contracts to retrieve.",
                )
            elif history_status in ("ready", "empty") and not self._listings:
                history_status, history_message = (
                    "waiting",
                    "Waiting for a fresh active catalog.",
                )
            elif history_status in ("ready", "empty") and self._listings:
                if any(
                    t not in archive.series or archive.series[t].through is None
                    for t in self._listings
                ):
                    history_status, history_message = (
                        "partial",
                        "Waiting for the newly listed contract history.",
                    )
                elif (
                    archive.success
                    and not 0
                    <= (now - archive.success).total_seconds()
                    <= candles.MISSING_RETRY
                ):
                    history_status, history_message = (
                        "partial",
                        "The historical source has not refreshed recently.",
                    )
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
                            "close_at": weather.iso(row.close),
                            "last_trading_at": weather.iso(row.last_trading)
                            if row.last_trading
                            else None,
                            "observed_at": weather.iso(row.observed),
                            "bid_cents": row.bid if status == "live" else None,
                            "ask_cents": row.ask if status == "live" else None,
                            "candles": archive.view(row.ticker),
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
                "history_status": history_status,
                "history_message": history_message,
                "history_last_checked_at": weather.iso(archive.checked)
                if archive.checked and archive.checked <= now
                else None,
                "history_last_success_at": weather.iso(archive.success)
                if archive.success and archive.success <= now
                else None,
                "history_window_minutes": candles.MINUTES,
                "history_period_seconds": candles.PERIOD,
                "history_source_url": candles.SOURCE,
                "markets": rows,
            }
