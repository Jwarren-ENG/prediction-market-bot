import json
import threading
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from worker import candles, feeds, quotes, weather
from worker.server import Desk

NOW = weather.instant("2026-11-26T19:00:00Z")


def market(day=date(2026, 11, 27), floor=44):
    event = "KXHIGHNY-" + day.strftime("%y%b%d").upper()
    name = f"{day.strftime('%B')} {day.day}, {day.year}"
    return {
        "ticker": f"{event}-B{floor}.5",
        "event_ticker": event,
        "market_type": "binary",
        "notional_value_dollars": "1.0000",
        "rules_primary": "Synthetic test values for the maximum temperature recorded at New York City (CLINYC).",
        "early_close_condition": f"The Last Trading Time will be 11:59 PM local time on {name} regardless "
        "of any data releases or events occurring. Expiration will occur on the sooner of the first "
        f"7:00 or 8:00 AM ET following the release of the data for {name}, or one week after {name}.",
        "strike_type": "between",
        "floor_strike": floor,
        "cap_strike": floor + 1,
        "status": "active",
        "open_time": weather.iso(weather.midnight(day) - timedelta(days=1)),
        "close_time": weather.iso(weather.event_end(day) + timedelta(hours=1)),
        "yes_bid_dollars": "0.3175",
        "yes_ask_dollars": "0.4000",
        "yes_bid_size_fp": "5.00",
        "yes_ask_size_fp": "0.23",
    }


class PublicQuotesTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.monotonic = 10_000.0
        self.mode = "enabled"
        self.generation = 4
        self.rows = [market()]
        self.cursor = ""
        self.age = 0
        self.shift = 0
        self.problem = None
        self.block = False
        self.calls = 0
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.lock = threading.RLock()
        self.tape = quotes.QuoteTape(
            lambda: (self.mode, self.generation),
            self.lock,
            fetch=self.fetch,
            now=lambda: self.now,
            monotonic=lambda: self.monotonic,
        )

    def fetch(self, url, **options):
        self.assertEqual(url, feeds.OPEN)
        self.assertIn("max_bytes", options)
        options["permission_guard"]()
        self.calls += 1
        self.entered.set()
        if self.block and not self.resume.wait(2):
            raise RuntimeError("Test quote source timed out.")
        if self.problem:
            raise self.problem
        return feeds.Reply(
            json.dumps({"markets": self.rows, "cursor": self.cursor}).encode(),
            self.now,
            self.now - timedelta(seconds=self.shift),
            None,
            self.age,
        )

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.monotonic += seconds

    def state(self):
        return self.tape.state()

    def test_only_due_scoped_public_reads_and_exact_cents(self):
        foreign = {
            **market(),
            "ticker": "KXOTHER-26NOV27-B44.5",
            "event_ticker": "KXOTHER-26NOV27",
        }
        wrong_event = {**market(), "event_ticker": "KXHIGHNY-26NOV26"}
        other_station = {
            **market(floor=46),
            "rules_primary": "Synthetic LaGuardia measurement.",
        }
        self.rows += [foreign, wrong_event, other_station]
        self.assertEqual(self.state()["status"], "waiting")
        self.assertTrue(self.tape.poll())
        first = self.state()
        self.assertEqual(first["status"], "live")
        self.assertEqual(first["last_success_at"], weather.iso(NOW))
        (row,) = first["markets"]
        self.assertEqual((row["bid_cents"], row["ask_cents"]), (31.75, 40))
        self.assertEqual(row["observed_at"], weather.iso(NOW))
        self.assertEqual(row["close_at"], "2026-11-28T06:00:00.000000Z")
        self.assertEqual(row["last_trading_at"], "2026-11-28T04:59:00.000000Z")
        self.assertEqual(
            row["history"],
            [{"at": weather.iso(NOW), "bid_cents": 31.75, "ask_cents": 40}],
        )
        self.advance(14)
        self.assertFalse(self.tape.poll())
        self.advance(1)
        self.assertTrue(self.tape.poll())
        self.assertEqual(self.calls, 2)
        self.assertEqual(
            self.state()["markets"][0]["history"][-1]["at"], weather.iso(self.now)
        )

    def test_absent_and_malformed_sides_are_never_invented_prices(self):
        self.rows[0].update(yes_bid_dollars="0.0000", yes_bid_size_fp="0.00")
        self.tape.poll()
        row = self.state()["markets"][0]
        self.assertEqual((row["bid_cents"], row["ask_cents"]), (None, 40))
        self.advance(15)
        self.rows[0].update(
            yes_bid_dollars="0.2100",
            yes_bid_size_fp="5.00",
            yes_ask_dollars="1.0000",
            yes_ask_size_fp="0.00",
        )
        self.tape.poll()
        self.assertEqual(self.state()["markets"][0]["bid_cents"], 21)
        self.assertIsNone(self.state()["markets"][0]["ask_cents"])
        self.advance(15)
        self.rows[0].update(yes_ask_dollars="NaN", yes_ask_size_fp="0.20")
        self.tape.poll()
        self.assertIn("invalid", self.state()["message"])
        self.assertEqual(self.state()["markets"][0]["bid_cents"], 21)
        self.assertIsNone(self.state()["markets"][0]["ask_cents"])
        self.advance(15)
        self.rows[0].update(yes_ask_dollars="0.1900")
        self.tape.poll()
        self.assertEqual(
            (
                self.state()["markets"][0]["bid_cents"],
                self.state()["markets"][0]["ask_cents"],
            ),
            (None, None),
        )

    def test_bad_transport_timestamps_do_not_rebadge_old_data(self):
        self.tape.poll()
        self.advance(15)
        self.age = 180
        self.tape.poll()
        out = self.state()
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["last_success_at"], weather.iso(NOW))
        self.assertEqual(out["last_checked_at"], weather.iso(self.now))
        row = out["markets"][0]
        self.assertIsNone(row["bid_cents"])
        self.assertEqual(row["observed_at"], weather.iso(NOW))
        self.assertEqual(
            row["history"][-1],
            {"at": weather.iso(self.now), "bid_cents": None, "ask_cents": None},
        )
        self.age = 0
        self.advance(15)
        self.shift = -90
        self.tape.poll()
        self.assertEqual(self.state()["last_success_at"], weather.iso(NOW))
        self.advance(30)
        self.shift = 0
        self.tape.poll()
        self.assertEqual(self.state()["status"], "live")
        self.assertEqual(self.state()["last_success_at"], weather.iso(self.now))

    def test_history_expires_is_bounded_and_blank_when_stale(self):
        self.tape.poll()
        self.advance(31)
        state = self.state()
        self.assertEqual(state["status"], "stale")
        self.assertIsNone(state["markets"][0]["ask_cents"])
        self.assertEqual(state["markets"][0]["history"][0]["ask_cents"], 40)
        for i in range(245):
            self.tape.poll()
            self.advance(15)
        row = self.state()["markets"][0]
        self.assertLessEqual(len(row["history"]), 241)
        self.assertGreaterEqual(
            weather.instant(row["history"][0]["at"]), self.now - timedelta(hours=1)
        )
        self.advance(3601)
        self.assertFalse(self.state()["markets"])

    def test_completed_catalog_removes_disappeared_and_ended_markets(self):
        self.rows.append(market(floor=46))
        self.tape.poll()
        self.assertEqual(len(self.state()["markets"]), 2)
        self.rows = self.rows[1:]
        self.advance(15)
        self.tape.poll()
        self.assertEqual(
            [r["id"] for r in self.state()["markets"]], [self.rows[0]["ticker"]]
        )
        self.rows[0]["close_time"] = weather.iso(self.now + timedelta(seconds=20))
        self.advance(15)
        self.tape.poll()
        self.advance(6)
        self.assertEqual(self.state()["markets"], [])
        self.advance(10)
        self.tape.poll()
        self.assertEqual(self.state()["status"], "empty")
        self.assertEqual(self.state()["markets"], [])

    def test_quote_display_uses_venue_close_even_after_strategy_stops_entries(self):
        self.now = weather.instant("2026-11-28T05:01:00Z")
        self.tape.poll()
        (row,) = self.state()["markets"]
        self.assertLess(weather.instant(row["last_trading_at"]), self.now)
        self.assertGreater(weather.instant(row["close_at"]), self.now)
        self.assertEqual(row["ask_cents"], 40)

    def test_incomplete_catalog_and_errors_preserve_gaps_and_back_off(self):
        self.tape.poll()
        self.advance(15)
        self.cursor = "opaque-other-page"
        self.tape.poll()
        self.assertEqual(self.state()["status"], "error")
        self.assertEqual(len(self.state()["markets"]), 1)
        self.advance(15)
        self.cursor = ""
        self.problem = feeds.Unavailable("HTTP 429", 429, 75)
        self.tape.poll()
        self.assertEqual(self.state()["status"], "rate_limited")
        self.advance(74)
        self.assertFalse(self.tape.poll())
        self.advance(1)
        self.problem = None
        self.assertTrue(self.tape.poll())
        self.assertEqual(self.calls, 4)
        self.assertEqual(self.state()["status"], "live")

    def test_permission_off_and_offline_erase_tape_and_block_requests(self):
        self.tape.poll()
        self.generation += 1
        self.mode = "disabled"
        self.tape.permission_changed()
        state = self.state()
        self.assertEqual(state["status"], "disabled")
        self.assertEqual(state["markets"], [])
        self.assertIsNone(state["last_success_at"])
        self.assertIsNone(state["last_checked_at"])
        self.advance(15)
        self.assertFalse(self.tape.poll())
        self.generation += 1
        self.mode = "offline"
        self.tape.permission_changed()
        self.assertFalse(self.tape.poll())
        self.assertEqual(self.state()["status"], "offline")
        self.assertEqual(self.calls, 1)

    def test_concurrent_polls_and_off_on_during_request_cannot_resurrect_quote(self):
        self.block = True
        t = threading.Thread(target=self.tape.poll)
        t.start()
        try:
            self.assertTrue(self.entered.wait(1))
            self.assertFalse(self.tape.poll())
            with self.lock:
                self.mode = "disabled"
                self.generation += 1
                self.tape.permission_changed()
                self.assertEqual(self.state()["markets"], [])
                self.mode = "enabled"
                self.generation += 1
                self.tape.permission_changed()
        finally:
            self.resume.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.state()["status"], "waiting")
        self.assertFalse(self.state()["markets"])
        self.assertIsNone(self.state()["last_success_at"])
        self.block = False
        self.assertFalse(self.tape.poll())
        self.advance(15)
        self.assertTrue(self.tape.poll())
        self.assertEqual(len(self.state()["markets"][0]["history"]), 1)

    def test_desk_is_network_idle_until_its_scheduler_starts(self):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            called = threading.Event()

            def live_fixture(url, **opts):
                opts["permission_guard"]()
                now = feeds.clock()
                m = market(weather.eastern(now).date() + timedelta(days=1))
                if url == feeds.OPEN:
                    called.set()
                    payload = {"markets": [m], "cursor": ""}
                else:
                    self.assertTrue(url.startswith(candles.SOURCE + "?"))
                    payload = {
                        "markets": [{"market_ticker": m["ticker"], "candlesticks": []}]
                    }
                return feeds.Reply(json.dumps(payload).encode(), now, now, None)

            network = stack.enter_context(
                patch.object(feeds, "download", side_effect=live_fixture)
            )
            stack.enter_context(
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("Real network disabled."),
                )
            )
            desk = Desk(Path(directory), online=True)
            for ledger in desk.ledgers.values():
                stack.callback(ledger.close)
            self.assertEqual(desk.state()["quotes"]["status"], "disabled")
            desk.action("permission", True)
            self.assertEqual(desk.state()["quotes"]["status"], "waiting")
            network.assert_not_called()
            research = threading.Event()
            stack.enter_context(
                patch.object(desk, "scan", side_effect=lambda **kwargs: research.set())
            )
            thread = threading.Thread(target=desk.schedule)
            thread.start()
            try:
                self.assertTrue(called.wait(1))
                self.assertTrue(research.wait(1))
                with desk.transition:
                    desk.action("permission", False)
                    self.assertEqual(desk.state()["quotes"]["markets"], [])
            finally:
                desk.stop.set()
                desk.wake.set()
                desk.quotes.wake.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(
                sum(call.args[0] == feeds.OPEN for call in network.call_args_list), 1
            )
            self.assertLessEqual(network.call_count, 2)


if __name__ == "__main__":
    unittest.main()
