"""Display-only monitoring of Kalshi's currently trading 15-minute BTC window."""

from __future__ import annotations

import re
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import feeds, quotes, weather

CATALOG = feeds.API + "/markets?series_ticker=KXBTC15M&status=open&limit=100"
RULES = "https://assets.kalshi.com/contract_terms/CRYPTO.pdf"
INDEX = "https://www.cfbenchmarks.com/data/indices/BRTI"
SITE = "https://kalshi.com/markets/kxbtc15m/btc-15-min/"
INTERVAL = 10
STALE = 2 * INTERVAL
DEPTH = 5
WINDOW = timedelta(minutes=15)
RECENT = timedelta(hours=1)
MAX_SAMPLES = int(WINDOW.total_seconds() / INTERVAL) + 2
TICKER = re.compile(r"(KXBTC15M-[0-9]{2}[A-Z]{3}[0-9]{6})-(00|15|30|45)")
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
BOOK_SIDES = ("yes", "no")


def stamp(value):
    return weather.iso(value) if value else None


def source_time(value, short):
    local = weather.eastern(value)
    zone = "EDT" if local.utcoffset() == timedelta(hours=-4) else "EST"
    month = MONTHS[local.month - 1]
    meridiem = "AM" if local.hour < 12 else "PM"
    return (
        f"{local.hour % 12 or 12}:{local.minute:02d} {meridiem} {zone} on "
        f"{month[:3] if short else month} {local.day}, {local.year}"
    )


def book_url(ticker):
    if not isinstance(ticker, str) or not TICKER.fullmatch(ticker):
        raise ValueError("Unexpected BTC market ticker.")
    return f"{feeds.API}/markets/{ticker}/orderbook?depth={DEPTH}"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise feeds.Unavailable("The Kalshi BTC endpoint redirected.")


def download(url, *, max_bytes, permission_guard):
    # Only a scoped catalog and a book on a validated ticker can be requested.
    prefix = feeds.API + "/markets/"
    suffix = f"/orderbook?depth={DEPTH}"
    if url != CATALOG and not (
        url.startswith(prefix)
        and url.endswith(suffix)
        and TICKER.fullmatch(url[len(prefix) : -len(suffix)])
    ):
        raise ValueError("Unexpected Kalshi BTC endpoint.")
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and Path("/etc/ssl/cert.pem").is_file():
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    client = build_opener(HTTPSHandler(context=ctx), NoRedirects())
    req = Request(
        feeds.checked_url(url),
        headers={
            "User-Agent": "PMB/0.1 (personal BTC market monitor)",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "identity",
        },
    )
    try:
        permission_guard()
        with client.open(req, timeout=6) as response:
            body = response.read(max_bytes + 1)
            when = feeds.clock()
            permission_guard()
            if len(body) > max_bytes:
                raise feeds.Unavailable("Kalshi BTC response exceeds its size limit.")
            return feeds.Reply(
                body,
                when,
                feeds.http_time(response.headers.get("Date")),
                None,
                int(response.headers.get("Age", 0)),
                response.status,
            )
    except HTTPError as exc:
        hint = exc.headers.get("Retry-After", "")
        try:
            retry = (
                int(hint)
                if hint.isdecimal()
                else int((feeds.http_time(hint) - feeds.clock()).total_seconds())
            )
        except (TypeError, ValueError, OverflowError):
            retry = 60
        finally:
            exc.close()
        raise feeds.Unavailable(
            f"Kalshi HTTP {exc.code}", exc.code, max(1, retry)
        ) from None
    except (HTTPException, URLError, OSError, ValueError) as exc:
        raise feeds.Unavailable("Kalshi BTC transport unavailable.") from exc


def target(market):
    value = market.get("floor_strike")
    if value is None:
        return None
    number = quotes._number(value)
    if (
        not 0 < number <= 100_000_000
        or number * 100 != (number * 100).to_integral_value()
    ):
        raise ValueError("Invalid BTC target.")
    subtitle = market.get("yes_sub_title") or ""
    numeric = re.fullmatch(
        r"Target price:\s*\$([\d,]+(?:\.\d{1,2})?)", subtitle, re.IGNORECASE
    )
    if numeric and Decimal(numeric[1].replace(",", "")) != number:
        raise ValueError("Kalshi target fields disagree.")
    return format(number, ".2f")


def select_market(payload, reply):
    rows = payload.get("markets")
    if not isinstance(rows, list) or len(rows) > 100 or payload.get("cursor") != "":
        raise ValueError("Incomplete Kalshi BTC market catalog.")
    found = None
    for m in rows:
        if not isinstance(m, dict):
            raise TypeError("Unexpected market metadata.")
        ticker = m.get("ticker")
        event = m.get("event_ticker")
        if (
            not isinstance(ticker, str)
            or not ticker.startswith("KXBTC15M-")
            or not isinstance(event, str)
            or not event.startswith("KXBTC15M-")
            or m.get("status") != "active"
        ):
            continue
        opened, closed = (
            weather.instant(m["open_time"]),
            weather.instant(m["close_time"]),
        )
        if not opened <= reply.seen < closed:
            continue
        match = TICKER.fullmatch(ticker)
        if not match or event != match[1]:
            raise ValueError("The active BTC contract has an unrecognized identifier.")
        # The venue identifies the Eastern closing quarter in both ticker segments.
        end = weather.eastern(closed)
        expected_event = (
            f"KXBTC15M-{end.year % 100:02d}{MONTHS[end.month - 1][:3].upper()}"
            f"{end.day:02d}{end.hour:02d}{end.minute:02d}"
        )
        updated = weather.instant(m["updated_time"]) if m.get("updated_time") else None
        if (
            closed - opened != WINDOW
            or closed.second
            or closed.microsecond
            or closed.minute % 15
            or match[1] != expected_event
            or match[2] != f"{end.minute:02d}"
            or not opened <= reply.origin_date < closed
            or (updated and updated > reply.seen)
            or m.get("market_type") != "binary"
            or quotes._number(m.get("notional_value_dollars")) != 1
            or m.get("strike_type") not in (None, "greater_or_equal")
            or m.get("result") not in ("", None)
        ):
            raise ValueError("Unverified active BTC contract terms.")
        primary = m.get("rules_primary")
        secondary = m.get("rules_secondary")
        title = m.get("title")
        if not all(
            isinstance(x, str) and 0 < len(x) <= n
            for x, n in ((primary, 1200), (secondary, 3000), (title, 160))
        ):
            raise ValueError("Missing BTC contract rules.")
        condition = (
            "the simple average of the sixty seconds of cf benchmarks' brti before "
        )
        expected = (
            f"if {condition}{source_time(closed, True)} is at least "
            f"{condition}{source_time(opened, False)}, then the market resolves to yes."
        )
        if " ".join(primary.replace("’", "'").lower().split()) != expected.lower():
            raise ValueError("Unrecognized BTC price comparison or observation window.")
        price = target(m)
        if price is not None and m.get("strike_type") != "greater_or_equal":
            raise ValueError("Unverified numeric BTC strike.")
        if found is not None:
            raise ValueError("Ambiguous overlapping BTC windows.")
        found = Contract(
            opened,
            closed,
            {
                "id": ticker,
                "event_id": match[1],
                "title": title,
                "open_at": stamp(opened),
                "close_at": stamp(closed),
                "updated_at": stamp(updated),
                "target_usd": price,
                "comparison": "greater_or_equal",
                "rules_primary": primary,
                "rules_secondary": secondary,
                "catalog_observed_at": stamp(reply.seen),
                "catalog_http_date": stamp(reply.origin_date),
                "market_url": SITE + match[1].lower(),
                "rules_url": RULES,
                "index_url": INDEX,
                "book_url": book_url(ticker),
            },
        )
    return found


def clean_levels(entries):
    if not isinstance(entries, list) or len(entries) > DEPTH:
        raise ValueError("Invalid fixed-point BTC order book.")
    levels = {}
    for item in entries:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("Invalid BTC order book level.")
        raw_price, raw_size = item
        # The published fixed-point schema expresses dollars and contract quantities as strings.
        if not (
            isinstance(raw_price, str)
            and re.fullmatch(r"[01](?:\.[0-9]{1,4})?", raw_price)
            and isinstance(raw_size, str)
            and re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", raw_size)
        ):
            raise ValueError("Invalid BTC quote precision or size.")
        price, size = quotes._number(raw_price) * 100, quotes._number(raw_size)
        if not 0 < price < 100 or size > 1_000_000_000 or price in levels:
            raise ValueError("Invalid or duplicate BTC quote level.")
        levels[price] = size
    return [(p, levels[p]) for p in sorted(levels, reverse=True) if levels[p] > 0]


def display_number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def empty_depth():
    return {side: {"bids": [], "asks": []} for side in BOOK_SIDES}


def parse_book(payload):
    book = payload["orderbook_fp"]
    bids = {side: clean_levels(book[side + "_dollars"]) for side in BOOK_SIDES}
    if bids["yes"] and bids["no"] and bids["yes"][0][0] + bids["no"][0][0] >= 100:
        raise ValueError("Locked or crossed BTC book.")
    depth = empty_depth()
    for side, opposite in (("yes", "no"), ("no", "yes")):
        depth[side]["bids"] = [
            [display_number(p), display_number(n)] for p, n in bids[side]
        ]
        depth[side]["asks"] = [
            [display_number(100 - p), display_number(n)] for p, n in bids[opposite]
        ]
    return depth


def best(depth, include_sizes):
    fields = {}
    for side in BOOK_SIDES:
        for direction in ("bid", "ask"):
            levels = depth[side][direction + "s"]
            fields[f"{side}_{direction}_cents"] = levels[0][0] if levels else None
            if include_sizes:
                fields[f"{side}_{direction}_size"] = levels[0][1] if levels else None
    return fields


@dataclass
class Contract:
    opened: datetime
    closed: datetime
    meta: dict
    seen: datetime | None = None
    http: datetime | None = None
    depth: dict = field(default_factory=empty_depth)
    history: deque = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES))

    def point(self, when, http, depth):
        at = stamp(when)
        if self.history and at <= self.history[-1]["at"]:
            return
        self.history.append(
            {
                "at": at,
                "http_date": stamp(http),
                "kind": "book" if http else "gap",
                **best(depth, False),
            }
        )

    def view(self, now, active):
        cutoff = stamp(now)
        depth = (
            {
                side: {
                    kind: [row.copy() for row in levels]
                    for kind, levels in self.depth[side].items()
                }
                for side in BOOK_SIDES
            }
            if active
            else empty_depth()
        )
        return {
            **self.meta,
            "observed_at": stamp(self.seen) if self.seen and self.seen <= now else None,
            "http_date": stamp(self.http)
            if self.http and self.http <= now + timedelta(seconds=3)
            else None,
            **best(depth, True),
            "depth": depth,
            "history": [
                point.copy() for point in self.history if point["at"] <= cutoff
            ],
        }


class BitcoinContracts:
    """All venue state is in memory. Access returns (enabled|disabled|offline, generation)."""

    def __init__(self, access, lock, *, fetch=None, now=None, monotonic=None):
        self._access = access
        self._lock = lock
        self._fetch = fetch or download
        self._now = now or feeds.clock
        self._mono = monotonic or time.monotonic
        self.wake = threading.Event()
        self._scope = None
        self._busy = False
        self._retry = self._next = 0.0
        self._throttled_until = 0.0
        self._reset()

    def _reset(self):
        self._contracts = {}
        self._current = None
        self._checked = self._success = self._http = None
        self._failures = 0
        self._status = "waiting"
        self._message = "Waiting for a current Kalshi BTC contract."

    def _synchronize(self):
        scope = self._access()
        if scope != self._scope:
            self._reset()
            self._scope = scope
        return scope

    def permission_changed(self):
        with self._lock:
            self._synchronize()
            self.wake.set()

    def _gate(self, expected):
        with self._lock:
            self._check_scope(expected)

    def _check_scope(self, expected):
        if self._synchronize() != expected:
            raise feeds.PermissionRevoked("Public Kalshi permission changed.")

    def _read(self, url, scope, started, limit):
        self._gate(scope)
        reply = self._fetch(
            url, max_bytes=limit, permission_guard=lambda: self._gate(scope)
        )
        self._gate(scope)
        if (
            reply.status != 200
            or not reply.origin_date
            or not 0 <= reply.age <= 5
            or not -3 <= (reply.seen - reply.origin_date).total_seconds() <= 5
            or reply.seen < started
            or not 0 <= (self._now() - reply.seen).total_seconds() <= INTERVAL
        ):
            raise ValueError("Stale or undated Kalshi BTC response.")
        return reply

    def _prune(self, now):
        # At most four actually observed earlier quarter-hours plus the current market.
        cutoff = stamp(now)
        self._contracts = {
            key: row
            for key, row in self._contracts.items()
            if row.opened <= now < row.closed + RECENT
            and row.meta["catalog_observed_at"] <= cutoff
        }
        if self._current and (
            self._current not in self._contracts
            or self._contracts[self._current].closed <= now
        ):
            self._current = None

    def _apply_catalog(self, candidate, reply):
        self._prune(self._now())
        if self._success and reply.seen <= self._success:
            raise ValueError("BTC source receipt clock did not advance.")
        if candidate is None or not candidate.opened <= self._now() < candidate.closed:
            self._current = None
            self._status = "empty"
            self._message = "Kalshi returned no active 15-minute BTC window."
            self._success, self._http = reply.seen, reply.origin_date
            self._failures = 0
            return False
        key = candidate.meta["id"]
        old = self._contracts.get(key)
        if (
            old
            and (old.opened, old.closed) == (candidate.opened, candidate.closed)
            and (
                old.meta["target_usd"] is None
                or old.meta["target_usd"] == candidate.meta["target_usd"]
            )
        ):
            old.meta = candidate.meta
        else:
            self._contracts[key] = candidate
        if self._current != key or self._contracts[key].seen is None:
            self._status = "waiting"
            self._message = "Waiting for the active contract's order book."
        self._current = key
        return True

    def _capture(self, scope, started):
        catalog = self._read(CATALOG, scope, started, 500_000)
        candidate = select_market(catalog.json(), catalog)
        with self._lock:
            self._check_scope(scope)
            if not self._apply_catalog(candidate, catalog):
                return
        book = self._read(candidate.meta["book_url"], scope, catalog.seen, 40_000)
        depth = parse_book(book.json())
        with self._lock:
            self._check_scope(scope)
            self._prune(self._now())
            if (
                self._current != candidate.meta["id"]
                or not candidate.opened <= book.seen < candidate.closed
                or not candidate.opened <= book.origin_date < candidate.closed
            ):
                raise ValueError("The 15-minute BTC window ended during this read.")
            row = self._contracts[self._current]
            if row.seen and book.seen <= row.seen:
                raise ValueError("BTC book receipt clock did not advance.")
            row.seen, row.http, row.depth = book.seen, book.origin_date, depth
            row.point(book.seen, book.origin_date, depth)
            self._success, self._http = book.seen, book.origin_date
            self._retry = 0.0
            self._failures = 0
            if row.meta["target_usd"] is None:
                self._status = "partial"
                self._message = "Kalshi has not published the opening BTC target."
            elif not all(depth[side]["bids"] for side in BOOK_SIDES):
                self._status = "partial"
                self._message = (
                    "The BTC book has missing sides. Only posted levels are shown."
                )
            else:
                self._status = "live"
                self._message = (
                    "Fresh posted YES and NO quotes for the active BTC window."
                )

    def _failed(self, error):
        now = self._now()
        self._prune(now)
        self._failures += 1
        if isinstance(error, feeds.Unavailable) and error.code == 429:
            delay = max(INTERVAL, min(366 * 86400, error.retry or 60))
            self._throttled_until = self._mono() + delay
            self._status = "rate_limited"
            self._message = f"Kalshi rate limited the BTC monitor. Retry in at least {delay} seconds."
        else:
            delay = min(300, INTERVAL * 2 ** min(5, self._failures - 1))
            self._status = "error"
            self._message = "A fresh verified BTC market or order book is unavailable."
        self._retry = max(self._retry, self._mono() + delay)
        if self._current:
            self._contracts[self._current].point(now, None, empty_depth())

    def poll(self):
        with self._lock:
            scope = self._synchronize()
            t = self._mono()
            if scope[0] != "enabled" or self._busy or t < max(self._retry, self._next):
                return False
            self._busy = True
            started = self._checked = self._now()
            self._next = t + INTERVAL
        try:
            self._capture(scope, started)
        except feeds.PermissionRevoked:
            pass
        except (feeds.Unavailable, *feeds.SAFE_ERRORS) as exc:
            with self._lock:
                if self._synchronize() == scope:
                    self._failed(exc)
        finally:
            with self._lock:
                self._busy = False
        return True

    def run(self, stop_event):
        while not stop_event.is_set():
            self.wake.clear()
            self.poll()
            with self._lock:
                mode, _ = self._synchronize()
                delay = max(self._retry, self._next) - self._mono()
            if not stop_event.is_set():
                self.wake.wait(max(0.1, min(60, delay)) if mode == "enabled" else 60)

    def state(self):
        with self._lock:
            mode, _ = self._synchronize()
            now = self._now()
            self._prune(now)
            current = self._contracts.get(self._current)
            status, message = self._status, self._message
            if mode == "offline":
                status, message = "offline", "The worker is offline."
            elif mode != "enabled":
                status, message = "disabled", "Public Kalshi market reads are off."
            elif status == "waiting" and self._mono() < self._throttled_until:
                status, message = (
                    "rate_limited",
                    "Waiting for the previous Kalshi rate limit to expire.",
                )
            elif (
                status in ("live", "partial", "empty")
                and self._success
                and not (0 <= (now - self._success).total_seconds() <= STALE)
            ):
                status, message = "stale", "The last successful BTC check is stale."
            elif not current and status in ("live", "partial"):
                status, message = (
                    "waiting",
                    "Waiting for the next 15-minute BTC contract.",
                )
            recent = sorted(
                (row for row in self._contracts.values() if row.closed <= now),
                key=lambda row: row.closed,
                reverse=True,
            )[:4]
            return {
                "status": status,
                "message": message,
                "enabled": mode == "enabled",
                "interval_seconds": INTERVAL,
                "stale_after_seconds": STALE,
                "last_checked_at": stamp(self._checked)
                if self._checked and self._checked <= now
                else None,
                "last_success_at": stamp(self._success)
                if self._success and self._success <= now
                else None,
                "http_date": stamp(self._http)
                if self._http and self._http <= now + timedelta(seconds=3)
                else None,
                "source_url": CATALOG,
                "rules_url": RULES,
                "current": current.view(now, status in ("partial", "live"))
                if current
                else None,
                "recent": [row.view(now, False) for row in recent],
            }
