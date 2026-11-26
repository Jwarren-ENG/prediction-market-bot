"""Private read-only Kalshi/NOAA collection. No credentials or order endpoints."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import ssl
import sys
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from . import weather as wx

API = "https://external-api.kalshi.com/trade-api/v2"
NWS = "https://api.weather.gov"
POINT = NWS + "/points/40.7833,-73.9667"
OBS = NWS + "/stations/KNYC/observations/latest"
CLIS = NWS + "/products/types/CLI/locations/NYC"
NBM = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
SERIES = API + "/series/KXHIGHNY"
TERMS = "https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf"
TERMS_SHA = "7d0bc031326bf6adcf0e5b50757a9f56c4e30bf21d84e331906869d39411c5a7"
SECONDARY_SHA = "5d478f7b26b83feeafc14c69f8ca44c17f449975c7307f7f7359deb9fe071cbb"
FEES = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
# Published July 7, 2026. Check the per-series type, multiplier and event overrides live.
GENERAL_TAKER = Decimal("0.07")
FEE_REVIEW = "2026-11-26T18:33:40.000000Z"
SERIES_FEES = API + "/series/fee_changes?series_ticker=KXHIGHNY"
OPEN = API + "/markets?series_ticker=KXHIGHNY&status=open&limit=100"
RECENT = API + "/markets?series_ticker=KXHIGHNY&status=settled&limit=12"
BOOK_TTL = 60
MAX_ERRORS = 12
ALLOWED_HOSTS = {urlsplit(u).hostname for u in (API, NWS, NBM, TERMS)}
VENUE_HOSTS = {urlsplit(u).hostname for u in (API, TERMS)}
SAFE_ERRORS = (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    IndexError,
    InvalidOperation,
    OverflowError,
)
TICKER = re.compile(r"(KXHIGHNY-\d{2}[A-Z]{3}\d{2})-[BT]-?\d+(?:\.\d+)?")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def clock():
    return datetime.now(wx.UTC)


class Unavailable(Exception):
    def __init__(self, message, code=None, retry=0):
        super().__init__(message)
        self.code, self.retry = code, retry


class PermissionRevoked(Exception):
    """Abort the entire uncommitted venue batch, including a response in flight."""


def checked_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("Unexpected source URL")
    return url


class OfficialRedirect(HTTPRedirectHandler):
    def __init__(self, source_host, permission_guard):
        self.source_host = source_host
        self.permission_guard = permission_guard

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(checked_url(newurl)).hostname != self.source_host:
            raise ValueError("Cross-host source redirect rejected")
        if self.permission_guard:
            self.permission_guard()
        return super().redirect_request(
            req, fp, code, msg, headers, checked_url(newurl)
        )


@dataclass
class Reply:
    body: bytes
    seen: datetime
    origin_date: datetime | None
    modified: datetime | None
    age: int = 0
    status: int = 200

    def json(self):
        value = json.loads(self.body)
        if not isinstance(value, dict):
            raise ValueError("Expected an object from the source")
        return value


def http_time(value):
    if not value:
        return None
    return parsedate_to_datetime(value).astimezone(wx.UTC)


def download(url, max_bytes=2_000_000, range_header=None, permission_guard=None):
    # The python.org macOS installer may have an empty private OpenSSL store.
    # The OS CA bundle preserves normal certificate and hostname verification.
    context = ssl.create_default_context()
    if (
        sys.platform == "darwin"
        and not context.get_ca_certs()
        and Path("/etc/ssl/cert.pem").is_file()
    ):
        context.load_verify_locations("/etc/ssl/cert.pem")
    client = build_opener(
        HTTPSHandler(context=context),
        OfficialRedirect(urlsplit(url).hostname, permission_guard),
    )
    headers = {
        "User-Agent": "PMB/0.1 (single-owner Central Park weather research)",
        "Accept": "*/*",
        "Cache-Control": "no-cache",
        "Accept-Encoding": "identity",
    }
    if range_header:
        headers["Range"] = range_header
    try:
        if permission_guard:
            permission_guard()
        with client.open(
            Request(checked_url(url), headers=headers), timeout=6
        ) as response:
            body = response.read(max_bytes + 1)
            seen = clock()
            if len(body) > max_bytes:
                raise Unavailable("Source response exceeds the expected size")
            return Reply(
                body,
                seen,
                http_time(response.headers.get("Date")),
                http_time(response.headers.get("Last-Modified")),
                int(response.headers.get("Age", 0)),
                response.status,
            )
    except HTTPError as exc:
        wait = exc.headers.get("Retry-After", "")
        try:
            seconds = (
                int(wait)
                if wait.isdecimal()
                else int((http_time(wait) - clock()).total_seconds())
            )
        except (TypeError, ValueError):
            seconds = 60
        finally:
            exc.close()
        raise Unavailable(f"HTTP {exc.code}", exc.code, max(1, seconds)) from exc
    except (OSError, URLError, HTTPException, ValueError) as exc:
        raise Unavailable(
            f"Source transport unavailable ({type(exc).__name__})"
        ) from exc


def database(data_dir):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "feeds.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    wx.initialize(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cache(
            name TEXT PRIMARY KEY, seen TEXT, value TEXT);
        CREATE TABLE IF NOT EXISTS market_registry(
            id TEXT PRIMARY KEY, event_day TEXT, close_at TEXT, checked TEXT DEFAULT '',
            final TEXT, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS packets(
            id TEXT, as_of TEXT, payload TEXT, PRIMARY KEY(id, as_of));
        CREATE TABLE IF NOT EXISTS books(
            id TEXT PRIMARY KEY, seen TEXT, levels TEXT);
        CREATE TABLE IF NOT EXISTS exceptional_results(
            id TEXT, resolved TEXT, seen TEXT, source TEXT, payout TEXT,
            PRIMARY KEY(id,resolved,payout));
    """)
    return db


class Cycle:
    def __init__(self, db, permission_check=None):
        self.db = db
        self.errors = []
        self.permission_check = permission_check

    def require_permission(self):
        if self.permission_check is None:
            return
        try:
            allowed = self.permission_check() is True
        except Exception as exc:
            raise PermissionRevoked("Kalshi permission could not be verified.") from exc
        if not allowed:
            raise PermissionRevoked("Kalshi permission changed during collection.")

    def cache(self, name, ttl):
        row = self.db.execute("SELECT * FROM cache WHERE name=?", (name,)).fetchone()
        now = clock()
        if row and 0 <= (now - wx.instant(row["seen"])).total_seconds() <= ttl:
            return json.loads(row["value"])
        return None

    def save(self, name, value, at=None):
        self.db.execute(
            "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
            (name, wx.iso(at or clock()), json.dumps(value)),
        )

    def get(self, url, **kwargs):
        host = urlsplit(checked_url(url)).hostname
        venue = host in VENUE_HOSTS
        if venue:
            self.require_permission()
        until = self.cache("retry:" + host, 366 * 86400)
        if until and wx.instant(until) > clock():
            raise Unavailable("Source is backing off until " + until)
        try:
            reply = download(
                url,
                permission_guard=self.require_permission if venue else None,
                **kwargs,
            )
        except Unavailable as exc:
            if venue:
                self.require_permission()
            if exc.code == 429 or exc.code in (502, 503, 504) or exc.code is None:
                seconds = exc.retry if exc.code == 429 else 30
                self.save(
                    "retry:" + host,
                    wx.iso(clock() + timedelta(seconds=min(366 * 86400, seconds))),
                )
            raise
        if venue:
            self.require_permission()
        return reply

    def attempt(self, name, operation):
        try:
            return operation()
        except (Unavailable, *SAFE_ERRORS) as exc:
            if len(self.errors) < MAX_ERRORS:
                self.errors.append(f"{name}: {exc}")
            return None

    def json(self, url, ttl=0):
        if ttl:
            old = self.cache(url, ttl)
            if old:
                return old["data"], wx.instant(old["seen"])
        response = self.get(url)
        if url.startswith(API + "/"):
            if (
                not response.origin_date
                or response.age < 0
                or response.age > 30
                or not -3
                <= (response.seen - response.origin_date).total_seconds()
                <= 30
            ):
                raise ValueError("Stale or untimestamped venue HTTP response")
        data = response.json()
        if ttl:
            self.save(url, {"data": data, "seen": wx.iso(response.seen)}, response.seen)
        return data, response.seen


def gather_weather(cycle):
    db = cycle.db

    def point():
        data, _ = cycle.json(POINT, 86400)
        p = data["properties"]
        if (
            p.get("gridId") != "OKX"
            or p.get("timeZone") != "America/New_York"
            or type(p.get("gridX")) is not int
            or type(p.get("gridY")) is not int
        ):
            raise ValueError("Central Park point metadata or forecast office changed")
        url = f"{NWS}/gridpoints/OKX/{p['gridX']},{p['gridY']}/forecast"
        if p.get("forecast") != url:
            raise ValueError("Unexpected Central Park grid forecast endpoint")
        data, seen = cycle.json(url, 600)
        wx.read_point(db, data, url, seen)

    def observation():
        data, seen = cycle.json(OBS, 300)
        issued = wx.instant(data["properties"]["timestamp"])
        url = f"{NWS}/stations/KNYC/observations/" + issued.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        wx.read_observation(db, data, url, seen)

    def climate():
        listing, _ = cycle.json(CLIS, 600)
        # A missed day may be recovered from the product list. Its first_seen
        # remains the actual retrieval, never the archived product's issue time.
        for product in listing["@graph"][:6]:
            item = product.get("id", "")
            if not UUID.fullmatch(item) or product.get("issuingOffice") != "KOKX":
                continue
            url = NWS + "/products/" + item
            if db.execute(
                "SELECT 1 FROM weather_records WHERE url=?", (url,)
            ).fetchone():
                continue

            def read(url=url):
                data, seen = cycle.json(url)
                wx.read_cli(db, data, url, seen)

            cycle.attempt("CLINYC bulletin", read)

    cycle.attempt("NWS point forecast", point)
    cycle.attempt("KNYC observation", observation)
    cycle.attempt("CLINYC product list", climate)
    cycle.attempt("NOAA NBM", lambda: gather_nbm(cycle))


def gather_nbm(cycle):
    now = clock()
    ready = now - timedelta(hours=2)
    cycle_hour = 1 + ((ready.hour - 1) // 6) * 6
    run = ready.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        hours=cycle_hour
    )
    url = NBM + f"/blend.{run:%Y%m%d}/{run:%H}/text/blend_nbstx.t{run:%H}z"
    if cycle.cache("nbm:run", 86400) == url:
        return
    # Use a previously measured byte offset to avoid downloading every station.
    # A changed layout falls back to the bounded, official full product.
    prior = cycle.cache("nbm:offset", 7 * 86400)
    card = None
    if type(prior) is int:
        first = max(0, prior - 256_000)
        end = prior + 256_000
        try:
            reply = cycle.get(
                url, max_bytes=45_000_000, range_header=f"bytes={first}-{end}"
            )
            card = nbm_card(reply.body)
            if reply.status != 206:
                first = 0
        except Unavailable as e:
            if e.code != 416:
                raise
    if card is None:
        reply = cycle.get(url, max_bytes=45_000_000)
        first = 0
        card = nbm_card(reply.body)
    if not card or not reply.modified:
        raise ValueError(
            "Operational KNYC NBS card or publication timestamp unavailable"
        )
    offset, raw = card
    wx.read_nbm(cycle.db, raw, url, run, reply.modified, reply.seen)
    cycle.save("nbm:offset", first + offset)
    cycle.save("nbm:run", url)


def nbm_card(data):
    match = re.search(rb"(?m)^ *KNYC\s+NBM[^\n]*NBS GUIDANCE[^\n]*\n", data)
    if not match:
        return None
    end = re.search(rb"\n[ \r]*\n", data[match.end() : match.end() + 10000])
    if not end:
        return None
    return match.start(), data[match.start() : match.end() + end.start()].decode(
        "ascii"
    )


def rule_data(market, terms_ok):
    ticker = market["ticker"]
    event = market["event_ticker"]
    ticker_match = TICKER.fullmatch(ticker)
    if not ticker_match or ticker_match[1] != event:
        raise ValueError("Unexpected series identifier")
    day = wx.event_day(event)
    primary, secondary, close_rule = (
        market.get(k, "")
        for k in ("rules_primary", "rules_secondary", "early_close_condition")
    )
    raw_close = wx.instant(market["close_time"])
    close = min(raw_close, wx.event_end(day) - timedelta(minutes=1))
    missing = []
    if (
        market.get("market_type") != "binary"
        or Decimal(str(market.get("notional_value_dollars"))) != 1
    ):
        missing.append("Unknown contract type or payout.")
    if market.get("price_level_structure") != "linear_cent":
        missing.append("Unsupported contract price grid.")
    if not terms_ok:
        missing.append(
            "Current certified GLOBALTEMPERATURE terms and source hierarchy unverified."
        )
    lo = market.get("floor_strike")
    hi = market.get("cap_strike")
    strike = market.get("strike_type")
    if not all(x is None or type(x) is int and -80 <= x <= 155 for x in (lo, hi)):
        raise ValueError("Unrecognized integer Fahrenheit boundary")
    if strike == "between" and lo is not None and hi is not None and lo <= hi:
        phrase, label, bounds = f"between {lo}-{hi}°", f"{lo} to {hi}°F", (lo, hi)
    elif strike == "less" and hi is not None and lo is None:
        phrase, label, bounds = (
            f"less than {hi}°",
            f"{hi - 1}°F or below",
            (None, hi - 1),
        )
    elif strike == "greater" and lo is not None and hi is None:
        phrase, label, bounds = (
            f"greater than {lo}°",
            f"{lo + 1}°F or above",
            (lo + 1, None),
        )
    else:
        raise ValueError("Unsupported temperature strike type or boundaries")
    # Verify date, authority, units and range in the actual operative sentence.
    short_months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    long_months = "January February March April May June July August September October November December".split()
    date_label = f"{short_months[day.month - 1]} {day.day}, {day.year}"
    full_date = f"{long_months[day.month - 1]} {day.day}, {day.year}"
    template = (
        f"If the maximum temperature recorded at New York City (CLINYC) for {date_label}, "
        f"is {phrase} fahrenheit according to The Weather Company, then the market resolves to Yes."
    )

    def normalize(text):
        return " ".join(text.lower().split())

    if normalize(primary) != normalize(template):
        missing.append(
            "Market proposition, date, bounds or settlement authority changed."
        )
    expected_close = (
        f"The Last Trading Time will be 11:59 PM local time on {full_date} regardless of any "
        f"data releases or events occurring. Expiration will occur on the sooner of the first 7:00 or 8:00 AM ET "
        f"following the release of the data for {full_date}, or one week after {full_date}."
    )
    if normalize(expected_close) != normalize(close_rule):
        missing.append("Exceptional trading cutoff unverified.")
    if hashlib.sha256(normalize(secondary).encode()).hexdigest() != SECONDARY_SHA:
        missing.append(
            "Material-error and last-fair-price settlement terms unverified."
        )
    rules = "\n\n".join([primary, secondary, close_rule])
    digest = hashlib.sha256((TERMS_SHA + rules).encode()).hexdigest()[:20]
    return day, close, bounds, label, rules, digest, missing


def book_levels(data):
    fixed = data["orderbook_fp"]
    levels = {}
    for ask, opposite_bid in (("yes", "no_dollars"), ("no", "yes_dollars")):
        bids = fixed[opposite_bid]
        if not isinstance(bids, list):
            raise ValueError("Unrecognized venue order book")
        rows = {}
        for price_str, size_str in bids:
            price, size = Decimal(price_str) * 100, Decimal(size_str)
            if (
                not price.is_finite()
                or not size.is_finite()
                or not 0 < price < 100
                or price != price.to_integral_value()
                or size < 0
                or size > 1_000_000_000
            ):
                raise ValueError("Unsupported or invalid venue price or depth")
            key = 100 - int(price)
            if key in rows:
                raise ValueError("Duplicate venue price level")
            rows[key] = int(size)  # Only whole contracts at this exact price.
        levels[ask] = [[p, q] for p, q in sorted(rows.items()) if q > 0]
    if (
        levels["yes"]
        and levels["no"]
        and levels["yes"][0][0] + levels["no"][0][0] <= 100
    ):
        raise ValueError("Locked or crossed venue order book")
    return levels


def fee_rate(series, series_changes, event_changes, event, asof):
    if (
        not series
        or series_changes is None
        or event_changes is None
        or wx.instant(FEE_REVIEW) > asof
    ):
        return None
    if (
        series["ticker"] != "KXHIGHNY"
        or wx.instant(series["last_updated_ts"]) > asof
        or event_changes.get("cursor")
        or not isinstance(event_changes.get("event_fee_changes"), list)
        or not isinstance(series_changes.get("series_fee_change_arr"), list)
    ):
        return None
    kind, multiplier = series["fee_type"], series["fee_multiplier"]
    overrides = event_changes["event_fee_changes"]
    active = []
    for entry in overrides:
        if (
            entry.get("event_ticker") != event
            or entry.get("series_ticker") != "KXHIGHNY"
        ):
            return None
        effective = wx.instant(entry["scheduled_ts"])
        if effective <= asof:
            active.append((effective, entry))
    if active:
        _, latest = max(active, key=lambda item: item[0])
        kind = (
            latest["fee_type_override"]
            if latest["fee_type_override"] is not None
            else kind
        )
        multiplier = (
            latest["fee_multiplier_override"]
            if latest["fee_multiplier_override"] is not None
            else multiplier
        )
    for change in series_changes["series_fee_change_arr"]:
        effective = wx.instant(change["scheduled_ts"])
        if (
            change.get("series_ticker") != "KXHIGHNY"
            or wx.instant(series["last_updated_ts"]) < effective <= asof
        ):
            # Stale series snapshot across an effective change.
            return None
    if kind not in ("quadratic", "quadratic_with_maker_fees"):
        return None
    multiplier = Decimal(str(multiplier))
    if not multiplier.is_finite() or not 0 <= GENERAL_TAKER * multiplier <= 1:
        return None
    return format(GENERAL_TAKER * multiplier, "f")


def certified(cycle):
    cached = cycle.cache("certified-terms", 3600)
    if cached is not None:
        return cached
    reply = cycle.get(TERMS)
    ok = hashlib.sha256(reply.body).hexdigest() == TERMS_SHA and (
        not reply.modified or reply.modified <= reply.seen
    )
    result = {
        "ok": ok,
        "published": wx.iso(reply.modified) if reply.modified else None,
        "seen": wx.iso(reply.seen),
    }
    cycle.save("certified-terms", result, reply.seen)
    return result


def report_settlement(cycle, market, seen, source, certified_terms):
    db = cycle.db
    ticker = market["ticker"]
    ticker_match = TICKER.fullmatch(ticker)
    if not ticker_match or market.get("status") != "finalized":
        return
    day = wx.event_day(market["event_ticker"])
    if ticker_match[1] != market["event_ticker"]:
        raise ValueError("Settlement market/event identifiers disagree")
    resolved = wx.instant(market["settlement_ts"])
    result = market["result"]
    price = Decimal(str(market.get("settlement_value_dollars")))
    if (
        resolved > seen
        or seen > clock()
        or resolved < wx.event_end(day) - timedelta(minutes=1)
    ):
        raise ValueError("Settlement time lies outside its valid window")
    direct = (
        API
        + ("/historical/markets/" if "/historical/markets/" in source else "/markets/")
        + ticker
    )
    if result == "scalar":
        if not price.is_finite() or not 0 <= price <= 1:
            raise ValueError("Invalid scalar settlement value")
        db.execute(
            "INSERT OR IGNORE INTO exceptional_results VALUES(?,?,?,?,?)",
            (ticker, wx.iso(resolved), wx.iso(seen), direct, str(price)),
        )
        raise ValueError(
            f"{ticker} finalized at a last fair price of {price} dollars. Its exceptional payout needs reconciliation."
        )
    if result not in ("yes", "no") or price != (1 if result == "yes" else 0):
        raise ValueError("Final result and published payout disagree")
    message = {
        "market_id": ticker,
        "result": result,
        "observed_at": wx.iso(seen),
        "source": direct,
        "retrieval_url": source,
        "published_at": wx.iso(resolved),
    }
    prior = db.execute(
        "SELECT final FROM market_registry WHERE id=?", (ticker,)
    ).fetchone()
    if prior and prior["final"] and json.loads(prior["final"])["result"] != result:
        raise ValueError(
            f"Conflicting prior final result for {ticker}; reconciliation required"
        )
    db.execute(
        """INSERT INTO market_registry(id,event_day,close_at,final,first_seen)
        VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET final=coalesce(market_registry.final,excluded.final),
        first_seen=coalesce(market_registry.first_seen,excluded.first_seen)""",
        (
            ticker,
            str(day),
            wx.iso(wx.event_end(day) - timedelta(minutes=1)),
            json.dumps(message),
            wx.iso(seen),
        ),
    )
    # Post the venue's verified binary result regardless of its weather model.
    # Admit an underlying high to training only if the audited rule and actual
    # settlement agree. A CLINYC report alone can never create a venue label.
    if not certified_terms:
        return
    _, _, bounds, _, _, _, issues = rule_data(market, True)
    if issues or not market.get("expiration_value"):
        return
    try:
        actual = Decimal(str(market["expiration_value"]))
    except InvalidOperation:
        return
    if (
        not actual.is_finite()
        or actual != actual.to_integral_value()
        or not -80 <= actual <= 155
        or wx.qualifies(int(actual), bounds) != (result == "yes")
    ):
        return
    db.execute(
        "INSERT OR IGNORE INTO venue_highs VALUES(?,?,?,?,?,?)",
        (str(day), wx.AUTHORITY, int(actual), wx.iso(seen), wx.iso(resolved), direct),
    )


def gather_settlements(cycle, terms_ok):
    def recent():
        data, seen = cycle.json(RECENT, 900)
        for market in data["markets"]:
            cycle.attempt(
                "Kalshi final result",
                lambda m=market: report_settlement(cycle, m, seen, RECENT, terms_ok),
            )

    cycle.attempt("Recent venue settlements", recent)
    now = clock()
    # Query all previously discovered and now-closed contracts fairly, even if a
    # long-disconnected worker misses the rolling recent-settlements page.
    due = cycle.db.execute(
        """SELECT id FROM market_registry WHERE final IS NULL AND close_at<=?
        AND checked<=? ORDER BY checked,close_at,id LIMIT 16""",
        (wx.iso(now), wx.iso(now - timedelta(minutes=10))),
    ).fetchall()
    for row in due:
        ticker = row["id"]

        def lookup(ticker=ticker):
            url = API + f"/markets/{ticker}"
            try:
                data, seen = cycle.json(url)
            except Unavailable as e:
                if e.code != 404:
                    raise
                url = API + f"/historical/markets/{ticker}"
                data, seen = cycle.json(url)
            if data["market"].get("ticker") != ticker:
                raise ValueError("Venue returned a different settlement market")
            report_settlement(cycle, data["market"], seen, url, terms_ok)

        cycle.db.execute(
            "UPDATE market_registry SET checked=? WHERE id=?", (wx.iso(now), ticker)
        )
        cycle.attempt("Pending venue settlement", lookup)


def discover(cycle):
    url = OPEN
    found = []
    for _ in range(4):
        listing, seen = cycle.json(url)
        for market in listing["markets"]:
            if not isinstance(market, dict):
                continue
            match = TICKER.fullmatch(str(market.get("ticker")))
            if match and match[1] == market.get("event_ticker"):
                found.append((market, seen))
        cursor = listing.get("cursor")
        if not cursor:
            return found
        url = OPEN + "&" + urlencode({"cursor": cursor})
    raise ValueError(
        "Series discovery exceeded the bounded scan; full catalog unavailable"
    )


def quote(cycle, ticker):
    url = f"{API}/markets/{ticker}/orderbook?depth=35"

    def fetch():
        reply = cycle.get(url)
        if not reply.origin_date:
            raise ValueError("Exchange HTTP snapshot time is missing")
        cache_age = (reply.seen - reply.origin_date).total_seconds()
        if cache_age < -3 or max(reply.age, cache_age) > 5 or reply.age < 0:
            raise ValueError("Stale or future HTTP order book")
        levels = book_levels(reply.json())
        stamp = wx.iso(reply.seen)
        cycle.db.execute(
            "INSERT OR REPLACE INTO books VALUES(?,?,?)",
            (ticker, stamp, json.dumps(levels)),
        )
        return (
            levels,
            stamp,
            None,
            wx.evidence(
                "Kalshi order book",
                url,
                None,
                stamp,
                "Visible executable asks reconstructed from opposite-side bids; sizes show whole contracts. "
                "The venue exposes no exchange publication timestamp for this REST book.",
                context_only=True,
                http_date=wx.iso(reply.origin_date),
            ),
        )

    new = cycle.attempt("Kalshi book " + ticker, fetch)
    if new:
        return new
    cached = cycle.db.execute(
        "SELECT seen,levels FROM books WHERE id=?", (ticker,)
    ).fetchone()
    reason = "Current order book unavailable."
    if cached and wx.instant(cached["seen"]) <= clock():
        return (
            json.loads(cached["levels"]),
            cached["seen"],
            reason + " Showing the last captured book.",
            None,
        )
    return {"yes": [], "no": []}, None, reason, None


def watch_cached(cycle):
    now = clock()
    rows = cycle.db.execute(
        """SELECT p.payload FROM packets p JOIN (
        SELECT id,MAX(as_of) stamp FROM packets WHERE as_of<=? GROUP BY id) last
        ON last.id=p.id AND last.stamp=p.as_of""",
        (wx.iso(now),),
    )
    result = []
    for row in rows:
        packet = json.loads(row[0])
        if wx.instant(packet["close_at"]) <= now:
            continue
        reason = (
            "Market discovery unavailable. Showing the last captured market and quote."
        )
        packet.update(
            tradable=False,
            book_executable=False,
            as_of=wx.iso(now),
            missing=list(dict.fromkeys([*packet["missing"], reason])),
        )
        result.append(packet)
    return result


def prepare(cycle, market, seen, meta, terms, fees, event_fees):
    db = cycle.db
    now = clock()
    ticker = market["ticker"]
    event = market["event_ticker"]
    terms_ok = bool(
        terms
        and terms["ok"]
        and meta
        and meta.get("ticker") == "KXHIGHNY"
        and meta.get("contract_terms_url") == TERMS
        and meta.get("settlement_sources")
        == [{"name": "The Weather Company", "url": "https://weather.com/kalshi"}]
    )
    try:
        day, close, bounds, label, rules, version, missing = rule_data(market, terms_ok)
    except SAFE_ERRORS:
        day = wx.event_day(event)
        close = min(
            wx.event_end(day) - timedelta(minutes=1), wx.instant(market["close_time"])
        )
        bounds, label = None, market.get("yes_sub_title") or ticker
        rules = "\n\n".join(
            str(market.get(k, ""))
            for k in ("rules_primary", "rules_secondary", "early_close_condition")
        )
        version = hashlib.sha256(rules.encode()).hexdigest()[:20]
        missing = [
            "Unrecognized contract boundaries, type or terms. Review the venue specification."
        ]
    db.execute(
        """INSERT INTO market_registry(id,event_day,close_at) VALUES(?,?,?)
        ON CONFLICT(id) DO UPDATE SET close_at=excluded.close_at""",
        (ticker, str(day), wx.iso(close)),
    )
    updated = wx.instant(market["updated_time"])
    if updated > seen or seen > now or wx.instant(market["open_time"]) > now:
        missing.append("Venue market timestamps lie ahead of the decision clock.")
    if market.get("status") != "active" or close <= now:
        missing.append("Venue trading window closed or inactive.")
    # A market's bid depth can remain usable to value an existing position after
    # a forecast expires. This flag also requires a fresh validated book below.
    market_active = not missing
    forecast, gap = (
        wx.estimate(db, day, bounds, now)
        if bounds
        else (
            dict(
                probability=None,
                lower=None,
                upper=None,
                baseline=None,
                model=wx.MODEL,
                samples=0,
                issued_at=None,
            ),
            "Forecast requires verified contract boundaries.",
        )
    )
    if gap:
        missing.append(gap)
    # Record the independent estimate before requesting a book. The entire
    # permission-scoped venue batch commits together.
    estimate_stamp = wx.iso(now)
    forecast["estimated_at"] = estimate_stamp
    db.execute(
        "INSERT OR IGNORE INTO estimates VALUES(?,?,?,?)",
        (ticker, estimate_stamp, version, json.dumps(forecast)),
    )
    items = list(wx.records(db, now, day, limit=8))
    if forecast.get("source_url") and all(
        x.get("sha256") != forecast.get("sha256") for x in items
    ):
        row = db.execute(
            """SELECT * FROM weather_records WHERE digest=? AND event_day=? AND first_seen<=?
                          ORDER BY issued DESC LIMIT 1""",
            (forecast["sha256"], str(day), estimate_stamp),
        ).fetchone()
        if row:
            items.append(
                wx.evidence(
                    "Forecast used in estimate",
                    row["url"],
                    row["published"],
                    row["first_seen"],
                    row["detail"],
                    issued_at=row["issued"],
                    sha256=row["digest"],
                )
            )
    market_url = API + "/markets/" + ticker
    items.append(
        wx.evidence(
            "Kalshi contract rules",
            market_url,
            wx.iso(updated),
            wx.iso(seen),
            f"Official proposition, cutoff and exceptions for {ticker}.",
            sha256=version,
        )
    )
    if terms:
        items.append(
            wx.evidence(
                "Certified contract terms",
                TERMS,
                terms["published"],
                terms["seen"],
                "GLOBALTEMPERATURE defines the source hierarchy, first non-preliminary report, "
                "revision handling and last-fair-price exception.",
                context_only=not bool(terms["published"]),
                sha256=TERMS_SHA if terms["ok"] else None,
            )
        )
    rate = cycle.attempt(
        "Applicable market fees", lambda: fee_rate(meta, fees, event_fees, event, now)
    )
    valid_until = None
    if rate is None:
        missing.append("Applicable series or event taker fee unknown.")
    else:
        scheduled = [
            wx.instant(item["scheduled_ts"])
            for item in (
                *fees["series_fee_change_arr"],
                *event_fees["event_fee_changes"],
            )
        ]
        future_changes = [t for t in scheduled if t > now]
        if future_changes:
            valid_until = wx.iso(min(future_changes))
        items.append(
            wx.evidence(
                "Kalshi general taker fee schedule",
                FEES,
                None,
                FEE_REVIEW,
                "The published schedule effective July 7, 2026 gives a base quadratic taker coefficient of 0.07.",
                context_only=True,
                effective_date="2026-07-07",
                provenance="Reviewed schedule",
            )
        )
        fee_url = API + "/events/fee_changes?" + urlencode({"event_ticker": event})
        for url, title in (
            (SERIES, "Kalshi series fee"),
            (fee_url, "Kalshi event fee overrides"),
            (SERIES_FEES, "Kalshi scheduled series fees"),
        ):
            cached = cycle.cache(url, 3600)
            if cached:
                items.append(
                    wx.evidence(
                        title,
                        url,
                        meta["last_updated_ts"] if url == SERIES else None,
                        cached["seen"],
                        f"The applicable quadratic taker coefficient is {rate}.",
                        context_only=url != SERIES,
                    )
                )
    return dict(
        id=ticker,
        event_id=event,
        title=market.get("title") or ticker,
        label=label,
        station="KNYC",
        station_name="Central Park, New York",
        event_date=str(day),
        close_at=wx.iso(close),
        venue_close_at=market["close_time"],
        as_of=estimate_stamp,
        observed_at=None,
        book_source="kalshi",
        levels={"yes": [], "no": []},
        forecast=forecast,
        rules=rules,
        rule_version=version,
        evidence=items,
        fee_rate=rate,
        fee_valid_until=valid_until,
        tradable=False,
        book_executable=market_active,
        missing=missing,
        strike_bounds_f=bounds,
        timezone="America/New_York",
        settlement_authority="The Weather Company (CLINYC)" if terms_ok else None,
        settlement_source_url="https://weather.com/kalshi" if terms_ok else None,
        terms_url=TERMS,
        market_baseline=None,
    )


def finish_packet(cycle, packet):
    levels, stamp, gap, book_evidence = quote(cycle, packet["id"])
    if book_evidence:
        packet["evidence"].append(book_evidence)
    if gap:
        packet["missing"].append(gap)
    now = clock()
    if not stamp or not 0 <= (now - wx.instant(stamp)).total_seconds() <= BOOK_TTL:
        packet["missing"].append("A fresh timestamped order book is required.")
    if not levels["yes"] or not levels["no"]:
        packet["missing"].append("Both sides need executable whole-contract depth.")
    else:
        packet["market_baseline"] = (
            levels["yes"][0][0] + 100 - levels["no"][0][0]
        ) / 200
    if now >= wx.instant(packet["close_at"]):
        packet["missing"].append("Trading cutoff reached.")
    if packet.get("fee_valid_until") and now >= wx.instant(packet["fee_valid_until"]):
        packet["missing"].append(
            "A scheduled fee change requires a new quote and fee check."
        )
    packet["book_executable"] = bool(
        packet["book_executable"]
        and not gap
        and stamp
        and 0 <= (now - wx.instant(stamp)).total_seconds() <= BOOK_TTL
        and levels["yes"]
        and levels["no"]
        and now < wx.instant(packet["close_at"])
    )
    packet.update(
        levels=levels,
        observed_at=stamp,
        as_of=wx.iso(now),
        tradable=not packet["missing"],
    )
    cycle.db.execute(
        "INSERT OR IGNORE INTO packets VALUES(?,?,?)",
        (packet["id"], packet["as_of"], json.dumps(packet)),
    )
    return packet


def gather_venue(cycle):
    db = cycle.db
    terms = cycle.attempt("Certified rules", lambda: certified(cycle))
    series = cycle.attempt("Kalshi series", lambda: cycle.json(SERIES, 60)[0]["series"])
    changes = cycle.attempt("Series fees", lambda: cycle.json(SERIES_FEES, 60)[0])
    gather_settlements(cycle, bool(terms and terms["ok"]))
    found = cycle.attempt("Kalshi market discovery", lambda: discover(cycle))
    packets = []
    if found is None:
        packets = watch_cached(cycle)
    else:
        event_fees = {}
        for market, seen in found:
            cycle.require_permission()
            event = market["event_ticker"]
            if event not in event_fees:
                url = API + "/events/fee_changes?" + urlencode({"event_ticker": event})
                event_fees[event] = cycle.attempt(
                    "Event fees", lambda u=url: cycle.json(u, 60)[0]
                )
            packet = cycle.attempt(
                "Kalshi contract " + market["ticker"],
                lambda: prepare(
                    cycle, market, seen, series, terms, changes, event_fees[event]
                ),
            )
            if packet:
                packets.append(packet)
        for packet in packets:
            cycle.require_permission()
            finish_packet(cycle, packet)
    final_rows = db.execute(
        "SELECT final FROM market_registry WHERE final IS NOT NULL AND first_seen<=?",
        (wx.iso(clock()),),
    )
    settlements = [json.loads(row[0]) for row in final_rows]
    evidence = wx.records(db, clock())
    for item in sorted(settlements, key=lambda r: r["published_at"], reverse=True)[:1]:
        evidence.insert(
            0,
            wx.evidence(
                "Final Kalshi settlement",
                item["source"],
                item["published_at"],
                item["observed_at"],
                f"{item['market_id']} finalized {item['result'].upper()}.",
                kind="settlement",
                market_id=item["market_id"],
            ),
        )
    exceptional = db.execute(
        """SELECT * FROM exceptional_results x WHERE seen<=?
        AND NOT EXISTS (SELECT 1 FROM market_registry m WHERE m.id=x.id AND m.final IS NOT NULL)
        ORDER BY resolved DESC LIMIT 5""",
        (wx.iso(clock()),),
    ).fetchall()
    for item in exceptional:
        evidence.insert(
            0,
            wx.evidence(
                "Exceptional Kalshi payout",
                item["source"],
                item["resolved"],
                item["seen"],
                f"{item['id']} finalized at ${item['payout']} for YES. Manual paper reconciliation is pending.",
                kind="exceptional_settlement",
                market_id=item["id"],
            ),
        )
    status = f"Collected {len(packets)} KXHIGHNY contracts. NOAA and venue sources recorded separately."
    if cycle.errors:
        status = "Feed degraded. " + " | ".join(dict.fromkeys(cycle.errors))
    if exceptional:
        status += " Exceptional venue payouts remain pending in the paper account."
    return {
        "packets": packets,
        "settlements": settlements,
        "evidence": evidence,
        "status": status,
        "degraded": bool(cycle.errors or exceptional),
    }


def collect(
    data_dir: Path,
    now: str,
    allow_market: bool,
    *,
    permission_check: Callable[[], bool] | None = None,
    permission_lock=None,
) -> dict:
    """One forward collection. Offline fixture runs do not call this function.

    now is the caller's cycle-start clock. Each network read gets its actual UTC
    receipt, and each decision cutoff falls after all its inputs. A historical
    or future clock never turns a current HTTP response into old data.

    A mutable owner setting should supply its generation check and the same
    lock that protects revocation, keeping the final check and commit atomic.
    No network request holds that lock.
    """
    start = wx.instant(now)
    if abs((clock() - start).total_seconds()) > 30:
        return {
            "packets": [],
            "settlements": [],
            "evidence": [],
            "degraded": True,
            "status": "Live collection requires a current clock. No new inputs were accepted.",
        }
    with closing(database(data_dir)) as db:
        cycle = Cycle(db, permission_check)
        with db:
            gather_weather(cycle)
        weather_errors = list(cycle.errors)
        status = "NOAA collection active. Kalshi market access is off."
        if allow_market is True:
            try:
                cycle.require_permission()
                result = gather_venue(cycle)
                with permission_lock if permission_lock is not None else nullcontext():
                    cycle.require_permission()
                    db.commit()
                return result
            except PermissionRevoked as exc:
                db.rollback()
                status = str(exc) + " The unfinished venue scan was discarded."
        if weather_errors:
            status += " " + " | ".join(weather_errors)
        return {
            "packets": [],
            "settlements": [],
            "status": status,
            "degraded": bool(weather_errors),
            "evidence": wx.records(db, clock()),
        }
