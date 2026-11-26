"""Cent-denominated, long-only paper account. No order-routing or network dependency.

The paper policy buys at most once per market and holds to a venue result. Amounts
are integer cents. It reserves the paper order debit plus an execution allowance and
releases any unused allowance after a fill. Whole contract/whole cent books and a
known quadratic taker rate are the supported execution model.

Fee references: https://kalshi.com/fee-schedule and
https://docs.kalshi.com/getting_started/fee_rounding . The paper balance precision
is one cent. Per-fill model fees are first rounded upward to six decimal dollars
and rounding credit is accumulated across the entire order.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from urllib.parse import urlparse


MAX_QTY = 100
POSITION_LIMIT = 10_000
EVENT_LIMIT = 20_000
PORTFOLIO_LIMIT = 50_000
DAILY_LOSS_LIMIT = 15_000
DRAWDOWN_LIMIT = 40_000
QUOTE_TTL_SECONDS = 120
ALLOWANCE_CENTS = 1
MICRO_PER_CENT = 10_000
RECONCILIATION_FAILURE = "Paper ledger reconciliation failed."


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _key(*values):
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


def _dt(value):
    if not isinstance(value, str):
        raise ValueError("A timezone-aware timestamp is required.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")
    return result.astimezone(timezone.utc)


def _utc(value):
    return _dt(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _wall():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid number.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal number.") from exc
    if not number.is_finite():
        raise ValueError("A finite number is required.")
    return number


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _age(packet, now):
    try:
        return (now - _dt(packet.get("observed_at"))).total_seconds()
    except (ValueError, OverflowError):
        return None


def _levels(packet):
    """Normalize explicit executable asks. Reject a broken snapshot in its entirety."""
    books = packet.get("levels")
    if not isinstance(books, dict):
        raise ValueError("Executable YES and NO depth is missing.")
    sides = {}
    for side in ("yes", "no"):
        rows = books.get(side)
        if not isinstance(rows, list) or not rows:
            raise ValueError("Executable depth on both sides is required.")
        sizes = {}
        for level in rows:
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                raise ValueError("A book level has no executable price and size.")
            p, q = map(_number, level)
            if (
                not 0 < p < 100
                or not 0 < q <= 2**53
                or p != p.to_integral_value()
                or q != q.to_integral_value()
            ):
                raise ValueError(
                    "This paper account requires whole contract sizes and whole cent ticks."
                )
            if int(p) in sizes:
                raise ValueError("The snapshot repeats a price level.")
            sizes[int(p)] = int(q)
        sides[side] = sorted(sizes.items())
    if sides["yes"][0][0] + sides["no"][0][0] <= 100:
        raise ValueError("The two sides of the book are locked or crossed.")
    return sides


def _rate(packet, now):
    expires = packet.get("fee_valid_until")
    if expires is not None and now >= _dt(expires):
        raise ValueError(
            "The series fee schedule expired. A new fee quote is required."
        )
    try:
        rate = _number(packet.get("fee_rate"))
    except ValueError as exc:
        raise ValueError("The series taker fee is missing or invalid.") from exc
    if not 0 <= rate <= 7 or packet.get("fee_model", "quadratic") not in (
        "quadratic",
        "quadratic_taker",
        "quadratic_with_maker_fees",
    ):
        raise ValueError("The series fee model is unsupported.")
    return rate


def _book_age_ok(packet, now):
    as_of = _dt(packet.get("as_of"))
    observed = _dt(packet.get("observed_at"))
    if as_of > now or observed > as_of:
        raise ValueError("The quote or research cutoff is ahead of its decision clock.")
    if (now - observed).total_seconds() > QUOTE_TTL_SECONDS:
        raise ValueError(
            f"The book is older than {QUOTE_TTL_SECONDS} seconds. A fresh snapshot is required."
        )


def _check_evidence(packet, as_of):
    evidence = packet.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Point-in-time source evidence is missing.")
    for item in evidence:
        if not isinstance(item, dict) or not item.get("url"):
            raise ValueError("An evidence item is missing its source.")
        try:
            available = _dt(item.get("available_at"))
            retrieved = (
                _dt(item["retrieved_at"]) if item.get("retrieved_at") else available
            )
            if item.get("published_at"):
                pub = _dt(item["published_at"])
            elif item.get("context_only") is True or item.get("kind") == "observation":
                # First-hand receipt bounds contextual information even when
                # the publisher omits publication time. Preserve the null.
                pub = _dt(item["issued_at"]) if item.get("issued_at") else available
            else:
                raise ValueError("Publication time is absent.")
        except ValueError as exc:
            raise ValueError("An evidence timestamp is unknown.") from exc
        if not pub <= available <= retrieved <= as_of:
            raise ValueError(
                "Evidence was unavailable at the decision cutoff or its timestamps disagree."
            )


def _inputs(packet, now):
    if packet.get("missing"):
        raise ValueError("Missing input: " + ", ".join(map(str, packet["missing"])))
    if packet.get("tradable") is not True:
        raise ValueError("The contract is unavailable for new paper entries.")
    if not all(
        packet.get(k)
        for k in ("event_id", "station", "event_date", "rules", "rule_version")
    ):
        raise ValueError(
            "The contract specification or settlement rule version is incomplete."
        )
    if now >= _dt(packet.get("close_at")):
        raise ValueError("The entry window has closed. Waiting for the venue result.")
    _book_age_ok(packet, now)
    books = _levels(packet)
    rate = _rate(packet, now)
    p, lower, upper = _forecast(packet, now)
    return books, rate, p, lower, upper


def _forecast(packet, now):
    as_of = _dt(packet.get("as_of"))
    f = packet.get("forecast")
    if (
        not isinstance(f, dict)
        or not f.get("model")
        or type(f.get("samples")) is not int
    ):
        raise ValueError(
            "A calibrated station forecast and its sample count are required."
        )
    minimum = f.get("minimum_samples", 30)
    if type(minimum) is not int or f["samples"] < max(30, minimum):
        raise ValueError(
            "The station and lead-time window has insufficient calibrated days."
        )
    try:
        p, lower, upper, baseline = (
            _number(f.get(k)) for k in ("probability", "lower", "upper", "baseline")
        )
    except ValueError as exc:
        raise ValueError(
            "A calibrated probability, interval or climatology baseline is missing."
        ) from exc
    if not (0 <= lower <= p <= upper <= 1 and 0 <= baseline <= 1):
        raise ValueError("The forecast or its uncertainty bounds are inconsistent.")
    issued = _dt(f.get("issued_at"))
    available = _dt(f["available_at"]) if f.get("available_at") else issued
    retrieved = _dt(f["retrieved_at"]) if f.get("retrieved_at") else available
    estimated = _dt(f["estimated_at"]) if f.get("estimated_at") else retrieved
    if not issued <= available <= retrieved <= estimated <= as_of:
        raise ValueError("The forecast was unavailable at the decision cutoff.")
    max_hours = {"NWS issued daytime high": 18, "NOAA NBM NBS 3-hour maximum": 42}.get(
        f.get("source"), 48
    )
    if (now - issued).total_seconds() > max_hours * 3600:
        raise ValueError("The issued forecast has expired for this decision.")
    _check_evidence(packet, as_of)
    return p, lower, upper


def _micro_fee(price, qty, rate):
    # Six decimal dollars per match. Price and the returned premium use cents.
    return _ceil(rate * price * (100 - price) * qty * MICRO_PER_CENT / 100)


def _cent_fee(micro):
    return (micro + MICRO_PER_CENT - 1) // MICRO_PER_CENT


def _portion(levels, start, end):
    """Return the actual price ladder consumed between cumulative quantities."""
    offset = 0
    for price, qty in levels:
        count = max(0, min(end, offset + qty) - max(start, offset))
        if count:
            yield price, count
        offset += qty
        if offset >= end:
            break


def _price(books, rate, probability, lower, upper, capacity):
    # Consider each executable prefix. Marginal depth can turn a profitable
    # smaller ticket into a loss. Compare total dollars after all costs.
    best = None
    passes = []
    midpoint = (books["yes"][0][0] + 100 - books["no"][0][0]) / 200
    for side, bound, point in (
        ("YES", lower, probability),
        ("NO", 1 - upper, 1 - probability),
    ):
        levels = books[side.lower()]
        available = sum(q for _, q in levels)
        qty = premium = fee_micro = 0
        last = None
        for ask, depth in levels:
            per_unit = _micro_fee(ask, 1, rate)
            for _ in range(min(depth, MAX_QTY - qty)):
                next_qty = qty + 1
                next_premium = premium + ask
                next_micro = fee_micro + per_unit
                fee = _cent_fee(next_micro)
                allowance = ALLOWANCE_CENTS * next_qty
                cost = next_premium + fee + allowance
                if cost > capacity:
                    break
                qty, premium, fee_micro = next_qty, next_premium, next_micro
                net = int(bound * 100 * qty) - cost
                last = {
                    "side": side,
                    "qty": qty,
                    "available_qty": available,
                    "avg_ask_cents": round(premium / qty, 3),
                    "limit_ask_cents": ask,
                    "premium_cents": premium,
                    "fee_cents": fee,
                    "allowance_cents": allowance,
                    "cost_cents": cost,
                    "net_cents": net,
                    "model_net_cents": int(point * 100 * qty) - cost,
                    "conservative_probability": float(bound),
                    "market_baseline": midpoint,
                    "break_even_probability": cost / (100 * qty),
                }
                if net > 0 and (
                    best is None
                    or (net, -cost) > (best["net_cents"], -best["cost_cents"])
                ):
                    best = last
            if qty >= MAX_QTY or (
                premium
                + ask
                + _cent_fee(fee_micro + per_unit)
                + (qty + 1) * ALLOWANCE_CENTS
                > capacity
            ):
                break
        if last is not None:
            passes.append(last)
    chosen = best or max(
        passes,
        key=lambda x: (Decimal(x["net_cents"]) / x["qty"], x["net_cents"]),
        default=None,
    )
    if chosen:
        bound = lower if chosen["side"] == "YES" else 1 - upper
        qty = chosen["qty"]
        chosen["worst_acceptable_ask_cents"] = next(
            (
                p
                for p in range(99, 0, -1)
                if int(bound * 100 * qty)
                > (p + ALLOWANCE_CENTS) * qty + _cent_fee(_micro_fee(p, 1, rate) * qty)
            ),
            None,
        )
    return chosen


def _empty(decision, reason):
    return {
        "decision": decision,
        "reason": reason,
        "side": None,
        "qty": 0,
        "available_qty": 0,
        "avg_ask_cents": None,
        "net_cents": None,
        "fee_cents": None,
        "allowance_cents": None,
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS account(
    id INTEGER PRIMARY KEY CHECK(id=1), initial_cash INTEGER NOT NULL,
    source TEXT, clock TEXT, manual_pause INTEGER NOT NULL DEFAULT 0,
    halt TEXT, day TEXT, day_start INTEGER NOT NULL, peak INTEGER NOT NULL, equity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS forecasts(
    id TEXT PRIMARY KEY, market_id TEXT NOT NULL, first_seen TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
    id TEXT PRIMARY KEY, market_id TEXT NOT NULL, first_seen TEXT NOT NULL,
    forecast_id TEXT REFERENCES forecasts(id), packet TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_by_market ON snapshots(market_id,first_seen);
CREATE TABLE IF NOT EXISTS markets(
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, snapshot TEXT NOT NULL REFERENCES snapshots(id),
    as_of TEXT NOT NULL, quote_at TEXT, conflicted INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS markets_event ON markets(event_id);
CREATE TABLE IF NOT EXISTS intents(
    id TEXT PRIMARY KEY, market_id TEXT NOT NULL REFERENCES markets(id), event_id TEXT NOT NULL,
    snapshot TEXT NOT NULL REFERENCES snapshots(id), side TEXT NOT NULL, qty INTEGER NOT NULL,
    levels TEXT NOT NULL, rate TEXT NOT NULL, created TEXT NOT NULL, terms TEXT NOT NULL,
    status TEXT NOT NULL, reserved INTEGER NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
    premium INTEGER NOT NULL DEFAULT 0, fee INTEGER NOT NULL DEFAULT 0, micro INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_entry ON intents(market_id)
    WHERE status != 'canceled' OR filled > 0;
CREATE TABLE IF NOT EXISTS fills(
    id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id), at TEXT NOT NULL,
    cumulative_qty INTEGER NOT NULL, qty INTEGER NOT NULL, premium INTEGER NOT NULL, fee INTEGER NOT NULL,
    UNIQUE(intent_id,cumulative_qty)
);
CREATE TABLE IF NOT EXISTS settlements(
    market_id TEXT PRIMARY KEY REFERENCES markets(id), result TEXT NOT NULL, at TEXT NOT NULL,
    source TEXT NOT NULL, status TEXT NOT NULL, payout INTEGER NOT NULL DEFAULT 0, realized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assessments(
    market_id TEXT PRIMARY KEY REFERENCES settlements(market_id), event_date TEXT,
    snapshot TEXT REFERENCES snapshots(id), decision_at TEXT, outcome INTEGER NOT NULL,
    model_p REAL, market_p REAL, climatology_p REAL
);
CREATE TABLE IF NOT EXISTS score_summary(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
    id TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, market_id TEXT, message TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_time ON activity(at);
"""


class Ledger:
    def __init__(self, db_path, initial_cash_cents=1_000_000):
        if type(initial_cash_cents) is not int or not 0 <= initial_cash_cents < 2**53:
            raise ValueError(
                "Opening paper cash must be a nonnegative whole number of cents."
            )
        if str(db_path) != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            db_path, timeout=15, isolation_level=None, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO account(id, initial_cash, day_start, peak, equity) VALUES(1,?,?,?,?)",
            (initial_cash_cents,) * 4,
        )
        with self._transaction():
            self._reconcile_balances()
            for r in list(
                self.db.execute("""SELECT s.* FROM settlements s LEFT JOIN assessments a
                                         ON a.market_id=s.market_id WHERE a.market_id IS NULL
                                         AND s.status='applied' AND s.result IN ('yes','no')""")
            ):
                self._grade(r["market_id"], r["result"], r["at"])
            if not self._one("SELECT 1 FROM score_summary WHERE id=1"):
                self._refresh_scores()

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def _transaction(self, write=True):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def _account(self):
        return self._one("SELECT * FROM account WHERE id=1")

    def _entry(self, market_id):
        return self._one(
            "SELECT * FROM intents WHERE market_id=? AND (status!='canceled' OR filled>0)",
            market_id,
        )

    def _event(self, key, at, kind, market_id, message, **fields):
        self.db.execute(
            "INSERT OR IGNORE INTO activity VALUES(?,?,?,?,?,?)",
            (_key(kind, key), at, kind, market_id, message, _json(fields)),
        )

    def _clock(self, at):
        account = self._account()
        if account["clock"] and at < account["clock"]:
            return account["clock"]
        day = at[:10]
        self.db.execute(
            "UPDATE account SET clock=?,day_start=CASE WHEN day IS NULL OR day!=? THEN equity ELSE day_start END,day=? WHERE id=1",
            (at, day, day),
        )
        return at

    def _source(self, packets):
        sources = {p.get("book_source") for p in packets}
        if not sources:
            return
        if len(sources) != 1 or not sources <= {"fixture", "kalshi"}:
            raise ValueError(
                "One recognized book source is required per paper account."
            )
        source = sources.pop()
        existing = self._account()["source"]
        if existing and source != existing:
            raise ValueError(
                "Synthetic and licensed paper accounts must use separate databases."
            )
        self.db.execute("UPDATE account SET source=? WHERE id=1", (source,))

    def _store(self, packet, at):
        market_id = packet.get("id")
        if (
            not isinstance(market_id, str)
            or not market_id
            or not isinstance(packet.get("event_id"), str)
        ):
            raise ValueError("A market ID and event ID are required.")
        snapshot = _key(packet)
        forecast = packet.get("forecast")
        forecast_id = _key(market_id, forecast) if forecast is not None else None
        if forecast_id:
            self.db.execute(
                "INSERT OR IGNORE INTO forecasts VALUES(?,?,?,?)",
                (forecast_id, market_id, at, _json(forecast)),
            )
        self.db.execute(
            "INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)",
            (snapshot, market_id, at, forecast_id, _json(packet)),
        )
        try:
            as_of = min(at, _utc(packet.get("as_of")))
        except ValueError:
            as_of = at
        try:
            observed = _utc(packet.get("observed_at"))
            if observed > as_of:
                observed = None
        except ValueError:
            observed = None
        last = self._market(market_id)
        if last:
            if as_of < last["as_of"] or (
                observed and last["quote_at"] and observed < last["quote_at"]
            ):
                return False
            if snapshot == last["snapshot"]:
                return True
            previous = json.loads(last["packet"])
            changed_identity = any(
                previous.get(k) and packet.get(k) and previous[k] != packet[k]
                for k in ("event_id", "event_date", "station")
            )
            changed_book = (
                observed
                and observed == last["quote_at"]
                and packet.get("levels") != previous.get("levels")
            )
            if (
                as_of == last["as_of"]
                or changed_identity
                or changed_book
                or (
                    last["conflicted"]
                    and last["quote_at"]
                    and (not observed or observed <= last["quote_at"])
                )
            ):
                self.db.execute(
                    "UPDATE markets SET conflicted=1 WHERE id=?", (market_id,)
                )
                return True
        self.db.execute(
            """
            INSERT INTO markets(id,event_id,snapshot,as_of,quote_at) VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET event_id=excluded.event_id,snapshot=excluded.snapshot,
            as_of=excluded.as_of,quote_at=excluded.quote_at,conflicted=0
        """,
            (market_id, packet["event_id"], snapshot, as_of, observed),
        )
        return True

    def _market(self, market_id):
        return self._one(
            """SELECT m.*,s.packet,s.forecast_id FROM markets m
                         JOIN snapshots s ON s.id=m.snapshot WHERE m.id=?""",
            market_id,
        )

    def has_market(self, market_id):
        """An indexed lookup for results first seen before this worker's catalog."""
        with self._transaction(write=False):
            return self._one("SELECT 1 FROM markets WHERE id=?", market_id) is not None

    def _verdict(self, row, value, at):
        prior = json.loads(row["verdict"])
        value = {**value, "decision_as_of": value.get("decision_as_of", at)}
        same = {k: v for k, v in value.items() if k != "decision_as_of"}
        if same == {k: v for k, v in prior.items() if k != "decision_as_of"}:
            return
        self.db.execute(
            "UPDATE markets SET verdict=? WHERE id=?", (_json(value), row["id"])
        )
        self._event(
            (row["snapshot"], same),
            at,
            "decision",
            row["id"],
            value["decision"] + ". " + value["reason"],
            **value,
        )

    def _funds(self):
        amounts = self._one("""
            SELECT coalesce(sum(i.premium+i.fee),0) AS spent,coalesce(sum(i.fee),0) AS fees,
            coalesce(sum(i.reserved),0) AS reserved,
            coalesce(sum(CASE WHEN s.status='applied' THEN 0 ELSE i.premium+i.fee END),0) AS open_cost,
            count(CASE WHEN i.filled>0 THEN 1 END) AS filled_orders,
            count(CASE WHEN i.filled>0 AND s.status='applied' THEN 1 END) AS settled_orders
            FROM intents i LEFT JOIN settlements s ON s.market_id=i.market_id
        """)
        results = self._one(
            "SELECT coalesce(sum(payout),0) AS payout,coalesce(sum(realized),0) AS realized FROM settlements"
        )
        cash = self._account()["initial_cash"] - amounts["spent"] + results["payout"]
        return dict(
            cash_cents=cash,
            reserved_cents=amounts["reserved"],
            available_cash_cents=cash - amounts["reserved"],
            fees_cents=amounts["fees"],
            open_cost_cents=amounts["open_cost"],
            realized_cents=results["realized"],
            filled_orders=amounts["filled_orders"],
            settled_orders=amounts["settled_orders"],
        )

    def _reconcile_balances(self):
        """Before resuming, compare position totals to their individual cash receipts."""
        bad_fill = self._one("""
            SELECT i.id AS ref FROM intents i LEFT JOIN fills f ON f.intent_id=i.id GROUP BY i.id
            HAVING i.filled!=coalesce(sum(f.qty),0) OR i.filled!=coalesce(max(f.cumulative_qty),0)
            OR i.premium!=coalesce(sum(f.premium),0) OR i.fee!=coalesce(sum(f.fee),0)
            OR i.filled<0 OR i.filled>i.qty OR i.reserved<0
            OR (i.status IN ('filled','canceled') AND i.reserved!=0)
            OR (i.status IN ('reserved','partial','unknown') AND (i.filled>=i.qty OR i.reserved=0)) LIMIT 1
        """)
        bad_settlement = self._one("""
            SELECT s.market_id AS ref FROM settlements s LEFT JOIN intents i ON i.market_id=s.market_id
            WHERE s.status='applied' GROUP BY s.market_id
            HAVING s.payout!=coalesce(sum(CASE WHEN s.result='void' THEN i.premium
              WHEN upper(s.result)=i.side THEN i.filled*100 ELSE 0 END),0)
            OR s.realized!=s.payout-coalesce(sum(i.premium+i.fee),0) OR coalesce(sum(i.reserved),0)!=0 LIMIT 1
        """)
        fault = bad_fill or bad_settlement
        if not fault and self._funds()["available_cash_cents"] >= 0:
            return True
        ref = fault["ref"] if fault else "available cash"
        reason = (
            RECONCILIATION_FAILURE + " Review the recorded receipts for " + ref + "."
        )
        self.db.execute("UPDATE account SET halt=? WHERE id=1", (reason,))
        self._event(
            ref, self._account()["clock"] or _wall(), "ledger_mismatch", None, reason
        )
        return False

    def _capacity(self, event_id):
        funds = self._funds()
        event = self._one(
            """
            SELECT coalesce(sum(i.premium+i.fee+i.reserved),0) AS exposure FROM intents i
            LEFT JOIN settlements s ON s.market_id=i.market_id
            WHERE i.event_id=? AND coalesce(s.status,'')!='applied'
        """,
            event_id,
        )["exposure"]
        return max(
            0,
            min(
                POSITION_LIMIT,
                EVENT_LIMIT - event,
                PORTFOLIO_LIMIT - funds["open_cost_cents"] - funds["reserved_cents"],
                funds["available_cash_cents"],
            ),
        )

    def _mark(self, packet, intent, now, conflicted):
        """Bid depth comes from the opposite asks. Give unsold contracts zero exit value."""
        qty = intent["filled"]
        age = _age(packet, now)
        try:
            if conflicted:
                raise ValueError("Conflicting snapshots.")
            _book_age_ok(packet, now)
            if now >= _dt(packet.get("close_at")):
                raise ValueError("Closed market.")
            if packet.get("book_executable", packet.get("tradable")) is not True:
                raise ValueError("Unavailable market.")
            opposite = "no" if intent["side"] == "YES" else "yes"
            bids = [(100 - ask, depth) for ask, depth in _levels(packet)[opposite]]
            rate = _rate(packet, now)
            matches = list(_portion(bids, 0, qty))
            matched = sum(q for _, q in matches)
            premium = sum(p * q for p, q in matches)
            fees = _cent_fee(sum(_micro_fee(p, q, rate) for p, q in matches))
            mark = max(0, premium - fees - ALLOWANCE_CENTS * matched)
            status = "executable" if matched == qty else "thin"
        except (ValueError, OverflowError):
            mark, matched, status = 0, 0, "stale"
        return {
            "mark_value_cents": mark,
            "marked_qty": matched,
            "mark_status": status,
            "quote_age_seconds": int(age) if age is not None else None,
        }

    def _portfolio(self, now):
        funds = self._funds()
        pos = []
        orders = []
        events = {}
        for r in self.db.execute("""
            SELECT i.*,s.packet,m.conflicted FROM intents i JOIN markets m ON m.id=i.market_id
            JOIN snapshots s ON s.id=m.snapshot LEFT JOIN settlements t ON t.market_id=m.id
            WHERE coalesce(t.status,'')!='applied' AND (i.reserved>0 OR i.filled>0)
            ORDER BY i.created,i.market_id
        """):
            record = dict(
                market_id=r["market_id"],
                event_id=r["event_id"],
                side=r["side"],
                intent_id=r["id"],
                status=r["status"],
                qty=r["filled"],
                reserved_cents=r["reserved"],
                outstanding_qty=r["qty"] - r["filled"] if r["reserved"] else 0,
                premium_cents=r["premium"],
                fee_cents=r["fee"],
                cost_cents=r["premium"] + r["fee"],
            )
            events.setdefault(r["event_id"], []).append(record)
            if r["reserved"]:
                orders.append(
                    {
                        **record,
                        "requested_qty": r["qty"],
                        "cancel_requested": bool(r["cancel_requested"]),
                    }
                )
            if r["filled"]:
                packet = json.loads(r["packet"])
                mark = self._mark(packet, r, now, r["conflicted"])
                pos.append(
                    {
                        **record,
                        **mark,
                        "id": r["market_id"],
                        "label": packet.get("label"),
                        "title": packet.get("title"),
                        "avg_ask_cents": round(r["premium"] / r["filled"], 3),
                        "open_pnl_cents": mark["mark_value_cents"]
                        - record["cost_cents"],
                        "stale": mark["mark_status"] == "stale",
                    }
                )
        # Known mutually exclusive bins, the residual range and an all-void state.
        # The admission limits additionally charge the full gross cost, so incorrect
        # offsets or an exceptional payout cannot expand buying power.
        event_risk = []
        for eid, legs in events.items():
            outcomes = {}
            for winner in [leg["market_id"] for leg in legs] + [None]:
                loss = 0
                for leg in legs:
                    wins = (leg["market_id"] == winner) == (leg["side"] == "YES")
                    loss += leg["cost_cents"] - (100 * leg["qty"] if wins else 0)
                    loss += max(
                        0,
                        leg["reserved_cents"]
                        - (100 * leg["outstanding_qty"] if wins else 0),
                    )
                outcomes[winner or "other_range"] = max(0, loss)
            # Charge every reserved cent in an exceptional state until the rest of
            # the order is canceled. A filled standard void returns only premium.
            outcomes["void"] = sum(
                leg["fee_cents"] + leg["reserved_cents"] for leg in legs
            )
            gross = sum(leg["cost_cents"] + leg["reserved_cents"] for leg in legs)
            event_risk.append(
                dict(
                    event_id=eid,
                    charged_exposure_cents=gross,
                    worst_case_cents=max(outcomes.values()),
                    outcome_loss_cents=outcomes,
                )
            )
        account = self._account()
        open_value = sum(p["mark_value_cents"] for p in pos)
        equity = funds["cash_cents"] + open_value
        return {
            **funds,
            "positions": pos,
            "orders": orders,
            "events": event_risk,
            "initial_cash_cents": account["initial_cash"],
            "open_value_cents": open_value,
            "equity_cents": equity,
            "net_pnl_cents": equity - account["initial_cash"],
            "open_pnl_cents": open_value - funds["open_cost_cents"],
            "max_loss_cents": funds["open_cost_cents"] + funds["reserved_cents"],
            "daily_loss_cents": max(0, account["day_start"] - equity),
            "drawdown_cents": max(0, account["peak"] - equity),
            "peak_equity_cents": max(equity, account["peak"]),
            "stale_marks": sum(p["stale"] for p in pos),
            "mark_as_of": now.isoformat(),
            "limits": {
                "position_cents": POSITION_LIMIT,
                "event_cents": EVENT_LIMIT,
                "portfolio_cents": PORTFOLIO_LIMIT,
                "daily_loss_cents": DAILY_LOSS_LIMIT,
                "drawdown_cents": DRAWDOWN_LIMIT,
            },
        }

    def _cancel(self, intent, at, reason):
        if not intent["reserved"]:
            return
        self.db.execute(
            "UPDATE intents SET reserved=0,status='canceled',cancel_requested=0 WHERE id=?",
            (intent["id"],),
        )
        self._event(
            intent["id"],
            at,
            "cancel",
            intent["market_id"],
            reason,
            intent_id=intent["id"],
            released_cents=intent["reserved"],
        )

    def _cancel_resting(self, at):
        for intent in list(self.db.execute("SELECT * FROM intents WHERE reserved>0")):
            if intent["status"] == "unknown":
                self.db.execute(
                    "UPDATE intents SET cancel_requested=1 WHERE id=?", (intent["id"],)
                )
                self._event(
                    intent["id"],
                    at,
                    "cancel_requested",
                    intent["market_id"],
                    "Cancellation requested. The unknown paper submission remains reserved until reconciled.",
                    intent_id=intent["id"],
                )
            else:
                self._cancel(
                    intent,
                    at,
                    "The paper adapter acknowledged cancellation of the remaining quantity.",
                )

    @staticmethod
    def _loss_reason(portfolio):
        if portfolio["drawdown_cents"] >= DRAWDOWN_LIMIT:
            return "Peak-to-trough marked drawdown reached the $400 paper limit."
        if portfolio["daily_loss_cents"] >= DAILY_LOSS_LIMIT:
            return "Marked loss reached the $150 UTC-day paper limit."
        return None

    def _risk(self, at):
        account = self._account()
        p = self._portfolio(_dt(at))
        self.db.execute(
            "UPDATE account SET peak=max(peak,?),equity=? WHERE id=1",
            (p["equity_cents"], p["equity_cents"]),
        )
        reason = self._loss_reason(p)
        if reason and not account["halt"]:
            self.db.execute("UPDATE account SET halt=? WHERE id=1", (reason,))
            self._event(at, at, "risk_pause", None, reason)
            self._cancel_resting(at)
        return self._account()

    def _active_reason(self, row, at):
        st = self._one("SELECT * FROM settlements WHERE market_id=?", row["id"])
        if st:
            if st["status"] == "applied":
                return "Settled", "Venue result: " + st["result"].upper() + "."
            return (
                "Held",
                "Venue result received. Cash posting awaits ledger reconciliation.",
            )
        entry = self._entry(row["id"])
        if not entry:
            return None
        if entry["status"] == "unknown":
            return (
                "Held",
                "Submission status is unknown. Remaining cash is reserved pending reconciliation.",
            )
        if entry["status"] == "canceled":
            return (
                "Held",
                "The unfilled remainder was canceled. Filled contracts remain open.",
            )
        if row["conflicted"]:
            return (
                "Held",
                "Conflicting snapshots share a timestamp. The existing entry remains under review.",
            )
        packet = json.loads(row["packet"])
        try:
            if _dt(at) >= _dt(packet.get("close_at")):
                return (
                    "Held",
                    "Entry window closed. Holding filled contracts until the venue publishes a result.",
                )
            _book_age_ok(packet, _dt(at))
        except ValueError:
            return (
                "Held",
                "The book is stale or unavailable. The existing paper entry remains open.",
            )
        acc = self._account()
        if acc["manual_pause"] or acc["halt"]:
            return (
                "Held",
                "New entries are paused. Filled contracts remain open to settlement.",
            )
        if entry["snapshot"] == row["snapshot"]:
            if entry["status"] == "partial":
                return (
                    "Held",
                    "The paper entry partly filled. Remaining cash is reserved.",
                )
            return "Paper buy", json.loads(entry["terms"])["reason"]
        return (
            "Held",
            "An entry already exists for this contract. The strategy holds through venue settlement.",
        )

    def _active_verdict(self, row, at):
        active = self._active_reason(row, at)
        if not active:
            return None
        intent = self._entry(row["id"])
        decision, reason = active
        base = json.loads(intent["terms"]) if intent else _empty(decision, reason)
        return {
            **base,
            "decision": decision,
            "reason": reason,
            "intent_id": intent["id"] if intent else None,
        }

    def _evaluate(self, row, at):
        active = self._active_verdict(row, at)
        if active:
            return active, None
        packet = json.loads(row["packet"])
        if row["conflicted"]:
            return _empty(
                "Watch",
                "Conflicting snapshots share a timestamp. Waiting for a new book.",
            ), None
        if self._account()["manual_pause"] or self._account()["halt"]:
            return _empty(
                "Watch",
                self._account()["halt"] or "New paper entries are paused by the owner.",
            ), None
        try:
            books, rate, p, lower, upper = _inputs(packet, _dt(at))
        except (ValueError, OverflowError) as exc:
            return _empty("Watch", str(exc)), None
        terms = _price(books, rate, p, lower, upper, self._capacity(row["event_id"]))
        if terms is None:
            return _empty(
                "Pass",
                "Free cash or an event or portfolio risk limit prevents a new paper entry.",
            ), None
        if terms["net_cents"] <= 0:
            return {
                **terms,
                "decision": "Pass",
                "reason": "Book depth, fees and the execution allowance use the conservative edge.",
            }, None
        choice = terms["side"]
        terms["receipt_to_decision_ms"] = int(
            (_dt(at) - _dt(packet["observed_at"])).total_seconds() * 1000
        )
        ref = "paper-" + _key(row["id"], row["snapshot"], choice, terms["qty"])[:32]
        if self._one("SELECT id FROM intents WHERE id=?", ref):
            return _empty(
                "Watch",
                "This paper reference was canceled. Waiting for a fresh snapshot.",
            ), None
        terms.update(
            decision="Paper buy",
            reason=f"The conservative {choice} payout covers depth, fees and the execution allowance.",
            intent_id=ref,
            decision_as_of=at,
        )
        return terms, (books[choice.lower()], rate)

    def _reserve(self, row, terms, book, at):
        levels, rate = book
        qty = terms["qty"]
        self.db.execute(
            """
            INSERT INTO intents(id,market_id,event_id,snapshot,side,qty,levels,rate,created,terms,status,reserved)
            VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?)
        """,
            (
                terms["intent_id"],
                row["id"],
                row["event_id"],
                row["snapshot"],
                terms["side"],
                qty,
                _json(list(_portion(levels, 0, qty))),
                str(rate),
                at,
                _json(terms),
                terms["cost_cents"],
            ),
        )
        self._event(
            terms["intent_id"],
            at,
            "intent",
            row["id"],
            f"Reserved cash for {qty} {terms['side']} contracts.",
            intent_id=terms["intent_id"],
            side=terms["side"],
            qty=qty,
            reserved_cents=terms["cost_cents"],
        )

    def ingest(self, packets, now, *, auto_fill=True):
        """Process all packets and their paper decisions as a single write transaction.

        With auto_fill=False, reservations are durable for a cumulative adapter report.
        A successful return contains counts. state() returns the complete client view.
        """
        at = _utc(now)
        with self._transaction():
            self._source(packets)
            acc = self._account()
            if acc["clock"] and at < acc["clock"]:
                for packet in packets:
                    if not self._one(
                        "SELECT id FROM snapshots WHERE id=?", _key(packet)
                    ):
                        self._event(
                            (_key(packet), at),
                            acc["clock"],
                            "clock_skew",
                            packet.get("id"),
                            "A newly received batch predates the worker clock. No new entry was evaluated.",
                            received_clock=at,
                        )
                return {"evaluated": 0, "filled": 0, "ignored": len(packets)}
            self._clock(at)
            market_ids = set()
            for packet in packets:
                if self._store(packet, at):
                    market_ids.add(packet["id"])
            self._risk(at)
            candidates = []
            for market_id in sorted(market_ids):
                row = self._market(market_id)
                terms, book = self._evaluate(row, at)
                if book is None:
                    self._verdict(row, terms, at)
                else:
                    candidates.append((-terms["net_cents"], market_id))
            filled = 0
            for _, market_id in sorted(candidates):
                row = self._market(market_id)
                terms, book = self._evaluate(
                    row, at
                )  # Earlier candidates may have consumed risk.
                self._verdict(row, terms, at)
                if book is not None:
                    self._reserve(row, terms, book, at)
                    if auto_fill:
                        self._fill(terms["intent_id"], terms["qty"], at)
                        filled += 1
                    self._risk(at)
            return {
                "evaluated": len(market_ids),
                "filled": filled,
                "ignored": len(packets) - len(market_ids),
            }

    def _fill(self, intent_id, cumulative_qty, at):
        intent = self._one("SELECT * FROM intents WHERE id=?", intent_id)
        if not intent:
            raise ValueError("Unknown paper intent.")
        if type(cumulative_qty) is not int or not 0 <= cumulative_qty <= intent["qty"]:
            raise ValueError("A cumulative fill must be within the intent quantity.")
        if cumulative_qty <= intent["filled"]:
            return False
        if (self._account()["halt"] or "").startswith(RECONCILIATION_FAILURE):
            raise ValueError(
                "Review the ledger receipt mismatch before applying another fill."
            )
        if intent["status"] == "canceled" or at < intent["created"]:
            raise ValueError(
                "A canceled or future paper intent cannot receive this fill."
            )
        last_fill = self._one(
            "SELECT max(at) AS at FROM fills WHERE intent_id=?", intent_id
        )["at"]
        if last_fill and at < last_fill:
            raise ValueError(
                "The higher cumulative fill is older than the last fill report."
            )
        qty = cumulative_qty - intent["filled"]
        levels, rate = json.loads(intent["levels"]), Decimal(intent["rate"])
        matches = list(_portion(levels, intent["filled"], cumulative_qty))
        premium = sum(p * q for p, q in matches)
        micro = intent["micro"] + sum(_micro_fee(p, q, rate) for p, q in matches)
        fee = _cent_fee(micro)
        fee_delta = fee - intent["fee"]
        remaining = list(_portion(levels, cumulative_qty, intent["qty"]))
        remaining_fee = (
            _cent_fee(micro + sum(_micro_fee(p, 1, rate) * q for p, q in remaining))
            - fee
        )
        reserved = sum((p + ALLOWANCE_CENTS) * q for p, q in remaining) + remaining_fee
        if reserved + premium + fee_delta > intent["reserved"]:
            raise RuntimeError(
                "The debit exceeds the amount reserved for this paper order."
            )
        if not remaining:
            status = "filled"
        elif intent["status"] == "unknown":
            status = "unknown"
        else:
            status = "partial"
        fill_id = f"{intent_id}:{cumulative_qty}"
        self.db.execute(
            "INSERT INTO fills VALUES(?,?,?,?,?,?,?)",
            (fill_id, intent_id, at, cumulative_qty, qty, premium, fee_delta),
        )
        self.db.execute(
            """
            UPDATE intents SET filled=?,premium=premium+?,fee=?,micro=?,reserved=?,status=?,
            cancel_requested=CASE WHEN ?=0 THEN 0 ELSE cancel_requested END WHERE id=?
        """,
            (
                cumulative_qty,
                premium,
                fee,
                micro,
                reserved,
                status,
                reserved,
                intent_id,
            ),
        )
        self._event(
            fill_id,
            at,
            "fill",
            intent["market_id"],
            f"Filled {qty} {intent['side']} contracts from visible paper depth.",
            intent_id=intent_id,
            qty=qty,
            cumulative_qty=cumulative_qty,
            premium_cents=premium,
            fee_cents=fee_delta,
            side=intent["side"],
            levels=matches,
        )
        return True

    def apply_fill(self, intent_id, cumulative_qty, at):
        """Reconcile a cumulative receipt using the original immutable paper depth."""
        stamp = _utc(at)
        with self._transaction():
            changed = self._fill(intent_id, cumulative_qty, stamp)
            if changed:
                clock = self._clock(stamp)
                intent = self._one("SELECT * FROM intents WHERE id=?", intent_id)
                self._finalize(intent["market_id"], clock)
                self._risk(clock)
            return {"applied": changed, "intent_id": intent_id}

    def mark_unknown(self, intent_id, at):
        stamp = _utc(at)
        with self._transaction():
            intent = self._one("SELECT * FROM intents WHERE id=?", intent_id)
            if not intent:
                raise ValueError("Unknown paper intent.")
            if intent["reserved"] and intent["status"] != "unknown":
                if stamp < intent["created"]:
                    raise ValueError("The incident predates the intent.")
                self.db.execute(
                    "UPDATE intents SET status='unknown' WHERE id=?", (intent_id,)
                )
                self._event(
                    intent_id,
                    stamp,
                    "unknown",
                    intent["market_id"],
                    "The paper adapter lost its submission response. Cash remains reserved.",
                    intent_id=intent_id,
                )
            return {"intent_id": intent_id}

    def reconcile_intent(self, intent_id, cumulative_qty, at, *, terminal=True):
        """Use the paper ledger’s reconciled totals. A terminal report cancels any unfilled remainder."""
        stamp = _utc(at)
        with self._transaction():
            intent = self._one("SELECT * FROM intents WHERE id=?", intent_id)
            if (
                not intent
                or type(cumulative_qty) is not int
                or cumulative_qty < intent["filled"]
            ):
                raise ValueError("The reconciliation contradicts confirmed fills.")
            if stamp < intent["created"]:
                raise ValueError("The reconciliation predates the intent.")
            self._fill(intent_id, cumulative_qty, stamp)
            intent = self._one("SELECT * FROM intents WHERE id=?", intent_id)
            if terminal:
                self._cancel(
                    intent,
                    stamp,
                    "The paper adapter confirmed the final cumulative quantity and canceled the remainder.",
                )
            elif intent["status"] == "unknown":
                self.db.execute(
                    "UPDATE intents SET status=? WHERE id=?",
                    ("partial" if intent["filled"] else "reserved", intent_id),
                )
                if intent["cancel_requested"]:
                    self._cancel(
                        intent,
                        stamp,
                        "The paper adapter acknowledged the pending cancellation.",
                    )
            clock = self._clock(stamp)
            self._finalize(intent["market_id"], clock)
            self._risk(clock)
            return {
                "intent_id": intent_id,
                "status": self._one("SELECT status FROM intents WHERE id=?", intent_id)[
                    "status"
                ],
            }

    def entry_control(self):
        """Read the durable entry gates without rebuilding positions or market detail."""
        with self._transaction(write=False):
            account = self._account()
            return {
                "manual_pause": bool(account["manual_pause"]),
                "risk_pause_reason": account["halt"],
            }

    def set_paused(self, paused, at=None, *, acknowledge_risk=True):
        if type(paused) is not bool:
            raise ValueError("Pause must be a boolean.")
        stamp = _utc(at) if at else _wall()
        with self._transaction():
            account = self._account()
            # UI wall time must not advance a historical synthetic replay.
            clock = (
                self._clock(stamp)
                if account["source"] == "kalshi"
                else account["clock"]
            )
            clock = clock or stamp
            if paused != bool(account["manual_pause"]):
                self.db.execute(
                    "UPDATE account SET manual_pause=? WHERE id=1", (int(paused),)
                )
                self._event(
                    (stamp, paused),
                    stamp,
                    "pause" if paused else "resume",
                    None,
                    "The owner paused new paper entries."
                    if paused
                    else "The owner released the manual entry pause.",
                )
            if paused:
                self._cancel_resting(stamp)
            elif (
                acknowledge_risk
                and account["halt"]
                and self._reconcile_balances()
                and not self._loss_reason(self._portfolio(_dt(clock)))
            ):
                self.db.execute("UPDATE account SET halt=NULL WHERE id=1")
                self._event(
                    stamp,
                    stamp,
                    "risk_resume",
                    None,
                    "The owner cleared the risk pause after the account returned within limits.",
                )
            account = self._risk(clock)
            return {
                "paused": bool(account["manual_pause"] or account["halt"]),
                "risk_reason": account["halt"],
            }

    def _finalize(self, market_id, at):
        result = self._one("SELECT * FROM settlements WHERE market_id=?", market_id)
        if not result or result["status"] != "pending":
            return
        if (self._account()["halt"] or "").startswith(RECONCILIATION_FAILURE):
            self._event(
                market_id,
                at,
                "settlement_pending",
                market_id,
                "Venue result recorded. Cash posting is on hold for ledger reconciliation.",
            )
            return
        intent = self._entry(market_id)
        if intent and intent["status"] == "unknown" and intent["reserved"]:
            self.db.execute(
                "UPDATE intents SET cancel_requested=1 WHERE id=?", (intent["id"],)
            )
            self._event(
                market_id,
                at,
                "settlement_pending",
                market_id,
                "Venue result received. An unknown submission must reconcile before posting cash.",
                result=result["result"],
                source=result["source"],
            )
            return
        payout = realized = 0
        if intent:
            self._cancel(
                intent, at, "Venue settlement canceled the remaining paper quantity."
            )
            if result["result"] == "void":
                payout = intent["premium"]
            elif result["result"].upper() == intent["side"]:
                payout = intent["filled"] * 100
            realized = payout - intent["premium"] - intent["fee"]
        self.db.execute(
            "UPDATE settlements SET status='applied',payout=?,realized=? WHERE market_id=?",
            (payout, realized, market_id),
        )
        if result["result"] in ("yes", "no"):
            self._grade(market_id, result["result"], result["at"])
        self._event(
            market_id,
            at,
            "settlement",
            market_id,
            f"Venue result {result['result'].upper()}. Paper settlement posted.",
            result=result["result"],
            source=result["source"],
            payout_cents=payout,
            realized_cents=realized,
        )

    def _grade(self, market_id, result, settled_at):
        """Freeze price, model and climatology at one pre-close observation, even on a pass."""
        outcome = int(result == "yes")
        for snap in self.db.execute(
            """SELECT id,first_seen,packet FROM snapshots WHERE market_id=?
                                    AND first_seen<=? ORDER BY first_seen,rowid""",
            (market_id, settled_at),
        ):
            p = json.loads(snap["packet"])
            decision = _dt(snap["first_seen"])
            try:
                if (
                    not p.get("rules")
                    or not p.get("rule_version")
                    or not p.get("event_date")
                    or p.get("tradable") is not True
                    or decision >= _dt(p.get("close_at"))
                ):
                    continue
                _book_age_ok(p, decision)
                books = _levels(p)
                model, _, _ = _forecast(p, decision)
                baseline = float(_number(p["forecast"]["baseline"]))
            except (ValueError, OverflowError):
                continue
            market = (books["yes"][0][0] + 100 - books["no"][0][0]) / 200
            self.db.execute(
                "INSERT OR IGNORE INTO assessments VALUES(?,?,?,?,?,?,?,?)",
                (
                    market_id,
                    p["event_date"],
                    snap["id"],
                    snap["first_seen"],
                    outcome,
                    float(model),
                    market,
                    baseline,
                ),
            )
            self._refresh_scores()
            return
        # Remember an ungradable outcome. Evidence arriving after settlement
        # must never make an earlier forecast appear retrospectively eligible.
        self.db.execute(
            "INSERT OR IGNORE INTO assessments(market_id,outcome) VALUES(?,?)",
            (market_id, outcome),
        )

    def _refresh_scores(self):
        scores = self._one("""
            SELECT count(*) AS resolved_dates,coalesce(sum(n),0) AS markets,
              avg(model) AS model_brier,avg(market) AS market_brier,avg(climate) AS climatology_brier
            FROM (SELECT event_date,count(*) AS n,avg((model_p-outcome)*(model_p-outcome)) AS model,
                  avg((market_p-outcome)*(market_p-outcome)) AS market,
                  avg((climatology_p-outcome)*(climatology_p-outcome)) AS climate
                  FROM assessments WHERE snapshot IS NOT NULL GROUP BY event_date)
        """)
        self.db.execute(
            "INSERT OR REPLACE INTO score_summary VALUES(1,?)", (_json(dict(scores)),)
        )

    def _scorecard(self, source):
        values = json.loads(
            self._one("SELECT data FROM score_summary WHERE id=1")["data"]
        )
        return {
            **values,
            "source": source,
            "weighting": "equal event-date weight",
            "observation": "first assessable pre-close snapshot per market",
        }

    def settle(self, market_id, result, observed_at, source):
        """Record one authoritative result. Repeating the same result is a no-op."""
        normalized = result.lower() if isinstance(result, str) else ""
        stamp = _utc(observed_at)
        if normalized not in ("yes", "no", "void"):
            raise ValueError(
                "Unsupported exceptional settlement. A standard YES, NO or documented void is required."
            )
        with self._transaction():
            row = self._market(market_id)
            if not row:
                raise ValueError("Unknown market.")
            packet = json.loads(row["packet"])
            if not isinstance(source, str):
                raise ValueError("A venue settlement source is required.")
            host = urlparse(source).hostname or ""
            if packet["book_source"] == "fixture":
                official = source == "fixture" or source.startswith(
                    ("fixture:venue", "fixture://venue")
                )
            else:
                official = source == "kalshi" or (
                    source.startswith("https://")
                    and (
                        host in ("kalshi.com", "kalshi.co")
                        or host.endswith((".kalshi.com", ".kalshi.co"))
                    )
                )
            if not official:
                raise ValueError(
                    "Settlement requires the venue's final result for this account."
                )
            existing = self._one(
                "SELECT * FROM settlements WHERE market_id=?", market_id
            )
            if existing:
                if existing["result"] != normalized:
                    raise ValueError(
                        "The venue result conflicts with the recorded settlement."
                    )
                if existing["status"] == "pending":
                    clock = self._clock(stamp)
                    self._finalize(market_id, clock)
                    self._risk(clock)
                    existing = self._one(
                        "SELECT * FROM settlements WHERE market_id=?", market_id
                    )
                return dict(existing)
            if normalized != "void" and _dt(stamp) < _dt(packet.get("close_at")):
                raise ValueError("The venue observation predates contract close.")
            if (
                normalized == "void"
                and packet["book_source"] != "fixture"
                and packet.get("void_policy") != "refund_premium_keep_fees"
            ):
                raise ValueError("The exceptional payout policy requires venue review.")
            intent = self._entry(market_id)
            if intent and stamp < intent["created"]:
                raise ValueError("The settlement predates the paper entry.")
            self.db.execute(
                "INSERT INTO settlements(market_id,result,at,source,status) VALUES(?,?,?,?,'pending')",
                (market_id, normalized, stamp, source),
            )
            at = self._clock(stamp)
            self._finalize(market_id, at)
            self._risk(at)
            return dict(
                self._one("SELECT * FROM settlements WHERE market_id=?", market_id)
            )

    def state(self, now=None):
        with self._transaction(write=False):
            account = self._account()
            if now is not None:
                at = _utc(now)
            elif account["source"] == "fixture":
                at = account["clock"] or _wall()
            else:
                at = max(_wall(), account["clock"] or "")
            instant = _dt(at)
            paused = bool(account["manual_pause"] or account["halt"])
            markets = []
            for (
                row
            ) in self.db.execute("""SELECT m.*,s.packet,s.forecast_id FROM markets m
                                        JOIN snapshots s ON s.id=m.snapshot ORDER BY m.id"""):
                packet = json.loads(row["packet"])
                value = json.loads(row["verdict"]) or _empty(
                    "Watch", "Awaiting the first paper decision."
                )
                active = self._active_verdict(row, at)
                if active:
                    value = {**value, **active}
                elif paused:
                    value = {
                        **value,
                        "decision": "Watch",
                        "reason": account["halt"]
                        or "New paper entries are paused by the owner.",
                    }
                elif value["decision"] != "Watch":
                    try:
                        _book_age_ok(packet, instant)
                        if instant >= _dt(packet.get("close_at")):
                            raise ValueError(
                                "The entry window has closed. Waiting for a venue result."
                            )
                    except ValueError as exc:
                        value = {**value, "decision": "Watch", "reason": str(exc)}
                settlement = self._one(
                    "SELECT * FROM settlements WHERE market_id=?", row["id"]
                )
                if settlement:
                    value["settlement"] = {
                        "result": settlement["result"],
                        "source": settlement["source"],
                        "observed_at": settlement["at"],
                        "status": settlement["status"],
                        "payout_cents": settlement["payout"],
                        "realized_cents": settlement["realized"],
                    }
                age = _age(packet, instant)
                if value.get("market_baseline") is None:
                    try:
                        books = _levels(packet)
                        value["market_baseline"] = (
                            books["yes"][0][0] + 100 - books["no"][0][0]
                        ) / 200
                    except ValueError:
                        value["market_baseline"] = None
                markets.append(
                    {
                        **packet,
                        **value,
                        "quote_age_seconds": int(age) if age is not None else None,
                        "forecast_id": row["forecast_id"],
                        "snapshot_id": row["snapshot"],
                    }
                )
            rank = {"Paper buy": 0, "Held": 1, "Pass": 2, "Watch": 3, "Settled": 4}
            markets.sort(
                key=lambda m: (
                    rank.get(m["decision"], 5),
                    -(m.get("net_cents") or 0),
                    m["id"],
                )
            )
            activity = [
                {
                    "id": r["id"],
                    "at": r["at"],
                    "kind": r["kind"],
                    "market_id": r["market_id"],
                    "message": r["message"],
                    **json.loads(r["data"]),
                }
                for r in self.db.execute(
                    "SELECT * FROM activity ORDER BY at DESC,rowid DESC LIMIT 500"
                )
            ]
            return {
                "markets": markets,
                "portfolio": self._portfolio(instant),
                "activity": activity,
                "paused": paused,
                "scorecard": self._scorecard(account["source"]),
                "manual_pause": bool(account["manual_pause"]),
                "risk_pause_reason": account["halt"],
                "account_source": account["source"],
                "clock": account["clock"],
            }
