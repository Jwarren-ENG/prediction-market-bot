"""Synthetic, network-free BTC contract execution and accounting checks."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

from worker import btc_broker as broker, weather

UTC = timezone.utc
START = datetime(2026, 1, 5, 17, 0, tzinfo=UTC)
T = START + timedelta(minutes=1)


def identifier(product, opened):
    if product == "kalshi":
        end = weather.eastern(opened + timedelta(minutes=15))
        return f"KXBTC15M-{end:%y}{end:%b}".upper() + f"{end:%d%H%M}-{end:%M}"
    duration = "1h" if product.endswith("hourly") else "5m"
    return f"cpc-btc-updown-{duration}-{opened:%Y-%m-%d-%H%M}z"


def source(product, mid, suffix):
    if product == "kalshi":
        return f"https://external-api.kalshi.com/trade-api/v2/markets/{mid}" + suffix
    return f"https://gateway.polymarket.us/v1/markets/{mid}" + suffix


def levels(yes, no):
    output = {}
    for side, bs, opp in (("yes", yes, no), ("no", no, yes)):
        output[side] = dict(
            bids=[list(v) for v in bs], asks=[[float(100 - Decimal(str(p))), n] for p, n in opp]
        )
    return output


def feed(product="kalshi", *, now=T, opened=START, yes=((49, 40),), no=((49, 40),), changes=None):
    venue, mins, _ = broker.PRODUCTS[product]
    mid = identifier(product, opened)
    meta = dict(
        id=mid,
        venue=venue,
        title="Fixture BTC window",
        open_at=weather.iso(opened),
        close_at=weather.iso(opened + timedelta(minutes=mins)),
        observed_at=weather.iso(now),
        source_at=weather.iso(now),
        http_date=weather.iso(now),
        target_usd="123.45",
        book_url=source(product, mid, "/orderbook?depth=5" if product == "kalshi" else "/book"),
        rules_primary="Fixture venue-published rule",
        rules_verified=True,
        fee_source=broker.FEE_URL[venue],
        fee_verified_at=weather.iso(now),
        fee_valid_until=weather.iso(opened + timedelta(minutes=mins)),
        fee_rate="0.07" if product == "kalshi" else "0.11",
        depth=levels(yes, no),
        history=[],
    )
    if changes:
        for offset, bid, ask in changes:
            at = weather.iso(now + timedelta(seconds=offset))
            meta["history"].append(
                dict(kind="book", at=at, source_at=at, http_date=at, yes_bid_cents=bid, yes_ask_cents=ask)
            )
    return {product: dict(status="live", enabled=True, current=meta)}


def order(product="kalshi", *, cid="one", action="buy", side="yes", qty=2, limit=51, opened=START):
    return dict(
        client_id=cid,
        venue=product,
        market_id=identifier(product, opened),
        action=action,
        side=side,
        qty=qty,
        limit_cents=limit,
    )


def settled(product, at, *, opened=START, result="YES", url=None):
    mid = identifier(product, opened)
    return dict(
        venue=broker.PRODUCTS[product][0],
        market_id=mid,
        result=result,
        observed_at=weather.iso(at),
        source=url or source(product, mid, "" if product == "kalshi" else "/settlement"),
    )


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.paper = broker.BTCBroker(self.dir)

    def test_personal_size_fits_whole_contracts_with_fees_and_resizes_with_equity(self):
        for initial, qty, fee in ((2500, 1, 2), (5000, 3, 5), (10000, 7, 12)):
            with self.subTest(starting_cents=initial):
                paper = broker.BTCBroker(self.dir, starting_cents=initial)
                q = feed(yes=((62, 40),), no=((36, 40),), changes=((-20, 51, 53), (-10, 57, 59), (0, 62, 64)))
                paper.sync(q, now=T)
                paper.set_auto(True)
                paper.sync(q, now=T + timedelta(seconds=1))
                paper.sync(q, now=T + timedelta(seconds=2))
                state = paper.state(T + timedelta(seconds=2), equity_minutes=15)
                self.assertEqual(len(state["fills"]), 1)
                fill = state["fills"][0]
                self.assertEqual((fill["qty"], fill["fee_cents"], fill["entry_budget_cents"]), (qty, fee, initial // 20))
                self.assertEqual(state["portfolio"]["cash_cents"], initial - 64 * qty - fee)
                self.assertEqual(state["portfolio_history"]["points"][0][1], initial)
                self.assertEqual(state["portfolio_history"]["account"], "bankroll")
                self.assertLess(state["limits"]["max_order_cents"], initial // 20)
                paper.set_auto(False)
                end = START + timedelta(minutes=15)
                paper.sync({}, [settled("kalshi", end)], now=end)
                restored = broker.BTCBroker(self.dir, starting_cents=initial).state(end)
                gain = (100 - 64) * qty - fee
                self.assertEqual(restored["portfolio"]["net_gain_cents"], gain)
                self.assertEqual(restored["limits"]["max_order_cents"], (initial + gain) // 20)
                self.assertFalse(restored["auto_enabled"])
        self.assertEqual(self.paper.state(T)["portfolio"]["cash_cents"], broker.INITIAL)

    def test_fee_rounding_reduces_a_two_lot_and_pause_keeps_exit_working(self):
        paper = broker.BTCBroker(self.dir, starting_cents=2500)
        paper.set_auto(True)
        q = feed(yes=((60, 40),), no=((38, 40),), changes=((-20, 47, 49), (-10, 53, 55), (0, 60, 62)))
        paper.sync(q, now=T)
        fill = paper.state(T)["fills"][0]
        self.assertEqual((fill["qty"], fill["cash_amount_cents"]), (1, 64))
        paper.set_auto(False)
        later = T + timedelta(seconds=10)
        paper.sync(feed(now=later, yes=((48, 40),), no=((50, 40),)), now=later)
        self.assertFalse(paper.has_holdings())
        self.assertEqual(paper.state(later)["fills"][0]["action"], "sell")
        self.assertEqual(paper.state(later)["portfolio"]["net_gain_cents"], -18)

    def test_personal_loss_room_can_rule_out_every_contract_and_resets_each_eastern_day(self):
        paper = broker.BTCBroker(self.dir, starting_cents=2500)
        paper.set_auto(True)
        for i in range(4):
            opened = START + timedelta(minutes=15 * i)
            now = opened + timedelta(minutes=1)
            q = feed(opened=opened, now=now, yes=((62, 40),), no=((36, 40),),
                     changes=((-20, 51, 53), (-10, 57, 59), (0, 62, 64)))
            paper.sync(q, now=now)
            self.assertEqual(paper.has_holdings(), i < 3)
            end = opened + timedelta(minutes=15)
            paper.sync({}, (settled("kalshi", end, opened=opened, result="NO"),), now=end)
        state = paper.state(end)
        self.assertEqual(state["portfolio"]["net_gain_cents"], -198)
        self.assertEqual(state["budget"]["entry_room_cents"], 52)
        self.assertEqual(state["limits"]["daily_loss_cents"], 250)
        self.assertEqual(state["performance"]["max_drawdown_cents"], 198)
        next_day = START + timedelta(days=1)
        paper.sync({}, now=next_day)
        renewed = paper.state(next_day)
        self.assertEqual(renewed["limits"]["daily_loss_cents"], 230)
        self.assertEqual(renewed["budget"]["entry_room_cents"], 115)

    def test_portfolio_chart_moves_on_a_new_bid_between_fills(self):
        self.paper.sync(feed(), now=T)
        self.paper.place(order(), feed(), T + timedelta(seconds=1))
        later = T + timedelta(seconds=8)
        self.paper.sync(feed(now=later, yes=((57,40),), no=((39,40),)), now=later)
        state = self.paper.state(later + timedelta(seconds=1), equity_minutes=15)
        self.assertEqual(state["portfolio"]["filled_orders"], 1)
        values = [p[1] for p in state["portfolio_history"]["points"]]
        self.assertLess(values[1], values[0])
        self.assertGreater(values[2], values[1])
        self.assertEqual(values[-1], state["portfolio"]["equity_cents"])

    def test_separate_accounts_manual_fee_marks_and_restart(self):
        for key in broker.PRODUCTS:
            with self.subTest(key=key):
                desk = broker.BTCBroker(self.dir, key)
                self.assertFalse(desk.state(T)["auto_enabled"])
                self.assertEqual(desk.state(T)["portfolio"]["equity_cents"], 1_000_000)
                quote = feed(key)
                o = order(key)
                filled = desk.place(o, quote, T)
                self.assertEqual(filled["status"], "filled")
                self.assertEqual(filled["fee_cents"], 4 if key == "kalshi" else 5)
                fee = filled["fee_cents"]
                self.assertEqual(filled["cash_delta_cents"], -102 - fee)
                state = broker.BTCBroker(self.dir, key).state(T, equity_minutes=60)
                self.assertEqual(state["portfolio"]["cash_cents"], 1_000_000 - 102 - fee)
                self.assertEqual(state["portfolio"]["equity_cents"], 1_000_000 - 4 - 2 * fee)
                self.assertEqual(state["portfolio_history"]["points"][-1][1], 1_000_000 - 4 - 2 * fee)
                stale = desk.state(T + timedelta(seconds=90), equity_minutes=60)["portfolio_history"]
                self.assertIsNone(stale["current_cents"])
                self.assertTrue(stale["has_gaps"])
                self.assertEqual(state["positions"][0]["auto_qty"], 0)
                self.assertEqual(state["daily"]["net_gain_cents"], -4 - 2 * fee)
                self.assertEqual(state["daily"]["fees_cents"], fee)
                self.assertEqual(state["daily"]["opening_equity_cents"], 1_000_000)
                self.assertEqual(state["performance"]["win_rate_pct"], None)
                self.assertEqual(desk.place(o, {}, T + timedelta(hours=1)), filled)
                self.assertEqual(desk.state(T)["portfolio"]["filled_orders"], 1)
        self.paper.set_auto(True)
        self.assertTrue(broker.BTCBroker(self.dir, "kalshi").state(T)["auto_enabled"])
        self.assertFalse(broker.BTCBroker(self.dir, "polymarket_us").state(T)["auto_enabled"])
        with self.assertRaises(ValueError):
            self.paper.place(order("polymarket_us"), feed("polymarket_us"), T)
        with self.assertRaises(ValueError):
            broker.BTCBroker(self.dir, "polymarket_us_hourly").place(
                {**order("polymarket_us"), "venue": "polymarket_us_hourly"}, feed("polymarket_us_hourly"), T
            )

    def test_sweep_limit_depth_fee_and_complementary_consumption(self):
        q = feed(yes=((48, 10), (47, 30)), no=((49, 1.9), (48, 2.8)))
        bad = self.paper.place(order(cid="price", limit=51), q, T)
        self.assertEqual(bad["status"], "rejected")
        o = order(qty=3, limit=52)
        fill = self.paper.place(o, q, T)
        self.assertEqual(fill["levels"], [[51, 1], [52, 2]])
        self.assertAlmostEqual(fill["price_cents"], 155 / 3)
        self.assertEqual((fill["fee_cents"], fill["cash_amount_cents"]), (6, 161))
        self.assertEqual(self.paper.place(order(cid="reuse", qty=1, limit=99), q, T)["status"], "rejected")
        with self.assertRaisesRegex(ValueError, "different order"):
            self.paper.place({**o, "qty": 2}, q, T)
        # A buy NO/sell YES share the same YES bid. Occupied sell-side depth
        # also reduces the liquidation mark for already-held YES.
        no_order = order(cid="no", side="no", qty=10, limit=52)
        self.assertEqual(self.paper.place(no_order, q, T)["status"], "filled")
        self.assertEqual(
            self.paper.place(order(cid="no-bid", action="sell", qty=1, limit=48), q, T)["status"], "rejected"
        )
        # A new HTTP receipt carrying identical depth never resets already consumed size.
        new = T + timedelta(seconds=10)
        with_same = feed(now=new, yes=((48, 10), (47, 30)), no=((49, 1.9), (48, 2.8)))
        self.assertEqual(
            self.paper.place(order(cid="same-depth", qty=1, limit=99), with_same, new)["status"], "rejected"
        )

    def test_limit_price_fixed_point_per_venue_fees_and_cash_conservation(self):
        q = feed(yes=((48.98, 10),), no=((49.99, 10),))
        r = self.paper.place(order(qty=1, limit=50.01), q, T)
        self.assertEqual((r["price_cents"], r["fee_cents"], r["cash_amount_cents"]), (50.01, 2, 53))
        sold = self.paper.place(order(cid="exit", action="sell", qty=1, limit=48.98), q, T)
        self.assertEqual((sold["fee_cents"], sold["cash_delta_cents"], sold["realized_cents"]), (2, 46, -7))
        p = self.paper.state(T)["portfolio"]
        self.assertEqual(p["realized_cents"], p["cash_cents"] - p["initial_cash_cents"])
        # At exactly 2.5 cents per fill, PMUS uses half-even. Multiple price levels
        # can never exceed the rounded cumulative commission.
        self.assertEqual(broker._fee("polymarket_us", Decimal("0.1"), [(5000, 1)]), 2)
        self.assertEqual(broker._fee("kalshi", Decimal("0.1"), [(5000, 1)]), 3)
        rate = Decimal("0.07")
        self.assertEqual(broker._fee("polymarket_us", rate, [(4900, 1), (4800, 1)]), 3)
        self.assertEqual(broker._fee("polymarket_us", Decimal("0.01"), [(4900, 1), (4800, 1), (4700, 1)]), 1)

    def test_concurrent_same_client_conflict_and_all_or_none_liquidity(self):
        q = feed(no=((49, 3),))
        other = broker.BTCBroker(self.dir)
        barrier = Barrier(2)

        def fill(desk, cid):
            barrier.wait(timeout=5)
            return desk.place(order(cid=cid, qty=3), q, T)

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(fill, self.paper, "same")
            b = pool.submit(fill, other, "same")
            self.assertEqual(a.result(), b.result())
        self.assertEqual(self.paper.state(T)["portfolio"]["filled_orders"], 1)
        newer = T + timedelta(seconds=10)
        q = feed(now=newer, no=((49, 4),))
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(lambda: other.place(order(cid="a", qty=4), q, newer))
            b = pool.submit(lambda: self.paper.place(order(cid="b", qty=4), q, newer))
            self.assertEqual(sorted((a.result()["status"], b.result()["status"])), ["filled", "rejected"])

    def test_quote_guards_missing_fees_wrong_product_stale_and_future_are_durable(self):
        changes = [
            {"fee_rate": None},
            {"fee_source": "https://example.invalid/fees"},
            {"fee_valid_until": weather.iso(T)},
            {"fee_verified_at": weather.iso(T + timedelta(seconds=1))},
            {"rules_verified": False},
            {"source_at": weather.iso(T - timedelta(seconds=21))},
            {"book_url": "https://example.invalid/book"},
            {"source_at": weather.iso(T + timedelta(seconds=4))},
            {"depth": levels(((60, 2),), ((42, 2),))},
            {
                "depth": {
                    "yes": {"bids": [[49, 2]], "asks": [[51, 2]]},
                    "no": {"bids": [[49, 3]], "asks": [[51, 2]]},
                }
            },
            {"open_at": weather.iso(START - timedelta(minutes=1))},
            {"id": identifier("polymarket_us", START)},
            {"observed_at": weather.iso(T + timedelta(seconds=1))},
            {"close_at": weather.iso(START + timedelta(hours=1))},
            {"depth": levels(((49, 0.5),), ((49, 0.8),))},
        ]
        for i, mut in enumerate(changes):
            q = feed()
            q["kalshi"]["current"].update(mut)
            o = order(cid=f"reject-{i}")
            self.assertEqual(self.paper.place(o, q, T)["status"], "rejected", mut)
            self.assertEqual(self.paper.place(o, feed(), T)["status"], "rejected")
        for bad in [False, "1E9999999", "-Infinity", "NaN", 100, 0, 0.001]:
            with self.assertRaises(ValueError):
                self.paper.place(order(limit=bad), feed(), T)
        for i, change in enumerate(({"status": "error"}, {"enabled": False})):
            q = feed()
            q["kalshi"].update(change)
            self.assertEqual(self.paper.place(order(cid=f"off-{i}"), q, T)["status"], "rejected")
        self.assertEqual(self.paper.state(T)["portfolio"]["filled_orders"], 0)

    def test_stale_mark_pending_unknown_refused_old_snapshot_and_official_only(self):
        self.assertEqual(self.paper.place(order(), feed(), T)["status"], "filled")
        t2 = T + timedelta(seconds=21)
        self.assertIsNone(self.paper.state(t2)["portfolio"]["equity_cents"])
        self.assertIsNone(self.paper.state(t2)["daily"]["net_gain_cents"])
        self.paper.sync(feed(now=t2), now=t2)
        self.assertIsNotNone(self.paper.state(t2)["portfolio"]["equity_cents"])
        self.assertEqual(self.paper.place(order(cid="older"), feed(), t2)["status"], "rejected")
        close = START + timedelta(minutes=15)
        self.assertEqual(self.paper.pending_settlements(close)[0]["market_id"], identifier("kalshi", START))
        for r in [
            settled("kalshi", close, result="VOID"),
            settled("kalshi", close, result="pending"),
            settled("kalshi", close, url="https://example.invalid/winner"),
            settled("kalshi", close - timedelta(seconds=1)),
            settled("kalshi", close + timedelta(seconds=1)),
        ]:
            self.paper.sync({}, [r], close)
        pending = self.paper.state(close)
        self.assertEqual(pending["positions"][0]["mark_status"], "Pending official settlement")
        self.assertIsNone(pending["portfolio"]["net_gain_cents"])
        archived = settled("kalshi", close)
        archived["source"] = archived["source"].replace("/v2/markets/", "/v2/historical/markets/")
        self.paper.sync({}, [archived, archived], close)
        state = broker.BTCBroker(self.dir).state(close)
        self.assertEqual(state["portfolio"]["net_gain_cents"], 200 - 106)
        self.assertEqual(state["performance"]["profitable_windows"], 1)
        self.assertEqual(state["performance"]["net_cents"], 94)
        self.assertEqual(state["performance"]["win_rate_pct"], 100)
        self.assertEqual(state["performance"]["auto_only"]["closed_windows"], 0)
        self.assertEqual(self.paper.pending_settlements(close), [])
        self.assertEqual(sum(e["kind"] == "settlement" for e in state["activity"]), 1)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.paper.sync({}, [settled("kalshi", close, result="NO")], close)

    def test_partial_sales_and_position_limits_persist(self):
        self.paper.set_limits(
            {
                "max_order_qty": 6,
                "max_order_cents": 320,
                "max_window_cents": 350,
                "max_open_cents": 400,
                "daily_loss_cents": 500,
            }
        )
        q = feed()
        self.assertEqual(self.paper.place(order(qty=6), q, T)["status"], "filled")
        self.assertIn("window", self.paper.place(order(cid="over"), q, T)["reason"])
        self.assertIn("per-order", self.paper.place(order(cid="qty", qty=7), q, T)["reason"])
        self.assertIn(
            "hold",
            self.paper.place(order(cid="short", action="sell", side="no", qty=1, limit=1), q, T)["reason"],
        )
        self.assertEqual(
            self.paper.place(order(cid="half", action="sell", qty=3, limit=49), q, T)["status"], "filled"
        )
        self.assertEqual(self.paper.state(T)["positions"][0]["qty"], 3)
        self.paper.set_limits({"max_window_cents": 1, "max_open_cents": 1, "daily_loss_cents": 1})
        cash = self.paper.place(order(cid="rest", action="sell", qty=3, limit=49), q, T)
        self.assertEqual(cash["status"], "filled")
        state = broker.BTCBroker(self.dir).state(T)
        self.assertEqual(state["portfolio"]["net_gain_cents"], state["portfolio"]["realized_cents"])
        self.assertEqual(state["positions"], [])
        self.paper.set_limits({"max_window_cents": 1000, "max_open_cents": 1000})
        self.assertIn("daily loss", self.paper.place(order(cid="daily", qty=1), q, T)["reason"])
        self.assertEqual(self.paper.state(START + timedelta(minutes=15))["performance"]["loss_windows"], 1)
        for limits in ({"max_order_qty": 101}, {"daily_loss_cents": 0}, {"x": 10}, {"max_order_cents": True}):
            with self.assertRaises(ValueError):
                self.paper.set_limits(limits)

    def test_auto_rule_three_venues_once_per_window_manual_ownership_and_exit(self):
        changes = ((-20, 48, 50), (-10, 52, 54), (0, 56, 58))
        for key in broker.PRODUCTS:
            with self.subTest(key=key):
                paper = broker.BTCBroker(self.dir, key)
                q = feed(key, yes=((56, 40),), no=((42, 40),), changes=changes)
                paper.sync(q, now=T)
                self.assertEqual(paper.state(T)["portfolio"]["filled_orders"], 0)
                paper.set_auto(True)
                paper.sync(q, now=T, allow_auto=False)
                self.assertEqual(paper.state(T)["portfolio"]["filled_orders"], 0)
                paper.place(order(key, qty=1, limit=58), q, T)
                paper.sync(q, now=T)
                paper.sync(q, now=T)
                st = paper.state(T)
                self.assertEqual(st["positions"][0]["auto_qty"], 5)
                self.assertEqual(st["positions"][0]["qty"], 6)
                self.assertEqual(st["portfolio"]["filled_orders"], 2)
                self.assertEqual(st["markets"][0]["signal_side"], "yes")
                auto = next(r for r in st["fills"] if r["source"] == "auto")
                self.assertEqual(len(auto["signal_evidence"]), 3)
                self.assertEqual(auto["rule_version"], "venue-book-momentum-v1")
                # One manual lot stays held through the automatic retracement exit.
                new = T + timedelta(seconds=10)
                q2 = feed(key, now=new, yes=((48, 40),), no=((50, 40),))
                paper.sync(q2, now=new)
                self.assertEqual(paper.state(new)["positions"][0]["qty"], 1)
                self.assertEqual(paper.state(new)["positions"][0]["auto_qty"], 0)
                paper.set_auto(False)
                self.assertFalse(broker.BTCBroker(self.dir, key).state(new)["auto_enabled"])

    def test_auto_rejects_gap_nonadvancing_source_spread_and_missing_ticks(self):
        changes = ((-20, 39, 41), (-10, 34, 36), (0, 28, 30))
        key = "polymarket_us"
        desk = broker.BTCBroker(self.dir, key)
        desk.set_auto(True)
        q = feed(key, yes=((28, 40),), no=((70, 40),), changes=changes)
        for i in range(4):
            bad = copy.deepcopy(q)
            rows = bad[key]["current"]["history"]
            if i == 0:
                rows[0]["kind"] = "gap"
            if i == 1:
                rows[1]["source_at"] = rows[0]["source_at"]
            if i == 2:
                rows[0]["yes_bid_cents"] = 36
            if i == 3:
                rows[0].pop("source_at")
            desk.sync(bad, now=T)
            self.assertEqual(desk.state(T)["portfolio"]["filled_orders"], 0)
        desk.sync(q, now=T)
        self.assertEqual(desk.state(T)["positions"][0]["side"], "no")
        at = START + timedelta(minutes=5)
        desk.sync({}, [settled(key, at, result="NO")], now=at)
        self.assertEqual(desk.state(at)["performance"]["auto_only"]["profitable_windows"], 1)

    def test_eastern_daily_rollover_missing_midnight_and_separate_products(self):
        key = "polymarket_us_hourly"
        opened = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)  # Jan 5, 11pm Eastern.
        now = opened + timedelta(minutes=59, seconds=50)
        desk = broker.BTCBroker(self.dir, key)
        desk.place(order(key, opened=opened), feed(key, opened=opened, now=now), now)
        midnight = opened + timedelta(hours=1)
        self.assertEqual(desk.state(now)["daily"]["date"], "2026-01-05")
        desk.sync({}, now=midnight)
        new = desk.state(midnight)
        self.assertEqual(new["daily"]["date"], "2026-01-06")
        self.assertIsNone(new["daily"]["opening_equity_cents"])
        prev = new["daily_history"][0]
        self.assertIsNone(prev["equity_cents"])
        self.assertIsNone(prev["net_gain_cents"])
        self.assertIsNotNone(prev["last_known_equity_cents"])
        self.assertEqual(prev["last_known_at"], weather.iso(now))
        self.assertEqual(prev["filled_orders"], 1)
        desk.sync({}, [settled(key, midnight, opened=opened, result="NO")], now=midnight)
        self.assertEqual(desk.state(midnight)["daily"]["realized_cents"], -107)
        self.assertEqual(desk.state(midnight)["daily"]["filled_orders"], 0)
        # Unknown opening remains honest. The independent risk upper bound permits new
        # exposure once prior risk resolves, without inventing midnight performance.
        day2 = midnight + timedelta(minutes=2)
        nxt = desk.place(order(key, opened=midnight, cid="day2"), feed(key, opened=midnight, now=day2), day2)
        self.assertEqual(nxt["status"], "filled")
        self.assertIsNone(desk.state(day2)["daily"]["net_gain_cents"])
        self.assertEqual(self.paper.state(midnight)["daily"]["net_gain_cents"], 0)
        json.dumps(desk.state(day2), allow_nan=False)

    def test_failed_commit_rolls_back_depth_positions_fills_and_cash(self):
        q = feed(no=((49, 2),))
        with closing(sqlite3.connect(self.paper.path)) as db, db:
            db.execute(
                """CREATE TRIGGER fail_receipt BEFORE INSERT ON orders BEGIN
                       SELECT RAISE(ABORT,'injected receipt failure'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected receipt failure"):
            self.paper.place(order(), q, T)
        original = broker.BTCBroker(self.dir).state(T)
        self.assertEqual(original["portfolio"]["cash_cents"], 1_000_000)
        self.assertEqual(original["positions"], [])
        self.assertEqual(original["fills"], [])
        with closing(sqlite3.connect(self.paper.path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM used").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_receipt")
        self.assertEqual(self.paper.place(order(), q, T)["status"], "filled")

    def test_peak_drawdown_skips_missing_marks_and_survives_recovery(self):
        self.paper.place(order(), feed(), T)
        at = T + timedelta(seconds=10)
        self.paper.sync(feed(now=at, yes=((80, 20),), no=((18, 30),)), now=at)
        peak = self.paper.state(at)["portfolio"]["equity_cents"]
        self.assertGreater(peak, 1_000_000)
        at += timedelta(seconds=10)
        self.paper.sync(feed(now=at, yes=((20, 20),), no=((78, 30),)), now=at)
        trough = self.paper.state(at)["portfolio"]["equity_cents"]
        expected = peak - trough
        self.assertEqual(self.paper.state(at)["performance"]["max_drawdown_cents"], expected)
        self.paper.sync({}, now=at + timedelta(seconds=30))
        self.assertIsNone(self.paper.state(at + timedelta(seconds=30))["portfolio"]["equity_cents"])
        final = START + timedelta(minutes=15)
        self.paper.sync({}, [settled("kalshi", final)], now=final)
        self.assertEqual(
            broker.BTCBroker(self.dir).state(final)["performance"]["max_drawdown_cents"], expected
        )
        self.assertGreater(self.paper.state(final)["performance"]["peak_equity_cents"], peak)

    def test_clock_rollback_and_cash_limits_do_not_backdate_trades(self):
        self.paper.sync(feed(), now=T)
        old = T - timedelta(days=1)
        self.paper.sync({}, now=old)
        back = self.paper.place(order(cid="back"), feed(), old)
        self.assertEqual(back["status"], "rejected")
        self.assertIn("clock", back["reason"])
        self.assertEqual(self.paper.state(T)["daily_history"], [])
        with closing(sqlite3.connect(self.paper.path)) as db, db:
            db.execute("UPDATE account SET cash=25")
        funds = self.paper.place(order(cid="funds"), feed(), T)
        self.assertEqual(funds["status"], "rejected")
        self.assertIn("cash", funds["reason"])

    def test_flat_completed_day_keeps_eastern_close_and_realized_after_restart(self):
        self.paper.place(order(), feed(), T)
        close = START + timedelta(minutes=15)
        self.paper.sync({}, [settled("kalshi", close)], now=close)
        day2 = datetime(2026, 1, 6, 5, 0, tzinfo=UTC)
        self.paper.sync({}, now=day2)
        state = broker.BTCBroker(self.dir).state(day2)
        self.assertEqual(state["daily"]["opening_equity_cents"], 1_000_094)
        self.assertEqual(state["daily"]["net_gain_cents"], 0)
        prev = state["daily_history"][0]
        self.assertEqual((prev["net_gain_cents"], prev["fees_cents"], prev["filled_orders"]), (94, 4, 1))
        self.assertEqual(prev["as_of"], weather.iso(close))


if __name__ == "__main__":
    unittest.main()
