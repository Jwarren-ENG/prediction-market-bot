"""Private synthetic BTC prices exercise the same worker used by the desk."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from worker import bitcoin as b


NOW = datetime(2026, 11, 26, 21, 10, tzinfo=timezone.utc)


def bar(t, close):
    return dict(
        timestamp=str(t),
        low=str(close - 2),
        high=str(close + 2),
        open=str(close),
        close=str(close),
        volume="2",
    )


class Fake:
    def __init__(self):
        self.now = NOW
        self.bid, self.ask = "10000", "10001"
        self.bid_size = self.ask_size = "10.0"
        self.age = 0
        self.override = {}
        self.calls = []
        self.on_read = None
        self.unavailable = {}
        self.missing = None

    def daily(self):
        end = int(self.now.timestamp()) // b.DAY * b.DAY
        start = int(NOW.timestamp()) // b.DAY * b.DAY - 8 * b.DAY
        return [
            bar(t, self.override.get(t, 10000 + (t - start) // b.DAY))
            for t in range(end - 8 * b.DAY, end + b.DAY, b.DAY)
            if t != self.missing
        ]

    def read(self, url):
        self.calls.append(url)
        if self.on_read:
            self.on_read(url)
        ms = int((self.now - timedelta(seconds=self.age)).timestamp() * 1_000_000)
        if url == b.BOOK:
            data = dict(
                timestamp=str(ms // 1_000_000),
                microtimestamp=str(ms),
                bids=[[self.bid, self.bid_size]],
                asks=[[self.ask, self.ask_size]],
            )
        else:
            assert url.startswith("https://www.bitstamp.net/api/v2/ohlc/btcusd/?")
            params = parse_qs(urlsplit(url).query)
            period = int(params["step"][0])
            assert params["exclude_current_candle"] == ["true"]
            if period in self.unavailable:
                raise self.unavailable[period]
            end = int(self.now.timestamp()) // period * period
            raw = (
                self.daily()
                if period == b.DAY
                else [
                    bar(t, 10000)
                    for t in range(end - 168 * period, end + period, period)
                ]
            )
            data = dict(data=dict(pair="BTC/USD", ohlc=raw))
        return data, self.now, self.now

    def step(self, sec=31):
        self.now += timedelta(seconds=sec)


class BitcoinPaperTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.src = Fake()
        for p in [
            patch.object(b, "clock", side_effect=lambda: self.src.now),
            patch.object(b, "public_read", side_effect=self.src.read),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.paper = b.BitcoinPaper(self.root)

    def activate(self):
        self.paper.configure(True)
        self.paper.set_auto(True)

    def test_manual_partial_sales_conserve_basis_pause_auto_and_retry_after_restart(
        self,
    ):
        self.paper.configure(True)
        self.paper.poll_once()
        self.paper.set_auto(True)
        order = dict(
            client_id=str(uuid4()), side="buy", budget_cents=10000, limit_usd="10100"
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.paper.manual_order, [order, order]))
        buy = receipts[0]
        self.assertEqual(receipts[1], buy)
        self.assertEqual(self.paper.manual_order(dict(order, limit_usd="10100.0")), buy)
        with self.assertRaisesRegex(ValueError, "different terms"):
            self.paper.manual_order(dict(order, budget_cents=19000))
        initial = self.paper.state()
        self.assertTrue(self.paper.managed())
        self.assertFalse(initial["auto_enabled"])
        self.assertEqual(initial["manual_trades"], 1)
        self.assertEqual(initial["benchmark"]["filled_orders"], 0)
        self.assertEqual(
            initial["portfolio"]["cash_cents"]
            + initial["portfolio"]["open_cost_cents"],
            b.START_CENTS,
        )
        qty = Decimal(buy["btc"])
        sale = dict(
            client_id=str(uuid4()), side="sell", quantity_btc="0.001", limit_usd="9900"
        )
        partial = self.paper.manual_order(sale)
        after = self.paper.state()["portfolio"]
        self.assertEqual(Decimal(after["btc"]), qty - Decimal("0.001"))
        self.assertGreater(after["open_cost_cents"], 0)
        basis = initial["portfolio"]["open_cost_cents"] - after["open_cost_cents"]
        self.assertEqual(after["realized_cents"], partial["cash_amount_cents"] - basis)
        self.paper.manual_order(dict(sale, client_id=str(uuid4()), quantity_btc="all"))
        closed = self.paper.state()["portfolio"]
        self.assertEqual(closed["open_cost_cents"], 0)
        self.assertEqual(closed["cash_cents"] - b.START_CENTS, closed["realized_cents"])
        self.src.step(500)
        self.assertEqual(
            b.BitcoinPaper(self.root, online=False).manual_order(order), buy
        )

    def test_manual_stale_limits_depth_reuse_and_no_automatic_retry_of_rejection(self):
        self.paper.configure(True)
        self.src.ask_size = "0.00010000"
        self.paper.poll_once()
        order = dict(
            client_id=str(uuid4()), side="buy", budget_cents=100, limit_usd="9000"
        )
        with self.assertRaisesRegex(ValueError, "outside your limit"):
            self.paper.manual_order(order)
        ok = self.paper.manual_order(
            dict(order, limit_usd="11000", client_id=str(uuid4()))
        )
        self.assertGreater(Decimal(ok["btc"]), 0)
        with self.assertRaisesRegex(ValueError, "depth"):
            self.paper.manual_order(
                dict(order, limit_usd="11000", client_id=str(uuid4()))
            )
        self.src.step()
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.paper.manual_order(dict(order, client_id=str(uuid4())))
        self.src.ask, self.src.bid = "8900", "8880"
        self.paper.poll_once()
        with self.assertRaisesRegex(ValueError, "outside your limit"):
            self.paper.manual_order(order)
        self.paper.configure(False)
        self.paper.configure(True)
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.paper.manual_order(dict(order, client_id=str(uuid4())))
        self.assertEqual(self.paper.state()["manual_trades"], 1)

    def test_manual_spot_exposure_and_oversell(self):
        self.paper.configure(True)
        self.paper.poll_once()
        order = dict(
            client_id=str(uuid4()), side="buy", budget_cents=49000, limit_usd="11000"
        )
        self.paper.manual_order(order)
        with self.assertRaisesRegex(ValueError, "open-cost"):
            self.paper.manual_order(
                dict(order, client_id=str(uuid4()), budget_cents=2000)
            )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.paper.manual_order(
                dict(
                    client_id=str(uuid4()),
                    side="sell",
                    quantity_btc="2",
                    limit_usd="5000",
                )
            )
        for invalid in (True, "-1", "0.000000001"):
            with self.assertRaises(ValueError):
                self.paper.manual_order(
                    dict(order, client_id=str(uuid4()), limit_usd=invalid)
                )

    def test_fixed_rule_uses_eight_contiguous_completed_utc_days(self):
        bars = self.src.daily()
        latest_complete = bars[-2]
        self.assertEqual(b.daily_signal(bars, NOW)["target"], "long")
        bars[-1] = bar(int(bars[-1]["timestamp"]), 10)
        self.assertEqual(b.daily_signal(bars[::-1], NOW)["target"], "long")
        equal = [*bars[:-2], bar(int(latest_complete["timestamp"]), 10000), bars[-1]]
        self.assertEqual(b.daily_signal(equal, NOW)["target"], "cash")
        self.assertIsNone(b.daily_signal(equal[1:], NOW)["target"])
        zero_volume = [*bars[:-2], {**latest_complete, "volume": "0.0"}, bars[-1]]
        self.assertIsNone(b.daily_signal(zero_volume, NOW)["target"])
        self.assertIsNone(
            b.daily_signal([*bars, bar(int(bars[0]["timestamp"]), 3)], NOW)["target"]
        )
        self.assertIsNone(b.daily_signal(bars, NOW + timedelta(days=2))["target"])
        with self.assertRaises(ValueError):
            b.daily_signal(bars, NOW.replace(tzinfo=None))

    def test_default_private_data_off_no_network_on_state_offline_or_disabled(self):
        self.assertFalse(self.paper.state()["enabled"])
        self.assertFalse(self.paper.state()["auto_enabled"])
        self.assertIsNone(self.paper.state()["quote"])
        self.assertEqual(self.paper.state()["portfolio"]["cash_cents"], 1_000_000)
        self.paper.poll_once()
        offline = b.BitcoinPaper(self.root, online=False)
        with self.assertRaises(ValueError):
            offline.set_auto(True)
        offline.configure(True)
        offline.poll_once()
        self.assertEqual(self.src.calls, [])
        self.paper.poll_once()
        state = self.paper.state()
        self.assertEqual(len(state["hourly"]), 168)
        self.assertEqual(
            b.moment(state["hourly"][-1]["at"]), NOW.replace(minute=0, second=0)
        )
        self.assertIsNone(state["started_at"])
        self.assertFalse(state["activity"])
        self.assertTrue(state["quote"]["fresh"])

    def test_entry_costs_eight_digit_quantity_benchmark_and_idempotent_restart(self):
        self.activate()
        self.paper.poll_once()
        state = self.paper.state()
        strategy = state["portfolio"]
        self.assertEqual(strategy, state["benchmark"])
        self.assertEqual(strategy["filled_orders"], 1)
        self.assertEqual(len(state["activity"]), 2)
        self.assertEqual(state["started_at"], b.stamp(NOW))
        self.assertGreaterEqual(strategy["cash_cents"], 950_000)
        self.assertEqual(
            strategy["open_cost_cents"], 1_000_000 - strategy["cash_cents"]
        )
        self.assertLess(strategy["equity_cents"], 1_000_000)
        self.assertEqual(state["history"][0]["equity_cents"], 1_000_000)
        self.assertEqual(state["history"][-1]["equity_cents"], strategy["equity_cents"])
        qty = Decimal(strategy["btc"])
        notional = qty * Decimal(self.src.ask) * Decimal("1.0005") * 100
        fee = int(
            (notional * Decimal("0.006")).to_integral_value(rounding=ROUND_CEILING)
        )
        self.assertEqual(state["activity"][0]["fee_cents"], fee)
        self.assertEqual(
            strategy["open_cost_cents"],
            int(notional.to_integral_value(rounding=ROUND_CEILING)) + fee,
        )
        exit_value = qty * Decimal(self.src.bid) * Decimal("0.9995") * 100
        expected_net = int(exit_value.to_integral_value(rounding=ROUND_FLOOR)) - int(
            (exit_value * Decimal("0.006")).to_integral_value(rounding=ROUND_CEILING)
        )
        self.assertEqual(
            strategy["equity_cents"], strategy["cash_cents"] + expected_net
        )
        self.assertEqual(strategy["realized_cents"], 0)
        restart = b.BitcoinPaper(self.root)
        restart.poll_once()
        self.assertEqual(self.src.calls.count(b.BOOK), 1)
        self.src.step()
        restart.poll_once()
        self.assertEqual(restart.state()["activity"], state["activity"])
        self.assertEqual(sum("/ohlc/" in u for u in self.src.calls), 2)

    def test_ask_depth_start_and_bid_depth_exit_retry_without_partial_fills(self):
        self.activate()
        self.src.ask_size = "0.00001"
        self.paper.poll_once()
        self.assertIsNone(self.paper.state()["started_at"])
        self.assertFalse(self.paper.state()["activity"])
        self.src.step()
        self.src.ask_size = "1"
        self.paper.poll_once()
        first = self.paper.state()
        self.assertEqual(first["portfolio"]["filled_orders"], 1)
        self.src.step(86400)
        yesterday = int(self.src.now.timestamp()) // b.DAY * b.DAY - b.DAY
        self.src.override[yesterday] = 9300
        self.src.bid_size = "0.000001"
        self.paper.poll_once()
        thin = self.paper.state()
        self.assertEqual(thin["signal"]["target"], "cash")
        self.assertEqual(thin["portfolio"]["filled_orders"], 1)
        self.assertFalse(thin["portfolio"]["mark_fresh"])
        self.assertIn("bid size", thin["status"])
        self.src.step()
        self.src.bid = "10500"
        self.src.ask = "10501"
        self.src.bid_size = "10"
        self.paper.poll_once()
        sold = self.paper.state()
        self.assertEqual(sold["portfolio"]["filled_orders"], 2)
        self.assertEqual(sold["portfolio"]["btc"], "0")
        self.assertEqual(sold["benchmark"]["filled_orders"], 1)
        self.assertEqual(
            sold["portfolio"]["realized_cents"],
            sold["portfolio"]["equity_cents"] - 1_000_000,
        )
        self.assertEqual(sold["activity"][0]["side"], "sell")
        self.src.step()
        self.paper.poll_once()
        self.assertEqual(self.paper.state()["activity"], sold["activity"])

    def test_cash_signal_starts_hold_control_and_missing_bid_mark_stays_unknown(self):
        self.activate()
        yesterday = int(NOW.timestamp()) // b.DAY * b.DAY - b.DAY
        self.src.override[yesterday] = 9000
        self.src.bid_size = "0.000001"
        self.paper.poll_once()
        state = self.paper.state()
        self.assertEqual(state["portfolio"]["filled_orders"], 0)
        self.assertEqual(state["benchmark"]["filled_orders"], 1)
        self.assertIsNone(state["benchmark"]["equity_cents"])
        self.assertFalse(state["benchmark"]["mark_fresh"])
        self.assertEqual(len(state["history"]), 1)
        self.src.step()
        self.src.bid_size = "10"
        self.paper.poll_once()
        state = self.paper.state()
        self.assertTrue(state["benchmark"]["mark_fresh"])
        self.assertEqual(len(state["history"]), 2)

    def test_revocation_and_pause_discard_an_inflight_response_and_batch(self):
        self.activate()
        self.src.on_read = lambda url: (
            self.paper.configure(False) if "/ohlc/" in url else None
        )
        self.paper.poll_once()
        state = self.paper.state()
        self.assertEqual(len(self.src.calls), 1)
        self.assertEqual(state["hourly"], [])
        self.assertIsNone(state["last_checked_at"])
        self.assertFalse(state["auto_enabled"])
        self.activate()
        self.src.on_read = lambda url: (
            self.paper.set_auto(False) if url == b.BOOK else None
        )
        self.paper.poll_once()
        self.assertFalse(self.paper.state()["activity"])
        self.assertIsNone(self.paper.state()["quote"])
        self.src.on_read = None
        self.paper.poll_once()
        self.assertTrue(self.paper.state()["quote"]["fresh"])
        self.assertFalse(self.paper.state()["auto_enabled"])

    def test_stale_crossed_books_and_missing_candles_do_not_fill(self):
        self.activate()
        self.src.age = 45
        self.paper.poll_once()
        self.assertIsNone(self.paper.state()["quote"])
        self.assertFalse(self.paper.state()["activity"])
        self.src.step(65)
        self.src.age = 0
        self.src.bid = "10501"
        self.paper.poll_once()
        self.assertFalse(self.paper.state()["activity"])
        self.src.step(120)
        self.src.bid = "10000"
        self.paper.poll_once()
        self.assertTrue(self.paper.state()["portfolio"]["filled_orders"])
        self.src.step(61)
        self.assertFalse(self.paper.state()["quote"]["fresh"])
        self.assertFalse(self.paper.state()["portfolio"]["mark_fresh"])
        self.src.step(86400)
        self.src.missing = int(self.src.now.timestamp()) // b.DAY * b.DAY - b.DAY
        self.paper.poll_once()
        self.assertIsNone(self.paper.state()["signal"]["target"])
        self.assertEqual(self.paper.state()["portfolio"]["filled_orders"], 1)

    def test_host_rate_limit_survives_restart_and_permission_toggle(self):
        self.activate()
        self.src.unavailable[b.DAY] = b.FeedError("HTTP 429", 90, throttled=True)
        self.assertEqual(self.paper.poll_once(), 90)
        self.assertEqual(len(self.src.calls), 1)
        self.assertIn("rate limit", self.paper.state()["status"])
        self.paper.configure(False)
        restart = b.BitcoinPaper(self.root)
        restart.configure(True)
        self.src.step(40)
        restart.poll_once()
        self.assertEqual(len(self.src.calls), 1)
        self.src.step(51)
        restart.poll_once()
        self.assertIn(b.BOOK, self.src.calls)
        self.assertIsNone(restart.state()["signal"]["target"])
        self.assertFalse(restart.state()["activity"])

    def test_book_microseconds_and_complete_cash_marks(self):
        data = {
            "timestamp": "1795727400",
            "microtimestamp": "1795727400123456",
            "bids": [["150.05", "2.5"], ["150", "20"]],
            "asks": [["150.09", "3.25"], ["151", "20"]],
        }
        seen = b.utc_at(1795727401)
        quote = b.parse_book(data, seen)
        self.assertEqual(quote["source_at"], "2026-11-26T21:10:00.123456Z")
        self.assertEqual(quote["bid_size_btc"], "2.5")
        for invalid in [
            {"microtimestamp": "0"},
            {"timestamp": "1795727450"},
            {"bids": [["151", "1"]]},
            {"asks": []},
            {"asks": [["150.09", "0"]]},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                b.parse_book({**data, **invalid}, seen)
