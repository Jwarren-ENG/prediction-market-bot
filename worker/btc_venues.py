"""Read-only BTC contract sources, market fees and official binary outcomes."""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import timedelta
from decimal import Decimal

from . import bitcoin_contracts as kalshi
from . import feeds, quotes, weather

US_API = "https://gateway.polymarket.us"
US_RULES = "https://www.polymarketexchange.com/files/product-terms/CPC.pdf"
US_FEES = "https://docs.polymarket.us/fees"
US_SITE = "https://polymarket.us/event/"
SLUG = re.compile(r"cpc-btc-updown-(5m|1h)-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}z")
HORIZONS = {5: "5m", 60: "1h"}
REVIEWED_POLY_RATES = {Decimal(v) for v in ("0.11", "0.10", "0.09", "0.0695")}
SEEN_FEE_SCHEDULE = weather.instant("2026-11-26T02:45:00Z")


def us_catalog(horizon):
    return f"{US_API}/v2/tags/bitcoin-{horizon}/events?active=true&closed=false&limit=8"


def us_market_url(slug, resource):
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ValueError("Unexpected US Bitcoin market slug.")
    if resource == "metadata":
        return US_API + "/v1/market/slug/" + slug
    if resource not in ("book", "settlement"):
        raise ValueError("Unexpected US public market resource.")
    return f"{US_API}/v1/markets/{slug}/{resource}"


def us_allowed(url):
    if url in (us_catalog("5m"), us_catalog("1h")):
        return True
    return bool(
        re.fullmatch(
            re.escape(US_API)
            + r"/v1/(?:market/slug/(?:"
            + SLUG.pattern
            + r")|markets/(?:"
            + SLUG.pattern
            + r")/(?:book|settlement))",
            url,
        )
    )


def us_download(url, *, max_bytes, permission_guard):
    return kalshi.official_get(
        url, us_allowed, max_bytes=max_bytes, permission_guard=permission_guard
    )


def cash(field):
    if not isinstance(field, dict) or field.get("currency") != "USD":
        raise ValueError("Missing USD market value.")
    raw = field["value"]
    if not isinstance(raw, str) or not re.fullmatch(
        r"[0-9]{1,10}(?:\.[0-9]{1,4})?", raw
    ):
        raise ValueError("Invalid USD market value.")
    return quotes._number(raw)


def rule_time(t):
    local = weather.eastern(t)
    ampm = "AM" if local.hour < 12 else "PM"
    month = kalshi.MONTHS[local.month - 1][:3]
    return f"{local.hour % 12 or 12}:{local.minute:02d}{ampm} ET on {month} {local.day}, {local.year}"


def us_identity(m, horizon):
    """Bind the instrument, reference index, UP side, UTC slug and economic window."""
    slug = m["slug"]
    t = m["assetPriceTerms"]
    opened, closed = weather.instant(t["windowStart"]), weather.instant(t["windowEnd"])
    minutes = 5 if horizon == "5m" else 60
    expected = "btc-updown-" + horizon + "-" + opened.strftime("%Y-%m-%d-%H%M") + "z"
    if (
        horizon not in HORIZONS.values()
        or slug != "cpc-" + expected
        or opened.minute % minutes
        or opened.second
        or opened.microsecond
        or closed - opened != timedelta(minutes=minutes)
        or m.get("category") != "crypto"
        or m.get("contractTermsUrl") != US_RULES
        or t.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
        or t["asset"].get("assetClass") != "ASSET_CLASS_CRYPTO"
        or t["asset"].get("symbol") != "btc"
        or t.get("indexSymbol") != "BRTI"
        or t.get("horizon") != horizon
        or t.get("priceDecimals") != 2
    ):
        raise ValueError("Unrecognized US BTC market identity, index or window.")
    sides = m.get("marketSides")
    if (
        not isinstance(sides, list)
        or len(sides) != 2
        or not all(
            item.get("identifier") == slug
            and item.get("marketSideType") == "MARKET_SIDE_TYPE_INSTRUMENT"
            and str(item.get("marketId")) == m.get("id")
            and type(item.get("long")) is bool
            for item in sides
        )
    ):
        raise ValueError("Unknown US BTC instrument sides.")
    mapping = {item["long"]: item.get("description") for item in sides}
    if mapping != {True: "Yes", False: "No"} or json.loads(m["outcomes"]) != [
        "Yes",
        "No",
    ]:
        raise ValueError("Unverified US BTC YES and NO sides.")
    rules = m.get("description")
    if not isinstance(rules, str) or not 0 < len(rules) <= 4000:
        raise ValueError("Missing US BTC contract rules.")
    normalized = " ".join(rules.lower().replace("’", "'").split())
    condition = (
        f"this market will settle to up if the bitcoin price at {rule_time(closed)} "
        f"is greater than or equal to the price at {rule_time(opened)}. "
        "otherwise, this market will settle to down."
    ).lower()
    if not normalized.startswith(condition) or not all(
        piece in normalized
        for piece in (
            "outcome sourced from cf benchmarks' bitcoin real-time index (brti)",
            "last minute before each of those two times, 60 brti prices are collected.",
            (
                "official value for that time is the simple average of these prices, "
                "rounded to the nearest 2 decimal places."
            ),
        )
    ):
        raise ValueError("The US BTC comparison or settlement source changed.")
    return opened, closed, expected


def us_levels(rows):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Invalid US depth.")
    levels = {}
    for row in rows:
        p = cash(row["px"]) * 100
        q = row["qty"]
        if not isinstance(q, str) or not re.fullmatch(
            r"[0-9]{1,10}(?:\.[0-9]{1,4})?", q
        ):
            raise ValueError("Invalid US quoted size.")
        size = quotes._number(q)
        if not 0 < p < 100 or size > 1_000_000_000 or p in levels:
            raise ValueError("Invalid or duplicate US price level.")
        levels[p] = size
    return {p: q for p, q in levels.items() if q}


class PolymarketContracts(kalshi.BitcoinContracts):
    """One US product and independent history, sharing only the owner's US consent."""

    venue_name = "Polymarket US"
    rules_url = US_RULES
    max_http_age = kalshi.STALE
    freshness_error = kalshi.StaleBook
    separate_fee_reads = False

    def __init__(
        self, access, lock, *, interval_minutes=5, fetch=None, now=None, monotonic=None
    ):
        if type(interval_minutes) is not int or interval_minutes not in HORIZONS:
            raise ValueError("Unsupported US Bitcoin interval.")
        self.window_minutes = interval_minutes
        self.horizon = HORIZONS[interval_minutes]
        self.catalog_url = us_catalog(self.horizon)
        super().__init__(
            access, lock, fetch=fetch or us_download, now=now, monotonic=monotonic
        )

    def select_market(self, payload, reply):
        rows = payload.get("events")
        if not isinstance(rows, list) or len(rows) >= 8:
            raise ValueError("Missing or incomplete US Bitcoin catalog.")
        chosen = None
        for e in rows:
            if (
                e.get("seriesSlug") != "btc-updown-" + self.horizon
                or e.get("category") != "crypto"
            ):
                continue
            if (
                e.get("active") is not True
                or e.get("closed") is not False
                or e.get("archived") is not False
            ):
                continue
            start, end = weather.instant(e["startDate"]), weather.instant(e["endDate"])
            if not start <= reply.seen < end:
                continue
            markets = e.get("markets")
            if not isinstance(markets, list) or len(markets) != 1:
                raise ValueError("Ambiguous US event instrument.")
            m = markets[0]
            if (
                m.get("status") != "MARKET_STATUS_OPEN"
                or m.get("active") is not True
                or m.get("closed") is not False
                or m.get("archived") is not False
            ):
                continue
            opened, closed, event = us_identity(m, self.horizon)
            updated = weather.instant(m["updatedAt"])
            if (
                (opened, closed) != (start, end)
                or e.get("slug") != event
                or e.get("ticker") != event
                or not opened <= reply.origin_date < closed
                or updated > reply.seen
                or weather.instant(e["updatedAt"]) > reply.seen
                or not all(x.get("tradable") is True for x in m["marketSides"])
                or not isinstance(e.get("title"), str)
                or not 0 < len(e["title"]) <= 160
            ):
                raise ValueError("US market and event timing or sides disagree.")
            ref = m["assetPriceTerms"].get("priceToBeat")
            price = cash(ref) if ref is not None else None
            if price is not None and (
                not 0 < price <= 100_000_000
                or price * 100 != (price * 100).to_integral_value()
            ):
                raise ValueError("Invalid US opening target.")
            rate = None
            tick = quotes._number(m["orderPriceMinTickSize"]) * 100
            minimum = quotes._number(m["minimumTradeQty"])
            if (
                tick not in (Decimal("0.01"), Decimal("0.1"), Decimal(1))
                or not 0 < minimum <= 1
            ):
                raise ValueError("Unverified US market tick or minimum quantity.")
            if m.get("feeCoefficient") is not None and reply.seen >= SEEN_FEE_SCHEDULE:
                try:
                    coefficient = quotes._number(m["feeCoefficient"])
                except feeds.SAFE_ERRORS:
                    coefficient = None
                if coefficient in REVIEWED_POLY_RATES:
                    rate = format(coefficient, "f")
            if chosen is not None:
                raise ValueError("Multiple active US BTC windows.")
            chosen = kalshi.Contract(
                opened,
                closed,
                {
                    "id": m["slug"],
                    "venue": "polymarket_us",
                    "interval_minutes": self.window_minutes,
                    "event_id": event,
                    "title": e["title"],
                    "open_at": kalshi.stamp(opened),
                    "close_at": kalshi.stamp(closed),
                    "updated_at": kalshi.stamp(updated),
                    "target_usd": format(price, ".2f") if price is not None else None,
                    "comparison": "greater_or_equal",
                    "rules_primary": m["description"],
                    "tick_size_cents": kalshi.display_number(tick),
                    "minimum_contracts": kalshi.display_number(minimum),
                    "rules_secondary": "The US contract uses the published BRTI average at each window boundary.",
                    "rules_verified": True,
                    "rules_url": US_RULES,
                    "index_url": kalshi.INDEX,
                    "market_url": US_SITE + event,
                    "book_url": us_market_url(m["slug"], "book"),
                    "catalog_observed_at": kalshi.stamp(reply.seen),
                    "catalog_http_date": kalshi.stamp(reply.origin_date),
                    "source_time_kind": "venue_book_transact_time",
                    "fee_rate": rate,
                    "fee_source": US_FEES,
                    "fee_market_url": us_market_url(m["slug"], "metadata"),
                    "fee_verified_at": kalshi.stamp(reply.seen) if rate else None,
                    "fee_valid_until": kalshi.stamp(
                        min(closed, reply.seen + timedelta(seconds=kalshi.STALE))
                    )
                    if rate
                    else None,
                },
                history=deque(maxlen=self.window_minutes * 60 // kalshi.INTERVAL + 2),
            )
        return chosen

    @staticmethod
    def read_depth(candidate, reply):
        b = reply.json()["marketData"]
        if (
            b.get("marketSlug") != candidate.meta["id"]
            or b.get("state") != "MARKET_STATE_OPEN"
        ):
            raise ValueError(
                "US market book is closed or belongs to another instrument."
            )
        source = weather.instant(b["transactTime"])
        if not 0 <= (reply.seen - source).total_seconds() <= kalshi.STALE:
            raise kalshi.StaleBook(
                "US book transaction timestamp is stale or in the future."
            )
        bids, asks = us_levels(b["bids"]), us_levels(b["offers"])
        tick = Decimal(str(candidate.meta["tick_size_cents"]))
        if any(p % tick for p in (*bids, *asks)):
            raise ValueError(
                "US book includes a price outside the market tick schedule."
            )
        if bids and asks and max(bids) >= min(asks):
            raise ValueError("Crossed or locked US BTC book.")
        depth = kalshi.empty_depth()
        for name, opposite, values, reverse in (
            ("bids", "asks", bids, True),
            ("asks", "bids", asks, False),
        ):
            for price in sorted(values, reverse=reverse)[: kalshi.DEPTH]:
                quantity = kalshi.display_number(values[price])
                depth["yes"][name].append([kalshi.display_number(price), quantity])
                depth["no"][opposite].append(
                    [kalshi.display_number(100 - price), quantity]
                )
        return depth, source


def kalshi_fee_snapshot(read, event, close, waiver=None):
    """A disclosed unknown or stale schedule suppresses new paper fills."""
    if not kalshi.EVENT.fullmatch(event):
        raise ValueError("Unexpected BTC fee event.")
    series_reply = read(kalshi.KALSHI_SERIES)
    changes_reply = read(kalshi.KALSHI_CHANGES)
    event_reply = read(kalshi.KALSHI_EVENTS + event)
    at = event_reply.seen
    series = series_reply.json()["series"]
    changes = changes_reply.json()["series_fee_change_arr"]
    overrides = event_reply.json()
    valid = min(close, at + timedelta(minutes=3))
    if (
        series.get("ticker") != "KXBTC15M"
        or series.get("contract_terms_url") != kalshi.RULES
        or not isinstance(changes, list)
        or overrides.get("cursor") != ""
        or not isinstance(overrides.get("event_fee_changes"), list)
    ):
        return None
    latest = weather.instant(series["last_updated_ts"])
    if latest > at or not series_reply.seen <= changes_reply.seen <= at or valid <= at:
        return None
    kind, multiplier = series.get("fee_type"), series.get("fee_multiplier")
    for c in changes:
        effective = weather.instant(c["scheduled_ts"])
        if c.get("series_ticker") != "KXBTC15M" or latest < effective <= at:
            return None
        if effective > at:
            valid = min(valid, effective)
    current = {}
    for c in overrides["event_fee_changes"]:
        if c.get("series_ticker") != "KXBTC15M" or c.get("event_ticker") != event:
            return None
        effective = weather.instant(c["scheduled_ts"])
        if effective > at:
            valid = min(valid, effective)
        else:
            if effective in current and current[effective] != c:
                return None
            current[effective] = c
    if current:
        active = current[max(current)]
        if active.get("fee_type_override") is not None:
            kind = active["fee_type_override"]
        if active.get("fee_multiplier_override") is not None:
            multiplier = active["fee_multiplier_override"]
    if kind not in ("quadratic", "quadratic_with_maker_fees"):
        return None
    rate = quotes._number(multiplier) * Decimal("0.07")
    if not 0 <= rate <= 1:
        return None
    if waiver and at < weather.instant(waiver):
        rate = Decimal(0)
        valid = min(valid, weather.instant(waiver))
    return {
        "fee_rate": format(rate, "f"),
        "fee_source": kalshi.KALSHI_FEES,
        "fee_series_url": kalshi.KALSHI_SERIES,
        "fee_changes_url": kalshi.KALSHI_CHANGES,
        "fee_event_url": kalshi.KALSHI_EVENTS + event,
        "fee_verified_at": kalshi.stamp(at),
        "fee_valid_until": kalshi.stamp(valid),
    }


def official_outcome(venue, market_id, *, permission_guard, fetch=None, now=None):
    """Return only a venue-declared final binary result. Scalar payouts need separate handling."""
    if not isinstance(market_id, str):
        raise TypeError("Unexpected BTC market identifier.")
    us = venue in ("polymarket_us", "polymarket_us_hourly")
    match = (SLUG if us else kalshi.TICKER).fullmatch(market_id)
    if (
        venue not in ("kalshi", "polymarket_us", "polymarket_us_hourly")
        or not match
        or (venue == "polymarket_us_hourly" and match[1] != "1h")
    ):
        raise ValueError("Unsupported settlement venue or instrument.")

    def guard():
        allowed = permission_guard()
        if allowed is not None and allowed is not True:
            raise feeds.PermissionRevoked("BTC outcome permission changed.")

    clock = now if callable(now) else (lambda: now) if now is not None else feeds.clock
    reader = fetch or (us_download if us else kalshi.download)

    def read(url):
        guard()
        try:
            r = reader(url, max_bytes=200_000, permission_guard=guard)
        finally:
            guard()
        if (
            r.status != 200
            or not r.origin_date
            or not 0 <= r.age <= 120
            or not -3 <= (r.seen - r.origin_date).total_seconds() <= 120
            or not 0 <= (clock() - r.seen).total_seconds() <= 30
        ):
            raise ValueError("Unverified official settlement receipt.")
        return r

    if us:
        try:
            meta = read(us_market_url(market_id, "metadata"))
        except feeds.Unavailable as e:
            if e.code == 404:
                return None
            raise
        m = meta.json()["market"]
        if m.get("slug") != market_id:
            raise ValueError("US settlement slug mismatch.")
        if m.get("status") != "MARKET_STATUS_RESOLVED" or m.get("closed") is not True:
            return None
        _, closed, _ = us_identity(m, match[1])
        if closed > meta.seen or weather.instant(m["updatedAt"]) > meta.seen:
            raise ValueError("US resolution precedes its observation window.")
        url = us_market_url(market_id, "settlement")
        try:
            reply = read(url)
        except feeds.Unavailable as e:
            if e.code == 404:
                return None
            raise
        data = reply.json()
        if data.get("slug") != market_id or reply.seen < meta.seen:
            raise ValueError("US settlement receipt does not match instrument.")
        if data.get("settlement") is None:
            return None
        value = quotes._number(data["settlement"])
        if value not in (0, 1):
            return None
        sides = {x["long"]: quotes._number(x["price"]) for x in m["marketSides"]}
        if sides != {True: value, False: 1 - value}:
            return None
        result = "YES" if value == 1 else "NO"
        resolved = None  # Gateway settlement documents the value but supplies no determination timestamp.
    else:
        url = feeds.API + "/markets/" + market_id
        try:
            reply = read(url)
        except feeds.Unavailable as e:
            if e.code != 404:
                raise
            url = feeds.API + "/historical/markets/" + market_id
            try:
                reply = read(url)
            except feeds.Unavailable as missing:
                if missing.code == 404:
                    return None
                raise
        m = reply.json()["market"]
        if m.get("ticker") != market_id or m.get("event_ticker") != match[1]:
            raise ValueError("Kalshi outcome belongs to a different instrument.")
        provisional = m.get("is_provisional")
        if m.get("status") != "finalized" or (
            provisional is not None and provisional is not False
        ):
            return None
        if m.get("result") not in ("yes", "no"):
            return None
        opened, closed = (
            weather.instant(m["open_time"]),
            weather.instant(m["close_time"]),
        )
        end = weather.eastern(closed)
        expected = f"KXBTC15M-{end.year % 100:02}{kalshi.MONTHS[end.month - 1][:3].upper()}{end.day:02}{end.hour:02}{end.minute:02}-{end.minute:02}"
        payout = quotes._number(m["settlement_value_dollars"])
        settled = weather.instant(m["settlement_ts"])
        if (
            m.get("market_type") != "binary"
            or quotes._number(m.get("notional_value_dollars")) != 1
            or m.get("strike_type") != "greater_or_equal"
            or expected != market_id
            or closed.second
            or closed.microsecond
            or closed - opened != kalshi.WINDOW
            or not closed <= settled <= reply.seen
            or payout != int(m["result"] == "yes")
        ):
            return None
        resolved = kalshi.stamp(settled)
        result = m["result"].upper()
    guard()
    return {
        "venue": "polymarket_us" if us else "kalshi",
        "market_id": market_id,
        "result": result,
        "observed_at": kalshi.stamp(reply.seen),
        "http_date": kalshi.stamp(reply.origin_date),
        "resolved_at": resolved,
        "source": url,
    }
