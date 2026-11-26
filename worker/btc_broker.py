"""Local, cash-only paper execution for one independently funded BTC contract product.

All inputs come from the worker's read-only venue adapters. The broker never contacts a
venue. Prices are in cents per contract; cash entries are integer cents. Orders walk
available depth through their limit, all or none. Each recorded venue book has a
single paper liquidity budget shared by complementary contracts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path
from urllib.parse import urlsplit

from . import weather

INITIAL = 1_000_000
FRESH = 20
SIDES = ("yes", "no")
PRODUCTS = {
    "kalshi": ("kalshi", 15, r"KXBTC15M-[0-9]{2}[A-Z]{3}[0-9]{6}-(?:00|15|30|45)"),
    "polymarket_us": ("polymarket_us", 5, r"cpc-btc-updown-5m-\d{4}-\d{2}-\d{2}-\d{4}z"),
    "polymarket_us_hourly": ("polymarket_us", 60, r"cpc-btc-updown-1h-\d{4}-\d{2}-\d{2}-\d{4}z"),
}
FEE_URL = {
    "kalshi": "https://kalshi.com/docs/kalshi-fee-schedule.pdf",
    "polymarket_us": "https://docs.polymarket.us/fees",
}
LIMITS = dict(
    max_order_qty=20, max_order_cents=2500, max_window_cents=5000, max_open_cents=10000, daily_loss_cents=5000
)
HARD_LIMITS = dict(
    max_order_qty=100,
    max_order_cents=10000,
    max_window_cents=25000,
    max_open_cents=50000,
    daily_loss_cents=25000,
)
RULE = (
    "Experimental venue-book momentum. Three consecutive two-sided quotes over 20–45 seconds "
    "must move in one direction by at least 6¢, with no gap over 20 seconds and each spread at most "
    "2¢. The selected side must be at 55–80¢ and its move must exceed spread plus estimated "
    "round-trip fees. With at least 60 seconds left, buy up to five contracts once per window. "
    "Sell automatic holdings when their midpoint falls to 50¢, the executable gain after fees "
    "reaches 8¢ per contract, or 30 seconds remain. Unfilled positions await the official result."
)
FEES = (
    "Paper taker fees use the verified venue coefficient times quantity times price times "
    "(1 − price), with price in dollars and no rebate credit. Kalshi costs round upward to "
    "whole cents per posted level. Polymarket US uses the published unrebated market rate "
    "and the published half-even cumulative commission cap for the order. Depth is "
    "aggregated by price; individual resting order sizes are unavailable."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id=1), venue TEXT NOT NULL, cash INTEGER NOT NULL CHECK(cash>=0),
    realized INTEGER NOT NULL DEFAULT 0, auto INTEGER NOT NULL DEFAULT 0, limits TEXT NOT NULL,
    peak INTEGER NOT NULL DEFAULT 1000000, drawdown INTEGER NOT NULL DEFAULT 0,
    current_market TEXT, seen TEXT, gate TEXT NOT NULL DEFAULT 'Waiting for the first verified venue book.');
CREATE TABLE IF NOT EXISTS windows (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, opened TEXT NOT NULL, closed TEXT NOT NULL,
    book TEXT NOT NULL, result TEXT, resolved TEXT, source TEXT,
    cash_flow INTEGER NOT NULL DEFAULT 0, fill_count INTEGER NOT NULL DEFAULT 0, manual_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS holdings (
    market TEXT NOT NULL REFERENCES windows(id), side TEXT NOT NULL, owner TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK(qty>0), cost INTEGER NOT NULL CHECK(cost>=0), PRIMARY KEY(market,side,owner));
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, req TEXT NOT NULL, at TEXT NOT NULL, day TEXT NOT NULL,
    source TEXT NOT NULL, market TEXT NOT NULL, action TEXT NOT NULL, filled INTEGER NOT NULL,
    fee INTEGER NOT NULL, receipt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS orders_day ON orders(day,filled);
CREATE INDEX IF NOT EXISTS orders_market ON orders(market,source,action,filled);
CREATE TABLE IF NOT EXISTS used (
    market TEXT NOT NULL, book TEXT NOT NULL, bid_side TEXT NOT NULL, price INTEGER NOT NULL,
    qty INTEGER NOT NULL CHECK(qty>0), PRIMARY KEY(market,book,bid_side,price));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    message TEXT NOT NULL, realized INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_day ON events(day);
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY, opening INTEGER, opening_at TEXT, risk_opening INTEGER, closing INTEGER, as_of TEXT,
    last_known INTEGER, last_known_at TEXT, positions INTEGER NOT NULL DEFAULT 0,
    unmarked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS marks (
    at TEXT PRIMARY KEY, equity INTEGER, cash INTEGER NOT NULL, unmarked INTEGER NOT NULL);
"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now(value):
    if value is None:
        return datetime.now(weather.UTC)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(weather.UTC)
    return weather.instant(value)


def _day(t):
    return weather.eastern(t).date().isoformat()


def _num(value):
    if not isinstance(value, (int, float, str, Decimal)) or isinstance(value, bool) or len(str(value)) > 32:
        raise ValueError("Invalid numeric input.")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid numeric input.") from None
    if not n.is_finite() or n.copy_abs() > 1_000_000_000 or (n and n.adjusted() < -10):
        raise ValueError("Invalid numeric input.")
    return n if n else Decimal(0)


def _price(value):
    n = _num(value) * 100  # hundredths of one cent
    if not 0 < n < 10_000 or n != n.to_integral_value():
        raise ValueError("Price must be between 0 and 100 cents with at most two decimal places.")
    return int(n)


def _number(n):
    return int(n) if n == int(n) else float(n)


def _opposite(side):
    return "no" if side == "yes" else "yes"


def _source_url(venue, market, url, *, book):
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.fragment:
        return False
    if venue == "kalshi":
        hosts = {"external-api.kalshi.com", "api.elections.kalshi.com", "api.kalshi.com"}
        path = "/trade-api/v2/markets/" + market + ("/orderbook" if book else "")
        paths = {path} if book else {path, "/trade-api/v2/historical/markets/" + market}
        return parts.netloc in hosts and parts.path in paths and (book or not parts.query)
    path = "/v1/markets/" + market + ("/book" if book else "/settlement")
    return parts.netloc == "gateway.polymarket.us" and parts.path == path and not parts.query


def _market_id(product, value):
    if not isinstance(value, str) or not re.fullmatch(PRODUCTS[product][2], value):
        raise ValueError("The market identifier does not belong to this BTC account.")
    return value


def _check_clock(t, seen, source, http, opened, closed):
    if not opened <= seen <= t < closed or (t - seen).total_seconds() > FRESH:
        raise ValueError("A fresh book for the open BTC window is required.")
    if source > t:
        raise ValueError("The venue quote is dated after this paper decision.")
    for stamp in (source, http):
        if not opened <= stamp < closed or not -3 <= (seen - stamp).total_seconds() <= FRESH:
            raise ValueError("The venue book timestamp is stale or outside this window.")
        if (t - stamp).total_seconds() > FRESH:
            raise ValueError("The venue book timestamp is stale or outside this window.")


def _depth(raw):
    """Canonicalize YES and NO bids; a complementary ask draws from the same level."""
    if not isinstance(raw, dict):
        raise ValueError("Posted BTC depth is missing.")
    sides = {}
    for side in SIDES:
        sides[side] = {}
        for key in ("bids", "asks"):
            rows = raw[side][key]
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("Invalid BTC depth.")
            levels = {}
            for v in rows:
                if not isinstance(v, (tuple, list)) or len(v) != 2:
                    raise ValueError("Invalid BTC depth level.")
                p, n = _price(v[0]), _num(v[1])
                if n <= 0 or p in levels:
                    raise ValueError("Invalid or duplicate BTC depth level.")
                levels[p] = n
            sides[side][key] = levels
    output = {}
    for side in SIDES:
        bids = sides[side]["bids"]
        if {10000 - p: n for p, n in bids.items()} != sides[_opposite(side)]["asks"]:
            raise ValueError("Complementary YES and NO depth disagrees.")
        output[side] = [[p, int(n)] for p, n in sorted(bids.items(), reverse=True) if n >= 1]
        if not output[side]:
            raise ValueError("Whole-contract depth is missing on one side.")
    if max(sides["yes"]["bids"]) + max(sides["no"]["bids"]) >= 10000:
        raise ValueError("The venue book is locked or crossed.")
    return output


def _samples(rows, opened, seen, venue):
    samples = []
    for r in rows[-3:]:
        try:
            at = weather.instant(r["at"])
            origin = weather.instant(
                r.get("source_at") or (r.get("http_date") if venue == "kalshi" else None)
            )
            if r["kind"] != "book" or not opened <= at <= seen or not opened <= origin:
                return []
            if not -3 <= (at - origin).total_seconds() <= FRESH:
                return []
            bid, ask = _price(r["yes_bid_cents"]), _price(r["yes_ask_cents"])
            if not 0 < ask - bid <= 200:
                return []
            samples.append([weather.iso(at), weather.iso(origin), bid, ask])
        except (KeyError, TypeError, ValueError):
            return []
    return samples


def _book(product, state, t):
    venue, minutes, _ = PRODUCTS[product]
    if not isinstance(state, dict) or state.get("enabled") is not True:
        raise ValueError("Public market access is off for this paper account.")
    if state.get("status") != "live":
        raise ValueError("The venue has no complete, fresh executable BTC book.")
    m = state.get("current")
    if not isinstance(m, dict):
        raise ValueError("The current BTC window is unavailable.")
    mid = _market_id(product, m.get("id"))
    if m.get("venue") != venue or m.get("rules_verified") is not True or not m.get("rules_primary"):
        raise ValueError("The published rules or venue identity are unverified.")
    if not _source_url(venue, mid, m.get("book_url"), book=True):
        raise ValueError("The quote source does not match the selected venue contract.")
    opened, closed = (weather.instant(m[k]) for k in ("open_at", "close_at"))
    if (
        closed - opened != timedelta(minutes=minutes)
        or opened.minute % minutes
        or opened.second
        or opened.microsecond
    ):
        raise ValueError("The contract window does not match this account.")
    if venue == "polymarket_us":
        if not mid.endswith(opened.strftime("%Y-%m-%d-%H%Mz")):
            raise ValueError("The market identifier and opening timestamp disagree.")
    else:
        end = weather.eastern(closed)
        months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
        expected = f"KXBTC15M-{end.year % 100:02d}{months[end.month-1]}{end:%d%H%M}-{end:%M}"
        if mid != expected:
            raise ValueError("The market identifier and closing timestamp disagree.")
    target = _num(m.get("target_usd"))
    if target <= 0 or target > 100_000_000:
        raise ValueError("The published opening BTC target is missing or invalid.")
    seen, source, http = (weather.instant(m[k]) for k in ("observed_at", "source_at", "http_date"))
    _check_clock(t, seen, source, http, opened, closed)
    fee_source = m.get("fee_source")
    if not isinstance(fee_source, str) or fee_source.split("#")[0] != FEE_URL[venue]:
        raise ValueError("The published taker fee source is unverified.")
    verified = weather.instant(m.get("fee_verified_at"))
    if not 0 <= (t - verified).total_seconds() <= 86400:
        raise ValueError("The published taker fee is stale or not yet verified.")
    # An announced fee change immediately invalidates the saved calculation.
    expiry = (
        weather.instant(m["fee_valid_until"]) if m.get("fee_valid_until") else verified + timedelta(days=1)
    )
    expiry = min(expiry, verified + timedelta(days=1))
    if t >= expiry:
        raise ValueError("The verified taker fee has expired.")
    rate = _num(m.get("fee_rate"))
    if not 0 <= rate <= 1:
        raise ValueError("Invalid venue taker fee.")
    bids = _depth(m["depth"])
    rows = m.get("history", [])
    return dict(
        id=mid,
        title=str(m.get("title") or mid)[:200],
        opened=weather.iso(opened),
        closed=weather.iso(closed),
        seen=weather.iso(seen),
        source=weather.iso(source),
        http=weather.iso(http),
        target_usd=str(target),
        fee_verified=weather.iso(verified),
        fee_source=fee_source,
        fee_until=weather.iso(expiry),
        fee_rate=str(rate),
        bids=bids,
        sample=_samples(rows, opened, seen, venue) if isinstance(rows, list) else [],
        book_url=m["book_url"],
        token=weather.iso(source),
    )


def _fee(venue, rate, fills):
    total_exact, fee = Decimal(0), 0
    for price, qty in fills:
        cents = Decimal(price) / 100
        exact = rate * qty * cents * (1 - cents / 100)
        total_exact += exact
        if venue == "kalshi":
            # The centicent schedule can charge less. Round each aggregated level up
            # to whole cents to keep this integer-cash paper account conservative.
            fee += int(exact.to_integral_value(rounding=ROUND_CEILING))
    if venue != "kalshi":
        # Each price level combines unknown resting orders. The published cap on
        # their cumulative commission bounds the actual per-fill half-even charges.
        fee = int(total_exact.to_integral_value(rounding=ROUND_HALF_EVEN))
    return fee


def _levels(db, b, action, side):
    underlying = _opposite(side) if action == "buy" else side
    used = dict(
        db.execute(
            "SELECT price,qty FROM used WHERE market=? AND book=? AND bid_side=?",
            (b["id"], b["token"], underlying),
        ).fetchall()
    )
    return [
        (10000 - p if action == "buy" else p, n - used.get(p, 0))
        for p, n in b["bids"][underlying]
        if n > used.get(p, 0)
    ]


def _walk(db, venue, b, action, side, qty, limit):
    fills, needed = [], qty
    for p, size in _levels(db, b, action, side):
        if (action == "buy" and p > limit) or (action == "sell" and p < limit):
            break
        n = min(needed, size)
        fills.append((p, n))
        needed -= n
        if not needed:
            break
    if needed:
        raise ValueError("Available whole-contract depth inside the price limit is insufficient.")
    exact = Decimal(sum(p * n for p, n in fills)) / 100
    gross = int(exact.to_integral_value(rounding=ROUND_CEILING if action == "buy" else ROUND_FLOOR))
    fee = _fee(venue, Decimal(b["fee_rate"]), fills)
    delta = -(gross + fee) if action == "buy" else gross - fee
    if action == "sell" and delta < 0:
        raise ValueError("Modeled sale costs exceed the proceeds.")
    return fills, _number(exact / qty), fee, delta


def _signal(b, t):
    points = b["sample"]
    if len(points) != 3 or points[-1][0] != b["seen"]:
        return None, "Waiting for three consecutive fresh, narrow books."
    times = [weather.instant(p[0]) for p in points]
    origins = [p[1] for p in points]
    if (
        not 20 <= (times[-1] - times[0]).total_seconds() <= 45
        or not all(0 < (y - x).total_seconds() <= 20 for x, y in zip(times, times[1:]))
        or not origins[0] < origins[1] < origins[2]
    ):
        return None, "Book times do not meet the 20-second observation rule."
    bid, ask = b["bids"]["yes"][0][0], 10000 - b["bids"]["no"][0][0]
    if points[-1][2:] != [bid, ask] or (weather.instant(b["closed"]) - t).total_seconds() < 60:
        return None, "Waiting for a matching book with at least 60 seconds left."
    a, c, z = [Decimal(p[2] + p[3]) / 200 for p in points]
    if a < c < z and 55 <= z <= 80:
        side = "yes"
    elif a > c > z and 55 <= 100 - z <= 80:
        side = "no"
    else:
        return None, "Midpoint direction or the 55–80¢ entry range has not qualified."
    entry = Decimal(10000 - b["bids"][_opposite(side)][0][0]) / 100
    cost = Decimal(ask - bid) / 100 + 2 * Decimal(b["fee_rate"]) * entry * (1 - entry / 100)
    if abs(z - a) < 6 or abs(z - a) <= cost:
        return None, "The midpoint move does not clear 6¢ and estimated trading costs."
    return side, f"{side.upper()} momentum qualified on three published venue books."


class BTCBroker:
    """One venue product, one SQLite file, one $10,000 practice account."""

    def __init__(self, directory, venue="kalshi"):
        if venue not in PRODUCTS:
            raise ValueError("Unknown BTC paper account.")
        self.venue = venue
        self.exchange = PRODUCTS[venue][0]
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"btc-contract-paper-{venue}.sqlite"
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            self._upgrade(db)
            db.execute(
                "INSERT OR IGNORE INTO account(id,venue,cash,limits) VALUES(1,?,?,?)",
                (venue, INITIAL, _json(LIMITS)),
            )
            if db.execute("SELECT venue FROM account").fetchone()[0] != venue:
                raise ValueError("The BTC account file belongs to another venue.")
            db.commit()

    @staticmethod
    def _upgrade(db):
        """Retain balances and receipt history in early local paper databases."""
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("The BTC account was written by a newer worker.")
        if version == 1:
            return
        for table, columns in {
            "account": {"peak": "INTEGER NOT NULL DEFAULT 1000000", "drawdown": "INTEGER NOT NULL DEFAULT 0"},
            "windows": {
                "cash_flow": "INTEGER NOT NULL DEFAULT 0",
                "fill_count": "INTEGER NOT NULL DEFAULT 0",
                "manual_count": "INTEGER NOT NULL DEFAULT 0",
            },
            "days": {"risk_opening": "INTEGER"},
        }.items():
            present = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        db.execute(
            """UPDATE windows SET (cash_flow,fill_count,manual_count)=(
                   SELECT COALESCE(SUM(json_extract(receipt,'$.cash_delta_cents')),0),COUNT(*),
                          COALESCE(SUM(source='manual'),0) FROM orders WHERE filled=1 AND market=windows.id)"""
        )
        db.execute(
            """UPDATE windows SET cash_flow=cash_flow+COALESCE((SELECT SUM(json_extract(data,'$.payout_cents'))
                   FROM events WHERE kind='settlement' AND json_extract(data,'$.market_id')=windows.id),0)
                   WHERE result IS NOT NULL"""
        )
        db.execute("UPDATE days SET risk_opening=opening WHERE risk_opening IS NULL")
        peak, drawdown = INITIAL, 0
        for (equity,) in db.execute("SELECT equity FROM marks WHERE equity IS NOT NULL ORDER BY at"):
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        db.execute("UPDATE account SET peak=?,drawdown=?", (peak, drawdown))
        db.execute("CREATE INDEX IF NOT EXISTS closed_trades ON windows(closed) WHERE fill_count>0")
        db.execute("PRAGMA user_version=1")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _db(self, write=False):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def _event(self, db, t, kind, message, data=None, realized=0):
        db.execute(
            "INSERT INTO events(at,day,kind,message,realized,data) VALUES(?,?,?,?,?,?)",
            (weather.iso(t), _day(t), kind, message, realized, _json(data or {})),
        )

    def _capture(self, db, feeds, t):
        acct = db.execute("SELECT current_market,seen FROM account").fetchone()
        if acct["seen"] and weather.iso(t) < acct["seen"]:
            return None, "An older observation cannot replace the current paper book."
        book, reason = None, ""
        try:
            book = _book(self.venue, feeds.get(self.venue) if isinstance(feeds, dict) else None, t)
            prior = db.execute("SELECT book,result FROM windows WHERE id=?", (book["id"],)).fetchone()
            if prior:
                old = json.loads(prior["book"])
                if prior["result"] or book["seen"] < old["seen"] or book["source"] < old["source"]:
                    raise ValueError("A settled window or older quote cannot be reused.")
                if book["source"] == old["source"] and book["bids"] != old["bids"]:
                    raise ValueError("The venue published conflicting depth at the same timestamp.")
                if book["bids"] == old["bids"]:
                    book["token"] = old["token"]  # identical depth never replenishes a paper fill
            db.execute(
                """INSERT INTO windows(id,title,opened,closed,book) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET book=excluded.book,title=excluded.title""",
                (book["id"], book["title"], book["opened"], book["closed"], _json(book)),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            reason = (
                str(exc) if isinstance(exc, ValueError) else "Required venue execution evidence is missing."
            )
            book = None
        db.execute(
            "UPDATE account SET current_market=?,seen=?,gate=? WHERE id=1",
            (book["id"] if book else None, weather.iso(t), reason),
        )
        return book, reason

    def _book_for_mark(self, db, t):
        row = db.execute(
            """SELECT w.book FROM account a JOIN windows w ON w.id=a.current_market
                          WHERE a.gate='' AND w.result IS NULL"""
        ).fetchone()
        if not row:
            return None
        b = json.loads(row[0])
        try:
            _check_clock(t, *(weather.instant(b[k]) for k in ("seen", "source", "http", "opened", "closed")))
            if t >= weather.instant(b["fee_until"]):
                return None
        except ValueError:
            return None
        return b

    def _portfolio(self, db, t):
        acc = db.execute("SELECT cash,realized FROM account").fetchone()
        b = self._book_for_mark(db, t)
        positions = []
        rows = db.execute(
            """SELECT h.market,h.side,SUM(h.qty) qty,SUM(h.cost) cost,
                SUM(CASE WHEN owner='auto' THEN h.qty ELSE 0 END) auto_qty,w.title,w.closed
                FROM holdings h JOIN windows w ON h.market=w.id GROUP BY h.market,h.side
                ORDER BY w.closed,h.side"""
        )
        for r in rows:
            mark, at = None, None
            status = (
                "Pending official settlement"
                if weather.instant(r["closed"]) <= t
                else "Fresh bid unavailable"
            )
            if b and b["id"] == r["market"]:
                try:
                    mark = _walk(db, self.exchange, b, "sell", r["side"], r["qty"], 1)[3]
                    status, at = "Fresh executable bid", b["source"]
                except ValueError:
                    status = "Insufficient posted bid depth"
            positions.append(
                dict(
                    venue=self.venue,
                    market_id=r["market"],
                    title=r["title"],
                    side=r["side"],
                    qty=r["qty"],
                    auto_qty=r["auto_qty"],
                    cost_cents=r["cost"],
                    mark_status=status,
                    mark_value_cents=mark,
                    marked_at=at,
                    close_at=r["closed"],
                )
            )
        unknown = sum(p["mark_value_cents"] is None for p in positions)
        cost = sum(p["cost_cents"] for p in positions)
        equity = None if unknown else acc["cash"] + sum(p["mark_value_cents"] for p in positions)
        filled = db.execute("SELECT COUNT(*) FROM orders WHERE filled=1").fetchone()[0]
        return dict(
            initial_cash_cents=INITIAL,
            cash_cents=acc["cash"],
            equity_cents=equity,
            realized_cents=acc["realized"],
            open_cost_cents=cost,
            filled_orders=filled,
            net_gain_cents=equity - INITIAL if equity is not None else None,
            unrealized_cents=equity - acc["cash"] - cost if equity is not None else None,
            unmarked_positions=unknown,
            positions=positions,
            marked_at=None if unknown else (b["source"] if positions else weather.iso(t)),
        )

    def _opening(self, db, day, portfolio, t):
        prior = db.execute("SELECT * FROM days WHERE day<? ORDER BY day DESC LIMIT 1", (day,)).fetchone()
        if prior and prior["positions"]:
            # A pre-midnight quote only supplies an opening if it remains executable at
            # midnight. Do not carry a closed window or a stale last mark through midnight.
            return None, None
        return portfolio["cash_cents"], weather.iso(t)

    def _start_day(self, db, t):
        day = _day(t)
        if db.execute("SELECT 1 FROM days WHERE day=?", (day,)).fetchone():
            return
        p = self._portfolio(db, t)
        opening, at = self._opening(db, day, p, t)
        # A carried position can have an unknowable opening mark. Its maximum payout
        # still gives an upper bound for the opening, sufficient for conservative risk.
        maximum_opening = p["cash_cents"] + 100 * sum(x["qty"] for x in p["positions"])
        db.execute(
            "INSERT INTO days(day,opening,opening_at,risk_opening) VALUES(?,?,?,?)",
            (day, opening, at, maximum_opening if opening is None else opening),
        )
        cutoff = weather.iso(t - timedelta(days=8))
        db.execute("DELETE FROM used WHERE market IN (SELECT id FROM windows WHERE closed<?)", (cutoff,))
        db.execute(
            """DELETE FROM windows WHERE closed<? AND id NOT IN (SELECT market FROM orders)
                      AND id NOT IN (SELECT market FROM holdings)""",
            (cutoff,),
        )

    def _checkpoint(self, db, t, *, force=False):
        p, at = self._portfolio(db, t), weather.iso(t)
        equity, unknown = p["equity_cents"], p["unmarked_positions"]
        db.execute(
            """UPDATE days SET closing=?,as_of=?,positions=?,unmarked=?,
                last_known=CASE WHEN ? IS NULL THEN last_known ELSE ? END,
                last_known_at=CASE WHEN ? IS NULL THEN last_known_at ELSE ? END WHERE day=?
                AND (as_of IS NULL OR as_of<=?)""",
            (equity, at, len(p["positions"]), unknown, equity, equity, equity, p["marked_at"], _day(t), at),
        )
        if equity is not None:
            db.execute("UPDATE account SET peak=MAX(peak,?), drawdown=MAX(drawdown,peak-?)", (equity, equity))
        last = db.execute("SELECT * FROM marks ORDER BY at DESC LIMIT 1").fetchone()
        if not last or (
            at >= last["at"]
            and (force or last["at"][:16] != at[:16] or bool(last["unmarked"]) != bool(unknown))
        ):
            db.execute("INSERT OR REPLACE INTO marks VALUES(?,?,?,?)", (at, equity, p["cash_cents"], unknown))
            db.execute("DELETE FROM marks WHERE at<?", (weather.iso(t - timedelta(days=8)),))

    def _order(self, order):
        if not isinstance(order, dict) or order.get("venue") != self.venue:
            raise ValueError("Choose the BTC contract account that owns this order.")
        cid = order.get("client_id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", cid):
            raise ValueError("A valid unique client order ID is required.")
        mid = _market_id(self.venue, order.get("market_id"))
        action, side, qty = order.get("action"), order.get("side"), order.get("qty")
        if action not in ("buy", "sell") or side not in SIDES:
            raise ValueError("Choose a buy or sell and a YES or NO side.")
        if type(qty) is not int or not 1 <= qty <= HARD_LIMITS["max_order_qty"]:
            raise ValueError("Enter a whole number of contracts from 1 to 100.")
        price = _price(order.get("limit_cents"))
        return dict(
            client_id=cid,
            venue=self.venue,
            market_id=mid,
            action=action,
            side=side,
            qty=qty,
            limit_cents=_number(Decimal(price) / 100),
        )

    def _existing(self, db, order):
        old = db.execute("SELECT req,receipt FROM orders WHERE id=?", (order["client_id"],)).fetchone()
        if old:
            if _json(order) != old["req"]:
                raise ValueError("This client order ID already belongs to a different order.")
            return json.loads(old["receipt"])
        return None

    def _risk(self, db, order, delta, t, limits):
        if order["qty"] > limits["max_order_qty"]:
            raise ValueError("Quantity exceeds the account's per-order limit.")
        if order["action"] != "buy":
            return
        p = self._portfolio(db, t)
        cost = -delta
        window_cost = sum(x["cost_cents"] for x in p["positions"] if x["market_id"] == order["market_id"])
        if cost > p["cash_cents"]:
            raise ValueError("Available paper cash does not cover the premium and fee.")
        if cost > limits["max_order_cents"]:
            raise ValueError("Premium and fee exceed the per-order budget.")
        if window_cost + cost > limits["max_window_cents"]:
            raise ValueError("The window exposure limit would be exceeded.")
        if p["open_cost_cents"] + cost > limits["max_open_cents"]:
            raise ValueError("The account's total open exposure limit would be exceeded.")
        if p["unmarked_positions"]:
            raise ValueError("An existing position has no fresh liquidation mark.")
        opening = db.execute("SELECT risk_opening FROM days WHERE day=?", (_day(t),)).fetchone()[0]
        if opening is None:
            raise ValueError("The daily opening equity is unknown while prior risk remains.")
        # Assume that every remaining contract pays zero. This bounds today's net loss
        # even if liquidity disappears before an exit. Sales never need a new risk budget.
        if opening - (p["cash_cents"] - cost) > limits["daily_loss_cents"]:
            raise ValueError("The account's Eastern daily loss budget would be exceeded.")

    def _buy(self, db, market, side, owner, qty, delta):
        db.execute(
            """INSERT INTO holdings(market,side,owner,qty,cost) VALUES(?,?,?,?,?)
                      ON CONFLICT(market,side,owner) DO UPDATE SET
                      qty=qty+excluded.qty,cost=cost+excluded.cost""",
            (market, side, owner, qty, -delta),
        )

    def _sell(self, db, market, side, owner, qty, delta):
        rows = db.execute(
            """SELECT owner,qty,cost FROM holdings WHERE market=? AND side=?
                           AND (?='manual' OR owner='auto') ORDER BY (owner='manual') DESC""",
            (market, side, owner),
        ).fetchall()
        if sum(r["qty"] for r in rows) < qty:
            raise ValueError("The account does not hold that many contracts on this side.")
        allocated = 0
        for r in rows:
            n = min(qty, r["qty"])
            cost = r["cost"] * n // r["qty"]
            if n == r["qty"]:
                db.execute(
                    "DELETE FROM holdings WHERE market=? AND side=? AND owner=?", (market, side, r["owner"])
                )
            else:
                db.execute(
                    "UPDATE holdings SET qty=qty-?,cost=cost-? WHERE market=? AND side=? AND owner=?",
                    (n, cost, market, side, r["owner"]),
                )
            allocated += cost
            qty -= n
            if not qty:
                break
        return delta - allocated

    def _execute(self, db, order, owner, b, gate, t, purpose=None):
        """Validate every gate before writing an accounting or liquidity entry."""
        action, side = order["action"], order["side"]
        mid, qty = order["market_id"], order["qty"]
        receipt = dict(
            **order,
            source=owner,
            at=weather.iso(t),
            status="rejected",
            reason="",
            fee_cents=0,
            price_cents=None,
            cash_amount_cents=0,
            cash_delta_cents=0,
            realized_cents=0,
            levels=[],
        )
        try:
            if not b or b["id"] != mid:
                raise ValueError(gate or "The selected market is no longer the current BTC window.")
            levels, price, fee, delta = _walk(
                db, self.exchange, b, action, side, qty, _price(order["limit_cents"])
            )
            limits = json.loads(db.execute("SELECT limits FROM account").fetchone()[0])
            self._risk(db, order, delta, t, limits)
            realized = 0
            if action == "buy":
                self._buy(db, mid, side, owner, qty, delta)
            else:
                realized = self._sell(db, mid, side, owner, qty, delta)
            db.execute("UPDATE account SET cash=cash+?,realized=realized+?", (delta, realized))
            db.execute(
                "UPDATE windows SET cash_flow=cash_flow+?,fill_count=fill_count+1,manual_count=manual_count+? WHERE id=?",
                (delta, int(owner == "manual"), mid),
            )
            underlying = _opposite(side) if action == "buy" else side
            for p, n in levels:
                db.execute(
                    """INSERT INTO used VALUES(?,?,?,?,?) ON CONFLICT(market,book,bid_side,price)
                              DO UPDATE SET qty=qty+excluded.qty""",
                    (mid, b["token"], underlying, 10000 - p if action == "buy" else p, n),
                )
            receipt.update(
                status="filled",
                reason=purpose or "Filled against the posted venue book.",
                price_cents=price,
                fee_cents=fee,
                cash_amount_cents=abs(delta),
                cash_delta_cents=delta,
                realized_cents=realized,
                quote_at=b["seen"],
                source_at=b["source"],
                book_url=b["book_url"],
                fee_rate=b["fee_rate"],
                fee_source=b["fee_source"],
                fee_verified_at=b["fee_verified"],
                fee_valid_until=b["fee_until"],
                open_at=b["opened"],
                close_at=b["closed"],
                target_usd=b["target_usd"],
                levels=[[_number(Decimal(p) / 100), n] for p, n in levels],
            )
            if owner == "auto":
                receipt["rule_version"] = "venue-book-momentum-v1"
                receipt["signal_evidence"] = [
                    dict(
                        observed_at=at,
                        source_at=source,
                        yes_bid_cents=_number(Decimal(bid) / 100),
                        yes_ask_cents=_number(Decimal(ask) / 100),
                    )
                    for at, source, bid, ask in b["sample"]
                ]
        except ValueError as exc:
            receipt["reason"] = str(exc)
        filled = receipt["status"] == "filled"
        # A failed automatic probe remains eligible for a later book; do not log a fresh
        # rejection every time the background loop checks unchanged risk or depth.
        if filled or owner == "manual":
            db.execute(
                "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    order["client_id"],
                    _json(order),
                    weather.iso(t),
                    _day(t),
                    owner,
                    mid,
                    action,
                    int(filled),
                    receipt["fee_cents"],
                    _json(receipt),
                ),
            )
            message = (
                f"Paper {action} filled for {qty} {side.upper()} at {receipt['price_cents']}¢."
                if filled
                else receipt["reason"]
            )
            self._event(db, t, "fill" if filled else "rejected", message, receipt, receipt["realized_cents"])
        return receipt

    def place(self, order, feeds, now=None):
        order = self._order(order)
        if order["client_id"].startswith("auto:"):
            raise ValueError("The auto order ID prefix is reserved.")
        with self._db(write=True) as db:
            existing = self._existing(db, order)
            if existing is not None:
                return existing
            t = _now(now)
            watermark = db.execute("SELECT seen FROM account").fetchone()[0]
            if watermark and weather.iso(t) < watermark:
                return self._execute(db, order, "manual", None, "The worker clock moved backwards.", t)
            self._start_day(db, t)
            book, gate = self._capture(db, feeds, t)
            result = self._execute(db, order, "manual", book, gate, t)
            self._checkpoint(db, t, force=result["status"] == "filled")
            return result

    def _auto(self, db, b, t):
        filled = False
        market, stamp = b["id"], b["seen"]
        elapsed = (weather.instant(b["closed"]) - t).total_seconds()
        limits = json.loads(db.execute("SELECT limits FROM account").fetchone()[0])
        held = db.execute(
            "SELECT side,qty,cost FROM holdings WHERE market=? AND owner='auto'", (market,)
        ).fetchall()
        for row in held:
            side, total = row["side"], row["qty"]
            bid = b["bids"][side][0][0]
            ask = 10000 - b["bids"][_opposite(side)][0][0]
            available = _levels(db, b, "sell", side)
            qty = min(total, limits["max_order_qty"], sum(n for _, n in available))
            if not qty:
                continue
            worst_allowed = max(1, bid - 100)  # at most 1¢ beyond the visible best quote
            qty = min(qty, sum(n for p, n in available if p >= worst_allowed))
            if not qty:
                continue
            try:
                net = _walk(db, self.exchange, b, "sell", side, qty, worst_allowed)[3]
            except ValueError:
                continue
            profit = net - Decimal(row["cost"]) * qty / total
            if elapsed <= 30:
                reason = "The contract is in its last 30 seconds."
            elif (bid + ask) <= 10000:
                reason = "The held side's midpoint retraced to 50¢ or below."
            elif profit >= 8 * qty:
                reason = "Executable gain reached 8¢ per contract after fees."
            else:
                continue
            order = self._order(
                dict(
                    client_id=f"auto:exit:{market}:{side}:{stamp}",
                    venue=self.venue,
                    market_id=market,
                    action="sell",
                    side=side,
                    qty=qty,
                    limit_cents=worst_allowed / 100,
                )
            )
            if not db.execute("SELECT 1 FROM orders WHERE id=?", (order["client_id"],)).fetchone():
                filled = self._execute(db, order, "auto", b, "", t, reason)["status"] == "filled" or filled
        if (
            held
            or db.execute(
                "SELECT 1 FROM orders WHERE market=? AND action='buy' AND source='auto' AND filled=1",
                (market,),
            ).fetchone()
        ):
            return filled
        side, reason = _signal(b, t)
        if not side:
            return filled
        levels = _levels(db, b, "buy", side)
        if not levels:
            return filled
        # No auto fill may walk more than 1¢ beyond the actual top quote.
        best = 10000 - b["bids"][_opposite(side)][0][0]
        cap = min(9999, best + 100)
        qty = min(5, limits["max_order_qty"], sum(n for p, n in levels if p <= cap))
        # Respect a user's reduced budget and the remaining risk room even for a single lot.
        samples = b["sample"]
        move = abs(Decimal(sum(samples[-1][2:]) - sum(samples[0][2:]))) / 200
        exit_limit = max(1, b["bids"][side][0][0] - 100)
        for n in range(qty, 0, -1):
            try:
                debit = -_walk(db, self.exchange, b, "buy", side, n, cap)[3]
                liquidation = _walk(db, self.exchange, b, "sell", side, n, exit_limit)[3]
            except ValueError:
                continue
            if move * n <= debit - liquidation:
                continue
            order = self._order(
                dict(
                    client_id=f"auto:entry:{market}",
                    venue=self.venue,
                    market_id=market,
                    action="buy",
                    side=side,
                    qty=n,
                    limit_cents=cap / 100,
                )
            )
            if self._execute(db, order, "auto", b, "", t, reason)["status"] == "filled":
                return True
        return filled

    def _settle(self, db, record, t):
        if not isinstance(record, dict) or record.get("venue") not in (self.venue, self.exchange):
            return False
        try:
            mid = _market_id(self.venue, record.get("market_id"))
            at = weather.instant(record.get("observed_at"))
        except ValueError:
            return False
        result = record.get("result")
        source = record.get("source")
        if result not in ("YES", "NO") or not _source_url(self.exchange, mid, source, book=False):
            # An official binary payout is the only supported outcome. A pending,
            # disputed, scalar, or unspecified void remains open and visible.
            return False
        win = db.execute("SELECT closed,result FROM windows WHERE id=?", (mid,)).fetchone()
        if not win or not weather.instant(win["closed"]) <= at <= t:
            return False
        if win["result"]:
            if win["result"] != result:
                raise ValueError("The official BTC result conflicts with its recorded settlement.")
            return False
        rows = db.execute("SELECT side,qty,cost FROM holdings WHERE market=?", (mid,)).fetchall()
        if not rows:
            return False
        payout = sum(100 * r["qty"] for r in rows if r["side"] == result.lower())
        cost = sum(r["cost"] for r in rows)
        db.execute("UPDATE account SET cash=cash+?,realized=realized+?", (payout, payout - cost))
        db.execute("DELETE FROM holdings WHERE market=?", (mid,))
        db.execute(
            "UPDATE windows SET result=?,resolved=?,source=?,cash_flow=cash_flow+? WHERE id=?",
            (result, weather.iso(at), source, payout, mid),
        )
        self._event(
            db,
            t,
            "settlement",
            f"Official {result} result settled the paper contracts.",
            dict(
                venue=self.venue,
                market_id=mid,
                result=result,
                payout_cents=payout,
                cost_cents=cost,
                realized_cents=payout - cost,
                source=source,
                observed_at=weather.iso(at),
            ),
            payout - cost,
        )
        return True

    def pending_settlements(self, now=None):
        t = weather.iso(_now(now))
        with self._db() as db:
            rows = db.execute(
                """SELECT DISTINCT w.id,w.closed FROM windows w JOIN holdings h ON h.market=w.id
                WHERE w.closed<=? AND w.result IS NULL ORDER BY w.closed""",
                (t,),
            )
            return [dict(venue=self.venue, market_id=r["id"], close_at=r["closed"]) for r in rows]

    def sync(self, feeds, settlements=(), now=None, *, allow_auto=True):
        with self._db(write=True) as db:
            t = _now(now)
            watermark = db.execute("SELECT seen FROM account").fetchone()[0]
            if watermark and weather.iso(t) < watermark:
                return
            self._start_day(db, t)
            book, _ = self._capture(db, feeds, t)
            settled = False
            for record in settlements:
                settled = self._settle(db, record, t) or settled
            filled = False
            if book and allow_auto is True and db.execute("SELECT auto FROM account").fetchone()[0]:
                filled = self._auto(db, book, t)
            self._checkpoint(db, t, force=settled or filled)

    def set_auto(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("The automatic paper trading setting must be true or false.")
        with self._db(write=True) as db:
            if db.execute("SELECT auto FROM account").fetchone()[0] != enabled:
                db.execute("UPDATE account SET auto=?", (int(enabled),))
                self._event(
                    db,
                    _now(None),
                    "settings",
                    "Automatic paper trading enabled." if enabled else "Automatic paper trading paused.",
                )
        return enabled

    def set_limits(self, limits):
        if not isinstance(limits, dict) or not limits or not set(limits) <= LIMITS.keys():
            raise ValueError("Choose a supported paper risk limit.")
        for k, n in limits.items():
            if type(n) is not int or not 1 <= n <= HARD_LIMITS[k]:
                raise ValueError(f"{k} must be a whole number from 1 to {HARD_LIMITS[k]}.")
        with self._db(write=True) as db:
            old = json.loads(db.execute("SELECT limits FROM account").fetchone()[0])
            new = {**old, **limits}
            if new != old:
                db.execute("UPDATE account SET limits=?", (_json(new),))
                self._event(db, _now(None), "settings", "Paper risk limits updated.", new)
        return new

    def _daily(self, db, day, portfolio=None, t=None):
        row = db.execute("SELECT * FROM days WHERE day=?", (day,)).fetchone()
        current = portfolio is not None
        if row:
            opening, opening_at = row["opening"], row["opening_at"]
        else:
            opening, opening_at = self._opening(db, day, portfolio, t)
        if current:
            equity, at = portfolio["equity_cents"], weather.iso(t)
            count, unknown = len(portfolio["positions"]), portfolio["unmarked_positions"]
        else:
            equity, at, count, unknown = row["closing"], row["as_of"], row["positions"], row["unmarked"]
            # When the worker slept with risk outstanding its final mark was not an EOD
            # quote. Preserve that observation explicitly, but never call it a closing P&L.
            # All three supported product windows end on the Eastern midnight boundary.
            if count:
                equity, unknown = None, count
        orders = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(fee),0) FROM orders WHERE day=? AND filled=1", (day,)
        ).fetchone()
        realized = db.execute("SELECT COALESCE(SUM(realized),0) FROM events WHERE day=?", (day,)).fetchone()[
            0
        ]
        net = equity - opening if equity is not None and opening is not None else None
        return dict(
            date=day,
            timezone="America/New_York",
            opening_equity_cents=opening,
            opening_at=opening_at,
            equity_cents=equity,
            net_gain_cents=net,
            realized_cents=realized,
            fees_cents=orders[1],
            filled_orders=orders[0],
            open_positions=count,
            unmarked_positions=unknown,
            as_of=at,
            last_known_equity_cents=row["last_known"] if row else equity,
            last_known_at=row["last_known_at"] if row else at,
            marked_at=(
                portfolio["marked_at"] if current else (row["last_known_at"] if equity is not None else None)
            ),
        )

    def _performance(self, db, t):
        # One outcome per expired, fully closed traded window. A partial sale cannot
        # classify a window, and mixed manual/automatic windows stay out of auto-only.
        rows = db.execute(
            """SELECT cash_flow pnl, manual_count manual FROM windows
                           WHERE fill_count>0 AND closed<=? AND id NOT IN (SELECT market FROM holdings)""",
            (weather.iso(t),),
        ).fetchall()
        risk = db.execute("SELECT peak,drawdown FROM account").fetchone()

        def counts(profits):
            wins, losses = sum(n > 0 for n in profits), sum(n < 0 for n in profits)
            return dict(
                closed_windows=len(profits),
                profitable_windows=wins,
                loss_windows=losses,
                flat_windows=len(profits) - wins - losses,
                net_cents=sum(profits),
                win_rate_pct=100 * wins / len(profits) if profits else None,
            )

        return dict(
            **counts([r["pnl"] for r in rows]),
            auto_only=counts([r["pnl"] for r in rows if not r["manual"]]),
            max_drawdown_cents=risk["drawdown"],
            peak_equity_cents=risk["peak"],
            denominator="Expired or settled, fully closed traded windows, including break-even windows.",
        )

    def state(self, now=None):
        t = _now(now)
        with self._db() as db:
            acc = db.execute("SELECT * FROM account").fetchone()
            portfolio = self._portfolio(db, t)
            b = self._book_for_mark(db, t)
            markets = []
            if acc["current_market"]:
                raw = db.execute("SELECT book FROM windows WHERE id=?", (acc["current_market"],)).fetchone()
                stored = json.loads(raw[0])
                side, signal = _signal(b, t) if b else (None, "Waiting for a fresh, verified venue book.")
                markets.append(
                    dict(
                        venue=self.venue,
                        market_id=stored["id"],
                        title=stored["title"],
                        observed_at=stored["seen"],
                        close_at=stored["closed"],
                        tradable=b is not None,
                        reason="" if b else "The last venue quote is stale or outside its window.",
                        signal=signal,
                        signal_side=side,
                        fee_rate=stored["fee_rate"],
                        fee_source=stored["fee_source"],
                    )
                )
            fills = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT receipt FROM orders WHERE filled=1 ORDER BY at DESC,rowid DESC LIMIT 100"
                )
            ]
            events = [
                dict(id=r["id"], at=r["at"], kind=r["kind"], message=r["message"], data=json.loads(r["data"]))
                for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")
            ]
            marks = [
                dict(
                    at=r["at"],
                    equity_cents=r["equity"],
                    cash_cents=r["cash"],
                    unmarked_positions=r["unmarked"],
                )
                for r in db.execute("SELECT * FROM marks ORDER BY at DESC LIMIT 180")
            ][::-1]
            day = _day(t)
            previous = [
                r[0]
                for r in db.execute("SELECT day FROM days WHERE day<? ORDER BY day DESC LIMIT 30", (day,))
            ]
            return dict(
                venue=self.venue,
                auto_enabled=bool(acc["auto"]),
                limits=json.loads(acc["limits"]),
                max_limits=HARD_LIMITS.copy(),
                rule=RULE,
                fee_policy=FEES,
                portfolio=portfolio,
                positions=portfolio["positions"],
                fills=fills,
                history=marks,
                activity=events,
                markets=markets,
                gate_reason=acc["gate"] or (None if b else "Waiting for a fresh venue book."),
                daily=self._daily(db, day, portfolio, t),
                daily_history=[self._daily(db, d) for d in previous],
                performance=self._performance(db, t),
                started_at=db.execute(
                    """SELECT MIN(at) FROM (SELECT at FROM events WHERE
                            kind='settings' AND message='Automatic paper trading enabled.'
                            UNION ALL SELECT at FROM orders WHERE filled=1)"""
                ).fetchone()[0],
            )
