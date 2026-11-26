"""Single-owner BTC/USD spot paper account using public Bitstamp market data."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
from pathlib import Path
import sqlite3
import ssl
from threading import Event, Lock
from uuid import UUID
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import equity as equity_history

UTC = timezone.utc
DAY, HOUR = 86400, 3600
START_CENTS, MAX_ENTRY_CENTS = 1_000_000, 50_000
SATOSHIS = 100_000_000
FEE, SLIPPAGE = Decimal("0.006"), Decimal("0.0005")
POLL_SECONDS, QUOTE_TTL = 30, 30
BASE = "https://www.bitstamp.net/api/v2"
BOOK = BASE + "/order_book/btcusd/"
CANDLES = BASE + "/ohlc/btcusd/"


def clock():
    return datetime.now(UTC)


def stamp(value):
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def moment(value):
    if not isinstance(value, str):
        raise ValueError("Missing exchange timestamp.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing exchange timezone.")
    return result.astimezone(UTC)


def utc_at(epoch):
    return datetime.fromtimestamp(epoch, UTC)


def dec(value):
    if isinstance(value, bool) or len(str(value)) > 45:
        raise ValueError("Invalid market number.")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid market number.") from None
    if (
        not result.is_finite()
        or not 0 <= result <= 1_000_000_000_000
        or result.as_tuple().exponent < -18
    ):
        raise ValueError("Invalid market number.")
    return result


def price_text(value):
    return format(value.normalize(), "f")


def completed_closes(rows, period, as_of, count):
    """Only aligned, closed buckets with valid OHLC and no conflicting duplicate."""
    if not isinstance(rows, list) or len(rows) > 350:
        raise ValueError("Unexpected Bitstamp candle response.")
    end = int(as_of.timestamp()) // period * period
    start = end - count * period
    closes = {}
    conflicts = set()
    for row in rows:
        if isinstance(row, dict):
            ts = row.get("timestamp")
            if (
                not isinstance(ts, (str, int))
                or not str(ts).isdecimal()
                or len(str(ts)) > 12
            ):
                continue
            row = [
                int(ts),
                *(row.get(k) for k in ("low", "high", "open", "close", "volume")),
            ]
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        t = row[0]
        if type(t) is not int or t % period or not start <= t < end:
            continue
        try:
            low, high, opened, close, volume = map(dec, row[1:6])
        except ValueError:
            continue
        if (
            volume <= 0
            or low <= 0
            or not low <= opened <= high
            or not low <= close <= high
        ):
            continue
        if t in closes and closes[t] != close:
            conflicts.add(t)
        closes[t] = close
    return {t: c for t, c in closes.items() if t not in conflicts}


def _signal(closes, as_of):
    end = int(as_of.timestamp()) // DAY * DAY
    starts = range(end - 8 * DAY, end, DAY)
    recent = utc_at(starts[-1]).date().isoformat()
    earlier = utc_at(starts[0]).date().isoformat()
    result = dict(
        target=None,
        day=recent,
        reference_day=earlier,
        close_usd=None,
        reference_close_usd=None,
    )
    if any(t not in closes for t in starts):
        return {
            **result,
            "reason": "Waiting for eight consecutive completed UTC daily closes through "
            + recent
            + ".",
        }
    last, first = dec(closes[starts[-1]]), dec(closes[starts[0]])
    long = last > first
    return {
        **result,
        "target": "long" if long else "cash",
        "close_usd": price_text(last),
        "reference_close_usd": price_text(first),
        "reason": f"The {recent} UTC close of ${last:,.2f} was "
        f"{'above' if long else 'at or below'} the {earlier} close of ${first:,.2f}. "
        f"The rule calls for {'a long BTC position' if long else 'cash'}.",
    }


def daily_signal(candles, as_of):
    """Apply the predeclared seven-day rule to eight completed Bitstamp daily candles."""
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("An aware UTC cutoff is required.")
    return _signal(completed_closes(candles, DAY, as_of, 8), as_of)


class FeedError(Exception):
    def __init__(self, message, retry=30, throttled=False):
        super().__init__(message)
        self.retry = retry
        self.throttled = throttled


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FeedError("Bitstamp returned an unexpected redirect.")


def public_read(url):
    """The worker constructs only the public BTC-USD book and candle GET endpoints."""
    if url != BOOK and not url.startswith(CANDLES + "?"):
        raise ValueError("Unexpected public BTC endpoint.")
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and Path("/etc/ssl/cert.pem").is_file():
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    req = Request(
        url,
        headers={
            "User-Agent": "PMB/0.1 (personal BTC paper research)",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with build_opener(HTTPSHandler(context=ctx), NoRedirects()).open(
            req, timeout=6
        ) as reply:
            limit = 2_000_000 if url == BOOK else 200_000
            body = reply.read(limit + 1)
            seen = clock()
            origin = parsedate_to_datetime(reply.headers["Date"]).astimezone(UTC)
            age = int(reply.headers.get("Age", 0))
        if (
            len(body) > limit
            or age < 0
            or age > QUOTE_TTL
            or not -2 <= (seen - origin).total_seconds() <= QUOTE_TTL
        ):
            raise FeedError("Bitstamp response is oversized or stale.")
        return json.loads(body, parse_float=Decimal), seen, origin
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After", "")
        try:
            delay = (
                int(retry)
                if retry.isdecimal()
                else int(
                    (
                        parsedate_to_datetime(retry).astimezone(UTC) - clock()
                    ).total_seconds()
                )
            )
        except (ValueError, TypeError, OverflowError):
            delay = 60
        finally:
            exc.close()
        raise FeedError(
            f"Bitstamp returned HTTP {exc.code}.",
            min(366 * DAY, max(30, delay)) if exc.code == 429 else 30,
            throttled=exc.code == 429,
        ) from None
    except (HTTPException, URLError, OSError, ValueError, TypeError, KeyError) as exc:
        raise FeedError("Bitstamp market data is temporarily unavailable.") from exc


def parse_book(data, seen):
    if not isinstance(data, dict):
        raise ValueError("BTC book is unavailable.")
    micros, seconds = str(data.get("microtimestamp", "")), str(
        data.get("timestamp", "")
    )
    if not (
        micros.isdecimal()
        and len(micros) <= 17
        and seconds.isdecimal()
        and len(seconds) <= 11
    ):
        raise ValueError("BTC book is missing its exchange timestamp.")
    epoch, fraction = divmod(int(micros), 1_000_000)
    if epoch != int(seconds) or abs(epoch - int(seen.timestamp())) > QUOTE_TTL + 1:
        raise ValueError("BTC book is stale or its timestamps disagree.")
    source = utc_at(epoch).replace(microsecond=fraction)
    if not 0 <= (seen - source).total_seconds() <= QUOTE_TTL:
        raise ValueError("BTC book is stale or its timestamp is invalid.")
    sides = []
    for name in ("bids", "asks"):
        levels = data.get(name)
        if (
            not isinstance(levels, list)
            or not levels
            or not isinstance(levels[0], list)
            or len(levels[0]) < 2
        ):
            raise ValueError("BTC book has no executable top level.")
        price, size = map(dec, levels[0][:2])
        if min(price, size) <= 0:
            raise ValueError("BTC book has no executable top level.")
        sides.extend([price, size])
    bid, bid_size, ask, ask_size = sides
    if bid >= ask:
        raise ValueError("BTC book is locked or crossed.")
    return {
        "bid_usd": price_text(bid),
        "ask_usd": price_text(ask),
        "bid_size_btc": price_text(bid_size),
        "ask_size_btc": price_text(ask_size),
        "mid_usd": price_text((bid + ask) / 2),
        "source_at": stamp(source),
        "observed_at": stamp(seen),
        "microtimestamp": micros,
    }


def quote_fresh(quote, now):
    return bool(
        quote
        and 0 <= (now - moment(quote["source_at"])).total_seconds() <= QUOTE_TTL
        and 0 <= (now - moment(quote["observed_at"])).total_seconds() <= QUOTE_TTL
    )


def cash_terms(side, sats, top_price):
    price = dec(top_price) * (1 + SLIPPAGE if side == "buy" else 1 - SLIPPAGE)
    raw = Decimal(sats) * price * 100 / SATOSHIS
    cents = int(
        raw.to_integral_value(rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR)
    )
    fee = int((raw * FEE).to_integral_value(rounding=ROUND_CEILING))
    return (cents + fee if side == "buy" else cents - fee), fee, price


def entry_size(budget, ask):
    # Rounding notional and fee independently can put the nominal maximum one cent over budget.
    lo, hi = 0, int(Decimal(budget) * SATOSHIS / (dec(ask) * 100))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cash_terms("buy", mid, ask)[0] <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


class BitcoinPaper:
    def __init__(self, directory, online=True):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "bitcoin.sqlite"
        self.online = bool(online)
        self._wake = Event()
        self._scan_lock = Lock()
        with closing(self._db()) as db, db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS trial(
                    id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER DEFAULT 0,auto INTEGER DEFAULT 0,
                    generation INTEGER DEFAULT 0,status TEXT DEFAULT 'BTC public data is off.',
                    last_checked TEXT,started TEXT,next_poll INTEGER DEFAULT 0,errors INTEGER DEFAULT 0);
                INSERT OR IGNORE INTO trial(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS cache(name TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candles(period INTEGER,start INTEGER,close TEXT NOT NULL,seen TEXT NOT NULL,
                    PRIMARY KEY(period,start));
                CREATE TABLE IF NOT EXISTS accounts(name TEXT PRIMARY KEY,cash INTEGER NOT NULL,sats INTEGER DEFAULT 0,
                    cost INTEGER DEFAULT 0,realized INTEGER DEFAULT 0,fills INTEGER DEFAULT 0,equity INTEGER,
                    marked TEXT,peak INTEGER NOT NULL,drawdown REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,account TEXT,signal_day TEXT,side TEXT,
                    at TEXT,sats INTEGER,price TEXT,fee INTEGER,amount INTEGER,quote_at TEXT,
                    UNIQUE(account,signal_day));
                CREATE INDEX IF NOT EXISTS spot_quote_fills ON fills(account,side,quote_at);
                CREATE TABLE IF NOT EXISTS manual_spot_orders(
                    client_id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    fill_id INTEGER UNIQUE REFERENCES fills(id), rejection TEXT,
                    CHECK((fill_id IS NULL) != (rejection IS NULL)));
                CREATE TABLE IF NOT EXISTS marks(bucket INTEGER,baseline INTEGER,at TEXT,equity INTEGER,hold INTEGER,
                    PRIMARY KEY(bucket,baseline));"""
            )
            db.execute(equity_history.SCHEMA)
            if not db.execute("SELECT 1 FROM portfolio_marks LIMIT 1").fetchone():
                db.execute("""INSERT OR REPLACE INTO portfolio_marks SELECT at,equity FROM marks
                              WHERE bucket>=? ORDER BY bucket,baseline DESC""",
                           (int(clock().timestamp()) // 300 - 8*288,))
            for name in ("strategy", "hold"):
                db.execute(
                    "INSERT OR IGNORE INTO accounts(name,cash,equity,peak) VALUES(?,?,?,?)",
                    (name, START_CENTS, START_CENTS, START_CENTS),
                )

    def _db(self):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _get(db, name, default=None):
        row = db.execute("SELECT value FROM cache WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put(db, name, value):
        db.execute(
            "INSERT OR REPLACE INTO cache VALUES(?,?)",
            (name, json.dumps(value, separators=(",", ":"))),
        )

    def configure(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("BTC public-data permission must be on or off.")
        with closing(self._db()) as db, db:
            changed = db.execute(
                """UPDATE trial SET enabled=?,auto=CASE WHEN ? THEN auto ELSE 0 END,
                        generation=generation+1,next_poll=0,errors=0,status=? WHERE id=1 AND enabled<>?""",
                (
                    enabled,
                    enabled,
                    (
                        "Waiting for Bitstamp market data."
                        if enabled
                        else "BTC public data is off."
                    ),
                    enabled,
                ),
            ).rowcount
            if changed:
                db.execute("DELETE FROM cache WHERE name='quote'")
        self._wake.set()

    def set_auto(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("BTC paper trading must be on or off.")
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT enabled,auto FROM trial WHERE id=1").fetchone()
            if enabled and (not self.online or not row["enabled"]):
                raise ValueError("Enable BTC public data on an online worker first.")
            if bool(row["auto"]) != enabled:
                db.execute(
                    "UPDATE trial SET auto=?,generation=generation+1,next_poll=0 WHERE id=1",
                    (enabled,),
                )
        self._wake.set()

    def _allowed(self, generation, stop):
        if not self.online or (stop is not None and stop.is_set()):
            return False
        with closing(self._db()) as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM trial WHERE id=1 AND enabled=1 AND generation=?",
                    (generation,),
                ).fetchone()
            )

    @staticmethod
    def _closes(db, period, now, count):
        end = int(now.timestamp()) // period * period
        return {
            r["start"]: r["close"]
            for r in db.execute(
                "SELECT start,close FROM candles WHERE period=? AND start>=? AND start<? AND seen<=? ORDER BY start",
                (period, end - count * period, end, stamp(now)),
            )
        }

    def poll_once(self, stop=None):
        """One due poll; tests and the background thread share this same execution path."""
        if not self.online or not self._scan_lock.acquire(blocking=False):
            return POLL_SECONDS
        try:
            return self._poll(stop)
        finally:
            self._scan_lock.release()

    def _poll(self, stop):
        start = clock()
        epoch = int(start.timestamp())
        with closing(self._db()) as db:
            trial = db.execute("SELECT * FROM trial WHERE id=1").fetchone()
            if not trial["enabled"] or (stop is not None and stop.is_set()):
                return POLL_SECONDS
            next_poll = max(trial["next_poll"], self._get(db, "host_backoff", 0))
            if next_poll > epoch:
                return next_poll - epoch
            due = [
                (p, n)
                for p, n in ((DAY, 8), (HOUR, 168))
                if self._get(db, f"next:{p}", 0) <= epoch
            ]
        generation = trial["generation"]
        batches = []
        failed_candles = []
        notes = []
        retry = POLL_SECONDS
        throttled = False
        book = None
        for period, count in due:
            if not self._allowed(generation, stop):
                return POLL_SECONDS
            end = epoch // period * period
            params = {"step": period, "limit": count, "exclude_current_candle": "true"}
            try:
                payload, seen, origin = public_read(CANDLES + "?" + urlencode(params))
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict) or data.get("pair") != "BTC/USD":
                    raise ValueError("Unexpected Bitstamp spot pair.")
                closes = completed_closes(
                    data.get("ohlc"), period, min(seen, origin), count
                )
                # Accept only the specifically requested window, even across a UTC boundary.
                closes = {
                    t: v for t, v in closes.items() if end - count * period <= t < end
                }
                batches.append((period, count, end, closes, seen))
            except (FeedError, ValueError) as exc:
                failed_candles.append(period)
                notes.append(
                    f"{'Daily' if period==DAY else 'Hourly'} candles unavailable."
                )
                retry = max(retry, getattr(exc, "retry", 0))
                throttled = getattr(exc, "throttled", False)
                if throttled:
                    break
        if not throttled and self._allowed(generation, stop):
            try:
                data, seen, _ = public_read(BOOK)
                book = parse_book(data, seen)
            except (FeedError, ValueError) as exc:
                notes.append("A fresh executable BTC book is unavailable.")
                retry = max(retry, getattr(exc, "retry", 0))
                throttled = getattr(exc, "throttled", False)
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM trial WHERE id=1").fetchone()
            if (
                not current["enabled"]
                or current["generation"] != generation
                or (stop is not None and stop.is_set())
            ):
                return POLL_SECONDS
            now = clock()
            if throttled:
                self._put(db, "host_backoff", int(now.timestamp()) + retry)
                notes.append("Bitstamp's rate limit is active.")
            for period in failed_candles:
                self._put(db, f"next:{period}", int(now.timestamp()) + max(300, retry))
            for period, count, end, closes, seen in batches:
                db.executemany(
                    "INSERT OR IGNORE INTO candles VALUES(?,?,?,?)",
                    [
                        (period, t, price_text(c), stamp(seen))
                        for t, c in closes.items()
                    ],
                )
                complete = len(self._closes(db, period, now, count)) == count
                self._put(
                    db,
                    f"next:{period}",
                    end + period if complete else int(now.timestamp()) + 300,
                )
                if not complete:
                    notes.append(
                        f"{'Daily' if period==DAY else 'Hourly'} candle history contains gaps."
                    )
                db.execute(
                    "DELETE FROM candles WHERE period=? AND start<?",
                    (period, end - (370 if period == DAY else 168) * period),
                )
            previous = self._get(db, "quote")
            if book and (
                not quote_fresh(book, now)
                or previous
                and book["source_at"] < previous["source_at"]
            ):
                book = None
                notes.append("BTC book is stale or earlier than the previous book.")
            signal = _signal(self._closes(db, DAY, now, 8), now)
            if book:
                self._put(db, "quote", book)
                decision = self._evaluate(db, current, signal, book, now)
            else:
                decision = "Waiting for a fresh BTC book."
            failures = 0 if book else min(current["errors"] + 1, 6)
            delay = max(retry, min(900, POLL_SECONDS * 2 ** max(0, failures - 1)))
            detail = " ".join([decision, *notes])
            db.execute(
                "UPDATE trial SET last_checked=?,status=?,errors=?,next_poll=? WHERE id=1",
                (stamp(now), detail, failures, int(now.timestamp()) + delay),
            )
        return delay

    @staticmethod
    def _account(db, name):
        return db.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()

    @staticmethod
    def _depth_left(db, account, side, book):
        size = dec(book["ask_size_btc" if side == "buy" else "bid_size_btc"]) * SATOSHIS
        used = db.execute(
            "SELECT COALESCE(SUM(sats),0) FROM fills WHERE account=? AND side=? AND quote_at=?",
            (account["name"], side, book["source_at"]),
        ).fetchone()[0]
        return max(0, int(size) - used)

    @staticmethod
    def _fill(db, account, side, sats, book, day, now):
        source = book["ask_usd" if side == "buy" else "bid_usd"]
        if (
            not 0
            < sats
            <= min(2**63 - 1, BitcoinPaper._depth_left(db, account, side, book))
        ):
            return False
        amount, fee, price = cash_terms(side, sats, source)
        if (
            amount <= 0
            or (side == "buy" and amount > account["cash"])
            or (side == "sell" and sats > account["sats"])
        ):
            return False
        inserted = db.execute(
            """INSERT OR IGNORE INTO fills(account,signal_day,side,at,sats,price,fee,amount,quote_at)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                account["name"],
                day,
                side,
                stamp(now),
                sats,
                price_text(price),
                fee,
                amount,
                book["source_at"],
            ),
        )
        if inserted.rowcount != 1:
            return False
        buying = side == "buy"
        basis_sold = 0 if buying else account["cost"] * sats // account["sats"]
        db.execute(
            """UPDATE accounts SET cash=?,sats=?,cost=?,realized=?,fills=fills+1,equity=NULL,marked=NULL WHERE name=?""",
            (
                account["cash"] + (-amount if buying else amount),
                account["sats"] + (sats if buying else -sats),
                account["cost"] + (amount if buying else -basis_sold),
                account["realized"] + (0 if buying else amount - basis_sold),
                account["name"],
            ),
        )
        return inserted.lastrowid

    @staticmethod
    def _manual_terms(order):
        if not isinstance(order, dict):
            raise ValueError("Enter a spot paper order.")
        side = order.get("side")
        size_key = "budget_cents" if side == "buy" else "quantity_btc"
        if side not in ("buy", "sell") or set(order) != {
            "client_id",
            "side",
            "limit_usd",
            size_key,
        }:
            raise ValueError("Choose a side, size and execution price limit.")
        try:
            client_id = str(UUID(order["client_id"]))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("A valid spot order ID is required.") from None

        def number(value):
            if not isinstance(value, str) or not value.isascii():
                raise ValueError(
                    "Enter a positive decimal with up to eight decimal places."
                )
            whole, dot, fraction = value.partition(".")
            if not whole.isdecimal() or (
                dot and (not fraction.isdecimal() or len(fraction) > 8)
            ):
                raise ValueError(
                    "Enter a positive decimal with up to eight decimal places."
                )
            parsed = dec(value)
            if parsed <= 0:
                raise ValueError("Order amounts and prices must be positive.")
            return price_text(parsed)

        limit = number(order["limit_usd"])
        size = order[size_key]
        if side == "buy":
            if type(size) is not int or not 1 <= size <= MAX_ENTRY_CENTS:
                raise ValueError(
                    "Enter a buy budget from $0.01 through $500, including fees."
                )
        elif size != "all":
            size = number(size)
        return client_id, {"side": side, size_key: size, "limit_usd": limit}

    @staticmethod
    def _receipt(row, client_id):
        return dict(
            id=row["id"],
            client_id=client_id,
            origin="manual",
            side=row["side"],
            btc=price_text(Decimal(row["sats"]) / SATOSHIS),
            price_usd=row["price"],
            fee_cents=row["fee"],
            cash_amount_cents=row["amount"],
            at=row["at"],
            quote_at=row["quote_at"],
        )

    def _execute_manual(self, db, terms, client_id):
        now, side = clock(), terms["side"]
        enabled = db.execute("SELECT enabled FROM trial WHERE id=1").fetchone()[0]
        book = self._get(db, "quote")
        if not self.online or not enabled or not quote_fresh(book, now):
            raise ValueError(
                "A fresh Bitstamp quote is required. Check spot collection and try again."
            )
        account = self._account(db, "strategy")
        buy = side == "buy"
        if buy:
            budget = terms["budget_cents"]
            if budget > min(account["cash"], MAX_ENTRY_CENTS - account["cost"]):
                raise ValueError(
                    "This buy exceeds available cash or the $500 open-cost limit."
                )
            sats = entry_size(budget, book["ask_usd"])
        else:
            qty = terms["quantity_btc"]
            sats = account["sats"] if qty == "all" else int(dec(qty) * SATOSHIS)
            if not 0 < sats <= account["sats"]:
                raise ValueError(
                    "The sale exceeds the BTC available in the spot account."
                )
        _, _, price = cash_terms(side, sats, book["ask_usd" if buy else "bid_usd"])
        limit = dec(terms["limit_usd"])
        if (buy and price > limit) or (not buy and price < limit):
            raise ValueError(
                "The executable price including the spot price allowance is outside your limit."
            )
        fill_id = self._fill(db, account, side, sats, book, "manual/" + client_id, now)
        if not fill_id:
            raise ValueError(
                "The requested order cannot fill at the available spot depth and size."
            )
        db.execute(
            "UPDATE trial SET auto=0,generation=generation+1,status=? WHERE id=1",
            ("Manual spot order filled. Spot automation is paused.",),
        )
        self._mark(db, book, now)
        return fill_id

    def manual_order(self, order):
        client_id, terms = self._manual_terms(order)
        request = json.dumps(terms, sort_keys=True, separators=(",", ":"))
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM manual_spot_orders WHERE client_id=?", (client_id,)
            ).fetchone()
            if prior:
                if request != prior["request"]:
                    raise ValueError(
                        "This order ID was already used with different terms."
                    )
                fill_id, error = prior["fill_id"], prior["rejection"]
            else:
                fill_id, error = None, None
                db.execute("SAVEPOINT manual_fill")
                try:
                    fill_id = self._execute_manual(db, terms, client_id)
                except ValueError as exc:
                    db.execute("ROLLBACK TO manual_fill")
                    error = str(exc)
                finally:
                    db.execute("RELEASE manual_fill")
                db.execute(
                    "INSERT INTO manual_spot_orders VALUES(?,?,?,?)",
                    (client_id, request, fill_id, error),
                )
            if fill_id:
                fill = db.execute(
                    "SELECT * FROM fills WHERE id=?", (fill_id,)
                ).fetchone()
                return self._receipt(fill, client_id)
        raise ValueError(error)

    def managed(self):
        with closing(self._db()) as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM manual_spot_orders WHERE fill_id IS NOT NULL LIMIT 1"
                ).fetchone()
            )

    def _evaluate(self, db, trial, signal, book, now):
        reason = "BTC paper trading is paused."
        target = signal["target"]
        if trial["auto"] and target:
            if not trial["started"]:
                hold = self._account(db, "hold")
                qty = entry_size(MAX_ENTRY_CENTS, book["ask_usd"])
                if not self._fill(db, hold, "buy", qty, book, signal["day"], now):
                    return "Waiting for sufficient ask size to start the strategy and buy-and-hold comparison."
                db.execute("UPDATE trial SET started=? WHERE id=1", (stamp(now),))
                if not self._account(db, "strategy")["fills"]:
                    db.execute(
                        "INSERT INTO marks VALUES(?,?,?,?,?)",
                        (
                            int(now.timestamp()) // 300,
                            1,
                            stamp(now),
                            START_CENTS,
                            START_CENTS,
                        ),
                    )
            account = self._account(db, "strategy")
            if target == "long" and not account["sats"]:
                budget = min(MAX_ENTRY_CENTS, account["cash"] // 20)
                qty = entry_size(budget, book["ask_usd"])
                reason = (
                    "Bought BTC on the long signal."
                    if self._fill(db, account, "buy", qty, book, signal["day"], now)
                    else "Waiting for executable ask size for the full BTC entry."
                )
            elif target == "cash" and account["sats"]:
                reason = (
                    "Sold the BTC position on the cash signal."
                    if self._fill(
                        db, account, "sell", account["sats"], book, signal["day"], now
                    )
                    else "Waiting for executable bid size for the full BTC exit."
                )
            else:
                reason = (
                    "Holding BTC under the long signal."
                    if target == "long"
                    else "The strategy is in cash under the daily signal."
                )
        elif trial["auto"]:
            reason = signal["reason"]
        if (
            db.execute("SELECT started FROM trial WHERE id=1").fetchone()[0]
            or self._account(db, "strategy")["fills"]
        ):
            if not self._mark(db, book, now):
                reason += (
                    " Waiting for sufficient bid size to value the full open position."
                )
        return reason

    def _mark(self, db, book, now):
        values = []
        for name in ("strategy", "hold"):
            a = self._account(db, name)
            if a["sats"] > self._depth_left(db, a, "sell", book):
                if name == "strategy":
                    equity_history.record(db, stamp(now), None)
                continue
            liquid = (
                cash_terms("sell", a["sats"], book["bid_usd"])[0] if a["sats"] else 0
            )
            equity = a["cash"] + liquid
            peak = max(a["peak"], equity)
            drawdown = max(a["drawdown"], 100 * (peak - equity) / peak)
            db.execute(
                "UPDATE accounts SET equity=?,marked=?,peak=?,drawdown=? WHERE name=?",
                (equity, book["observed_at"], peak, drawdown, name),
            )
            values.append(equity)
            if name == "strategy":
                equity_history.record(db, stamp(now), equity)
        if len(values) == 2:
            db.execute(
                "INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?)",
                (int(now.timestamp()) // 300, 0, stamp(now), *values),
            )
            return True
        return False

    def run(self, stop_event):
        while not stop_event.is_set():
            self._wake.clear()
            try:
                delay = self.poll_once(stop_event)
            except sqlite3.Error:
                delay = POLL_SECONDS
            # Respond promptly to shutdown and permission changes without polling the database in a loop.
            for _ in range(max(1, min(1800, int(delay) * 2))):
                if self._wake.is_set() or stop_event.wait(0.5):
                    break

    @staticmethod
    def _mark_fresh(account, quote):
        return not account["sats"] or bool(quote and quote["fresh"] and quote["observed_at"] == account["marked"])

    def current_equity(self, now):
        with closing(self._db()) as db:
            db.execute("BEGIN")
            account = self._account(db, "strategy")
            if not account["sats"]:
                return account["cash"]
            quote = self._get(db, "quote")
            if quote:
                enabled = db.execute("SELECT enabled FROM trial WHERE id=1").fetchone()[0]
                quote["fresh"] = self.online and enabled and quote_fresh(quote, now)
            return account["equity"] if self._mark_fresh(account, quote) else None

    def state(self, *, equity_minutes=None):
        now = clock()
        with closing(self._db()) as db, db:
            db.execute("BEGIN")
            trial = db.execute("SELECT * FROM trial WHERE id=1").fetchone()
            quote = self._get(db, "quote")
            if quote:
                quote["fresh"] = bool(
                    self.online and trial["enabled"] and quote_fresh(quote, now)
                )
            signal = _signal(self._closes(db, DAY, now, 8), now)
            hourly = [
                {"at": stamp(utc_at(t + HOUR)), "close_usd": v}
                for t, v in self._closes(db, HOUR, now, 168).items()
            ]
            # Keep five-minute resolution for the past week and a daily mark
            # for older dates. The stored rows retain their original precision.
            hist = db.execute(
                """WITH older AS (
                   SELECT MAX(bucket) AS bucket FROM marks WHERE baseline=0 AND bucket<?
                   GROUP BY bucket/288)
                   SELECT * FROM marks WHERE baseline=1 OR bucket>=? OR bucket IN (SELECT bucket FROM older)
                   ORDER BY at,baseline DESC""",
                (int(now.timestamp()) // 300 - 7 * 24 * 12,) * 2,
            ).fetchall()

            def holdings(name):
                a = self._account(db, name)
                equity = a["cash"] if not a["sats"] else a["equity"]
                gain = equity - START_CENTS if equity is not None else None
                return dict(
                    initial_cash_cents=START_CENTS,
                    cash_cents=a["cash"],
                    equity_cents=equity,
                    net_gain_cents=gain,
                    return_pct=gain * 100 / START_CENTS if gain is not None else None,
                    realized_cents=a["realized"],
                    btc=price_text(Decimal(a["sats"]) / SATOSHIS),
                    open_cost_cents=a["cost"],
                    filled_orders=a["fills"],
                    max_drawdown_pct=a["drawdown"],
                    marked_at=a["marked"],
                    mark_fresh=self._mark_fresh(a, quote),
                )

            activities = []
            for x in db.execute("SELECT * FROM fills ORDER BY id DESC LIMIT 40"):
                btc = price_text(Decimal(x["sats"]) / SATOSHIS)
                manual = x["signal_day"].startswith("manual/")
                who = (
                    "Manual BTC"
                    if manual
                    else "Buy-and-hold" if x["account"] == "hold" else "BTC strategy"
                )
                message = f"{who} paper {'buy' if x['side']=='buy' else 'sale'} of {btc} BTC at ${Decimal(x['price']):,.2f}."
                activities.append(
                    dict(
                        id="bitcoin-fill-" + str(x["id"]),
                        at=x["at"],
                        kind="fill",
                        message=message,
                        account=x["account"],
                        side=x["side"],
                        btc=btc,
                        price_usd=x["price"],
                        fee_cents=x["fee"],
                        cash_amount_cents=x["amount"],
                        origin=(
                            "manual"
                            if manual
                            else "benchmark" if x["account"] == "hold" else "auto"
                        ),
                        signal_day=None if manual else x["signal_day"],
                        quote_at=x["quote_at"],
                    )
                )
            strategy = holdings("strategy")
            chart = {}
            if equity_minutes:
                current = strategy["equity_cents"] if strategy["mark_fresh"] else None
                chart["portfolio_history"] = equity_history.history(
                    db, "spot", equity_minutes, now, current, gap_seconds=420)
            return dict(
                source="Bitstamp",
                product="BTC/USD",
                source_url="https://www.bitstamp.net/api/",
                fee_bps=60,
                adverse_execution_bps=5,
                max_entry_cents=MAX_ENTRY_CENTS,
                manual_trades=db.execute(
                    "SELECT count(*) FROM manual_spot_orders WHERE fill_id IS NOT NULL"
                ).fetchone()[0],
                enabled=bool(trial["enabled"]),
                auto_enabled=bool(trial["auto"]),
                online=self.online,
                status=(
                    trial["status"] if self.online else "BTC public reads are offline."
                ),
                last_checked_at=trial["last_checked"],
                started_at=trial["started"],
                quote=quote,
                signal=signal,
                portfolio=strategy,
                **chart,
                benchmark=holdings("hold"),
                hourly=hourly,
                history=[
                    dict(
                        at=x["at"],
                        equity_cents=x["equity"],
                        hold_equity_cents=x["hold"],
                    )
                    for x in hist
                ],
                activity=activities,
            )
