import json
import threading
import unittest
from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from worker import bitcoin_contracts as btc
from worker import feeds, weather

OPENED = weather.instant("2026-11-26T21:00:00Z")
NOW = OPENED + timedelta(minutes=3)


def market(opened=OPENED):
    closed = opened + timedelta(minutes=15)
    local = weather.eastern(closed)
    event = "KXBTC15M-" + local.strftime("%y%b%d%H%M").upper()

    def clock(t, month):
        e = weather.eastern(t)
        zone = "EDT" if e.utcoffset() == timedelta(hours=-4) else "EST"
        return f"{e.strftime('%I:%M %p').lstrip('0')} {zone} on {e.strftime(month)} {e.day}, {e.year}"

    return {
        "ticker": event + "-" + local.strftime("%M"),
        "event_ticker": event,
        "open_time": weather.iso(opened),
        "close_time": weather.iso(closed),
        "updated_time": weather.iso(opened),
        "title": "Synthetic BTC 15-minute market",
        "market_type": "binary",
        "status": "active",
        "result": "",
        "notional_value_dollars": "1.0000",
        "floor_strike": 80000.45,
        "strike_type": "greater_or_equal",
        "yes_sub_title": "Target Price: $80,000.45",
        "no_sub_title": "Target price: TBD",
        "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' BRTI before "
        + clock(closed, "%b")
        + " is at least the simple average of the sixty seconds of CF Benchmarks' BRTI before "
        + clock(opened, "%B")
        + ", then the market resolves to Yes.",
        "rules_secondary": "Synthetic secondary rules.",
        "yes_bid_dollars": "0.1000",
        "yes_ask_dollars": "0.9000",
    }


def book():
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.0610", "201.34"], ["0.0840", "7.28"]],
            "no_dollars": [["0.8500", "3.00"], ["0.8600", "2.17"]],
        }
    }


class BitcoinContractsTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.t = 1000
        self.mode, self.generation = "enabled", 1
        self.catalog = {"markets": [market()], "cursor": ""}
        self.book = book()
        self.calls = []
        self.errors = {}
        self.ages = {}
        self.origins = {}
        self.hook = None
        self.lock = threading.RLock()
        self.feed = btc.BitcoinContracts(
            lambda: (self.mode, self.generation),
            self.lock,
            fetch=self.fetch,
            now=lambda: self.now,
            monotonic=lambda: self.t,
        )

    def fetch(self, url, *, max_bytes, permission_guard):
        self.assertGreater(max_bytes, 0)
        permission_guard()
        self.calls.append(url)
        kind = "catalog" if url == btc.CATALOG else "book"
        if self.hook:
            self.hook(kind)
        if kind in self.errors:
            raise self.errors[kind]
        payload = self.catalog if kind == "catalog" else self.book
        return feeds.Reply(
            json.dumps(payload).encode(),
            self.now,
            self.now - timedelta(seconds=self.origins.get(kind, 0)),
            None,
            self.ages.get(kind, 0),
        )

    def advance(self, seconds):
        self.t += seconds
        self.now += timedelta(seconds=seconds)

    def poll(self):
        self.assertTrue(self.feed.poll())
        return self.feed.state()

    def test_current_window_depth_complements_exact_precision_and_timestamps(self):
        self.catalog["markets"] += [
            market(OPENED - btc.WINDOW),
            market(OPENED + btc.WINDOW),
            {**market(), "ticker": "KXETH15M-26NOV261615-15"},
            {**market(), "event_ticker": "KXETH15M-26NOV261615"},
            {**market(), "status": "inactive"},
        ]
        self.origins["book"] = 1
        s = self.poll()
        self.assertEqual(s["status"], "live")
        row = s["current"]
        self.assertEqual(self.calls, [btc.CATALOG, btc.book_url(market()["ticker"])])
        self.assertEqual(row["target_usd"], "80000.45")
        self.assertEqual(row["comparison"], "greater_or_equal")
        self.assertEqual((row["yes_bid_cents"], row["yes_ask_cents"]), (8.4, 14))
        self.assertEqual((row["no_bid_cents"], row["no_ask_cents"]), (86, 91.6))
        self.assertEqual(
            (
                row["yes_bid_size"],
                row["yes_ask_size"],
                row["no_bid_size"],
                row["no_ask_size"],
            ),
            (7.28, 2.17, 2.17, 7.28),
        )
        self.assertEqual(row["depth"]["no"]["asks"], [[91.6, 7.28], [93.9, 201.34]])
        point = row["history"][0]
        self.assertEqual(
            point,
            {
                "at": weather.iso(NOW),
                "http_date": weather.iso(NOW - timedelta(seconds=1)),
                "kind": "book",
                "yes_bid_cents": 8.4,
                "yes_ask_cents": 14,
                "no_bid_cents": 86,
                "no_ask_cents": 91.6,
            },
        )
        self.assertEqual(row["catalog_http_date"], weather.iso(NOW))
        self.assertEqual(row["observed_at"], weather.iso(NOW))
        self.assertEqual(s["last_success_at"], row["observed_at"])
        self.assertFalse(s["recent"])
        self.assertFalse(self.feed.poll())
        # Consumers cannot mutate captured levels or history via state().
        row["history"][0]["yes_bid_cents"] = False
        row["depth"]["yes"]["bids"][0].clear()
        self.assertEqual(
            self.feed.state()["current"]["depth"]["yes"]["bids"][0], [8.4, 7.28]
        )

    def test_rollover_at_exact_close_and_no_synthetic_history(self):
        first = self.poll()["current"]
        self.advance(12 * 60)
        at_close = self.feed.state()
        self.assertIsNone(at_close["current"])
        self.assertEqual(at_close["recent"][0]["id"], first["id"])
        self.assertIsNone(at_close["recent"][0]["yes_ask_cents"])
        self.catalog["markets"] = [
            market(),
            market(OPENED + btc.WINDOW),
            market(OPENED + 2 * btc.WINDOW),
        ]
        second = self.poll()
        self.assertEqual(second["current"]["open_at"], first["close_at"])
        self.assertEqual(len(second["current"]["history"]), 1)
        self.assertNotEqual(second["current"]["id"], first["id"])
        self.assertEqual(second["recent"][0]["history"], first["history"])
        self.advance(10)
        self.catalog["markets"] = [market(OPENED + 2 * btc.WINDOW)]
        state = self.poll()
        self.assertIsNone(state["current"])
        self.assertEqual(state["status"], "empty")
        self.assertEqual(self.calls[-1], btc.CATALOG)
        self.advance(3601)
        self.assertEqual(
            [x["id"] for x in self.feed.state()["recent"]], [second["current"]["id"]]
        )
        self.advance(15 * 60)
        self.assertFalse(self.feed.state()["recent"])

    def test_pending_target_empty_side_and_later_published_target(self):
        self.catalog["markets"][0].update(
            floor_strike=None, strike_type=None, yes_sub_title="Target price: TBD"
        )
        self.book["orderbook_fp"]["no_dollars"] = []
        row = self.poll()["current"]
        self.assertEqual(self.feed.state()["status"], "partial")
        self.assertIsNone(row["target_usd"])
        self.assertIsNone(row["no_bid_cents"])
        self.assertIsNone(row["yes_ask_size"])
        self.assertEqual(row["yes_bid_cents"], 8.4)
        self.advance(10)
        self.catalog["markets"] = [market()]
        self.book = book()
        self.assertEqual(len(self.poll()["current"]["history"]), 2)
        self.advance(10)
        self.catalog["markets"][0].update(
            floor_strike=78000, yes_sub_title="Target Price: $78,000.00"
        )
        self.assertEqual(len(self.poll()["current"]["history"]), 1)
        self.advance(10)
        self.book["orderbook_fp"] = {"yes_dollars": [], "no_dollars": []}
        s = self.poll()
        self.assertEqual(s["status"], "partial")
        self.assertIsNone(s["current"]["yes_ask_cents"])
        self.assertEqual(s["current"]["history"][-1]["kind"], "book")

    def test_stale_reply_failed_gap_and_throttle(self):
        self.poll()
        self.advance(21)
        row = self.feed.state()["current"]
        self.assertEqual(self.feed.state()["status"], "stale")
        self.assertIsNone(row["yes_bid_cents"])
        self.assertEqual(row["history"][0]["yes_bid_cents"], 8.4)
        self.ages["book"] = 7
        s = self.poll()
        self.assertEqual(s["status"], "error")
        self.assertEqual(s["last_success_at"], weather.iso(NOW))
        self.assertEqual(s["current"]["observed_at"], weather.iso(NOW))
        self.assertEqual(s["current"]["history"][-1]["kind"], "gap")
        self.assertIsNone(s["current"]["history"][-1]["http_date"])
        self.ages.clear()
        self.advance(10)
        self.errors["catalog"] = feeds.Unavailable("fixture", 429, 62)
        s = self.poll()
        self.assertEqual(s["status"], "rate_limited")
        self.assertEqual(self.calls[-1], btc.CATALOG)
        self.mode = "disabled"
        self.feed.permission_changed()
        self.mode = "enabled"
        self.generation += 1
        self.feed.permission_changed()
        self.assertEqual(self.feed.state()["status"], "rate_limited")
        self.assertIsNone(self.feed.state()["current"])
        self.errors.clear()
        self.advance(61)
        self.assertFalse(self.feed.poll())
        self.advance(1)
        self.assertEqual(self.poll()["status"], "live")

    def test_revocation_during_every_io_phase_discards_response_and_hides_history(self):
        for stage in ("catalog", "book"):
            with self.subTest(stage=stage):
                self.setUp()
                self.poll()
                self.advance(10)
                entered, resume = threading.Event(), threading.Event()

                def block(kind, target=stage, started=entered, release=resume):
                    if kind == target:
                        started.set()
                        release.wait(2)

                self.hook = block
                worker = threading.Thread(target=self.feed.poll)
                worker.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.feed.poll())
                self.mode = "disabled"
                self.feed.permission_changed()
                self.assertIsNone(self.feed.state()["current"])
                self.mode = "enabled"
                self.generation += 1
                self.feed.permission_changed()
                resume.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                after = self.feed.state()
                self.assertEqual(after["status"], "waiting")
                self.assertIsNone(after["last_success_at"])
                self.assertFalse(after["recent"])
                self.assertIsNone(after["current"])
                self.assertEqual(len(self.calls), 3 if stage == "catalog" else 4)
                self.mode = "offline"
                self.feed.permission_changed()
                self.advance(60)
                self.assertFalse(self.feed.poll())
                self.assertEqual(self.feed.state()["status"], "offline")

    def test_no_public_reads_with_permission_off_or_offline(self):
        for mode in ("offline", "disabled"):
            self.mode = mode
            self.feed.permission_changed()
            self.assertTrue(self.feed.wake.is_set())
            self.assertFalse(self.feed.poll())
            self.assertEqual(self.feed.state()["status"], mode)
        self.assertEqual(self.calls, [])

    def test_unsupported_metadata_never_triggers_dynamic_get(self):
        for change in (
            {"strike_type": "greater"},
            {"floor_strike": 80100.0},
            {
                "rules_primary": market()["rules_primary"].replace(
                    "is at least", "is above"
                )
            },
            {"rules_primary": market()["rules_primary"].replace("4:15 PM", "4:00 PM")},
            {"ticker": "KXBTC15M-26NOV261615-45"},
            {"market_type": "scalar"},
            {"updated_time": weather.iso(NOW + timedelta(hours=1))},
            {"open_time": weather.iso(OPENED - timedelta(seconds=1))},
            {"notional_value_dollars": "5.0000"},
        ):
            with self.subTest(change=change):
                self.setUp()
                self.catalog["markets"] = [{**market(), **change}]
                self.assertEqual(self.poll()["status"], "error")
                self.assertEqual(self.calls, [btc.CATALOG])
        self.setUp()
        self.catalog["markets"] = [market(), market()]
        self.assertEqual(self.poll()["status"], "error")
        self.assertEqual(self.calls, [btc.CATALOG])

    def test_midnight_and_summer_rules_and_scope_guard(self):
        for close in ("2026-07-15T04:00:00Z", "2026-11-28T05:00:00Z"):
            opened = weather.instant(close) - btc.WINDOW
            self.now = opened + timedelta(minutes=6)
            self.t += 60
            self.catalog["markets"] = [market(opened)]
            self.assertEqual(
                self.poll()["current"]["close_at"], weather.iso(weather.instant(close))
            )
        with self.assertRaises(ValueError):
            btc.book_url("KXBTC15M-26NOV261615-15/../../account")
        for u in (
            feeds.API + "/portfolio/orders",
            btc.CATALOG.replace("status=open", "status=settled"),
            "https://example.com/trade-api/v2/markets",
            btc.CATALOG + "&other=1",
        ):
            with self.assertRaises(ValueError):
                btc.download(u, max_bytes=20, permission_guard=lambda: None)

    def test_crossed_invalid_or_oversized_book_rejected(self):
        for entries in (
            [["0.2000", "1.01"]],
            [["0.00100", "1"]],
            [["NaN", "1"]],
            [["0.0700", "-2"]],
            [["0.0400", "1.123"]],
            [["0.0000", "1"]],
            [["0.0100", "1"], ["0.01", "3"]],
            [["0.0001", "1"]] * 6,
        ):
            b = book()
            b["orderbook_fp"]["yes_dollars"] = entries
            with self.assertRaises(ValueError):
                btc.parse_book(b)
        b = book()
        b["orderbook_fp"]["yes_dollars"] = [["0.0100", "0.00"], ["0.0150", "0.05"]]
        self.assertEqual(btc.parse_book(b)["yes"]["bids"], [[1.5, 0.05]])

    def test_market_closes_while_book_request_in_flight(self):
        self.now = OPENED + btc.WINDOW - timedelta(seconds=1)
        self.hook = lambda kind: self.advance(2) if kind == "book" else None
        s = self.poll()
        self.assertIsNone(s["current"])
        self.assertFalse(s["recent"][0]["history"])
        self.assertIsNone(s["last_success_at"])

    def test_transport_rejects_same_host_redirect_and_honors_http_retry(self):
        body = BytesIO(b"fixture")
        err = HTTPError(btc.CATALOG, 429, "test", {"Retry-After": "121"}, body)
        client = Mock()
        client.open.side_effect = err
        guard = Mock()
        with (
            patch.object(btc, "build_opener", return_value=client),
            self.assertRaises(feeds.Unavailable) as raised,
        ):
            btc.download(btc.CATALOG, max_bytes=100, permission_guard=guard)
        self.assertEqual((raised.exception.code, raised.exception.retry), (429, 121))
        self.assertTrue(body.closed)
        guard.assert_called_once()
        self.assertEqual(client.open.call_args.args[0].get_method(), "GET")
        redirect_body = BytesIO(b"redirect")
        with self.assertRaises(feeds.Unavailable):
            btc.NoRedirects().redirect_request(
                None,
                redirect_body,
                302,
                "redirect",
                {},
                feeds.API + "/portfolio/orders",
            )
        self.assertTrue(redirect_body.closed)
