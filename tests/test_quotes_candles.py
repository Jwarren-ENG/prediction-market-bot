"""The hourly, minute and live quote series use independent clocks and provenance."""

import json
import threading
import unittest
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from tests.test_quotes import NOW, market
from worker import candles, feeds, quotes, weather


def period(stamp, bid="0.1700", ask="0.3250"):
    # A trade close is deliberately different from either posted YES side.
    return {
        "end_period_ts": stamp,
        "yes_bid": {"close_dollars": bid},
        "yes_ask": {"close_dollars": ask},
        "price": {"close_dollars": "0.6100"},
    }


def group(ticker, records):
    return {"market_ticker": ticker, "candlesticks": records}


def utc_at(timestamp):
    return weather.iso(NOW.fromtimestamp(timestamp, weather.UTC))


class CandleBoundariesTest(unittest.TestCase):
    def test_native_hourly_request_limits_and_sparse_seven_day_window(self):
        window = candles.HOURLY
        end = window.completed_at(NOW + timedelta(minutes=47))
        archive = candles.Archive(window)
        ids = tuple(f"id{i:02}" for i in range(100))
        calls = archive.plan(ids, end, 1)
        self.assertEqual([len(c.tickers) for c in calls], [59, 41])
        for c in calls:
            self.assertEqual((c.end, c.start), (end, end - 7 * 86400))
            self.assertLessEqual(
                (1 + (c.end - c.start) // 3600) * len(c.tickers), 10_000
            )
            query = parse_qs(urlsplit(c.url()).query)
            self.assertEqual(query["period_interval"], ["60"])
            self.assertNotIn("include_latest_before_start", query)

        req = candles.Request(("week", "new"), end - window.span, end, window)
        # A full inclusive response can contain an extra lower-boundary bar.
        full = [period(t) for t in range(req.start, end + 1, 3600)]
        opening = end - 3 * 3600 + 1500
        sparse = [
            period(req.start),
            period(end - 3 * 3600),
            period(end - 2 * 3600, "0.0000", "0.4400"),
            period(end, "0.3000", "1.0000"),
            period(end + 3600),
            period(end - 60),
        ]
        records, issues = candles.parse(
            {"markets": [group("week", full), group("new", sparse)]},
            req,
            {"week": req.start - 86400, "new": opening},
        )
        self.assertEqual(issues, {"new"})
        self.assertEqual(len(records["week"]), 168)
        self.assertNotIn(req.start, records["week"])
        self.assertEqual(records["new"], {end - 2 * 3600: (None, 44), end: (30, None)})
        archive.merge(req, records, issues, req.tickers, 2)
        self.assertEqual(
            [v["at"] for v in archive.view("new")],
            [utc_at(end - 2 * 3600), utc_at(end)],
        )
        archive.retain(["week"], end + 2 * 3600)
        self.assertEqual(len(archive.view("week")), 166)
        self.assertFalse(archive.view("new"))

    def test_only_requested_completed_periods_and_reported_sides(self):
        end = candles.MINUTE.completed_at(NOW)
        req = candles.Request(("verified",), end - candles.MINUTE.span, end)
        records = [
            period(end + 60),
            period(end),
            period(req.start),
            period(end - 60, "0.0000", "0.4050"),
            period(end - 180, "0.1999", "1.0000"),
            period(end - 240, "0.0500", None),
            period(end - 300, "NaN", "0.2700"),
            period(end - 360, "0.3100", "0.2600"),
            period(end - 420, "0.20000", "0.3600"),
            period(end - 420, "0.2100", "0.3600"),
            period(end - 421),
            period(end - 480),  # Exactly at market opening.
        ]
        returned, issues = candles.parse(
            {"markets": [group("foreign", [period(end)]), group("verified", records)]},
            req,
            {"verified": end - 480},
        )
        self.assertEqual(issues, {"verified"})
        self.assertEqual(
            returned,
            {
                "verified": {
                    end: (17, 32.5),
                    end - 60: (None, 40.5),
                    end - 180: (19.99, None),
                    end - 240: (5, None),
                    end - 300: (None, 27),
                    end - 360: (None, None),
                }
            },
        )

    def test_exact_four_hours_and_documented_batch_limits(self):
        end = candles.MINUTE.completed_at(NOW)
        a = candles.Archive()
        names = tuple(f"m{i:02}" for i in range(100))
        calls = a.plan(names, end, 0)
        self.assertEqual([len(r.tickers) for r in calls], [41, 41, 18])
        self.assertEqual(set(names), {t for req in calls for t in req.tickers})
        for req in calls:
            self.assertEqual(req.start, end - 240 * 60)
            sizes = 1 + (req.end - req.start) // 60
            self.assertLessEqual(sizes * len(req.tickers), 10_000)
            parts = urlsplit(req.url())
            query = parse_qs(parts.query)
            self.assertEqual(parts.path, "/trade-api/v2/markets/candlesticks")
            self.assertEqual(
                set(query), {"market_tickers", "start_ts", "end_ts", "period_interval"}
            )
            self.assertEqual(query["period_interval"], ["1"])
        req = candles.Request(("m00",), end - candles.MINUTE.span, end)
        records = [period(t) for t in range(req.start, req.end + 1, 60)]
        data, issues = candles.parse(
            {"markets": [group("m00", records)]}, req, {"m00": req.start - 86400}
        )
        self.assertEqual(len(data["m00"]), 240)
        self.assertFalse(issues)
        a.merge(req, data, issues, names, 1)
        view = a.view("m00")
        self.assertEqual(
            (view[0]["at"], view[-1]["at"]), (utc_at(req.start + 60), utc_at(end))
        )

    def test_duplicate_group_or_conflicting_closes_are_never_chosen_by_order(self):
        end = candles.MINUTE.completed_at(NOW)
        req = candles.Request(("a", "b"), end - 240 * 60, end)
        a = group("a", [period(end)])
        b = group("b", [period(end), period(end)])
        data, issues = candles.parse({"markets": [a, b, a]}, req, {"a": 0, "b": 0})
        self.assertEqual(data, {"b": {end: (17, 32.5)}})
        self.assertEqual(issues, {"a"})


class VenueHistoryTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW + timedelta(seconds=12)
        self.mono = 1000
        self.generation = 1
        self.mode = "enabled"
        self.listings = [market()]
        self.tick = self.listings[0]["ticker"]
        self.calls = []
        self.hourly_calls = []
        self.missing = set()
        self.no_close = False
        self.error = None
        self.error_period = None
        self.age = 0
        self.header_shift = 0
        self.block = False
        self.block_period = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.RLock()
        self.tape = quotes.QuoteTape(
            lambda: (self.mode, self.generation),
            self.lock,
            fetch=self.fetch,
            now=lambda: self.now,
            monotonic=lambda: self.mono,
        )

    def fetch(self, url, *, permission_guard, max_bytes):
        permission_guard()
        if url == feeds.OPEN:
            return feeds.Reply(
                json.dumps({"markets": self.listings, "cursor": ""}).encode(),
                self.now,
                self.now,
                None,
            )
        self.assertTrue(url.startswith(candles.SOURCE + "?"))
        self.assertLessEqual(max_bytes, 8_000_000)
        query = parse_qs(urlsplit(url).query)
        names = tuple(query["market_tickers"][0].split(","))
        start, end = int(query["start_ts"][0]), int(query["end_ts"][0])
        self.assertLessEqual(set(names), {m["ticker"] for m in self.listings})
        self.assertNotIn("include_latest_before_start", query)
        interval = int(query["period_interval"][0])
        self.assertIn(interval, (1, 60))
        calls = self.calls if interval == 1 else self.hourly_calls
        calls.append((names, start, end))
        self.started.set()
        if (
            self.block
            and self.block_period in (None, interval)
            and not self.release.wait(2)
        ):
            raise RuntimeError("Historical test source timed out.")
        if self.error and self.error_period in (None, interval):
            raise self.error
        out = [
            group(
                t, [] if self.no_close else [period(start + interval * 60), period(end)]
            )
            for t in names
            if t not in self.missing
        ]
        body = json.dumps({"markets": out}).encode()
        return feeds.Reply(
            body,
            self.now,
            self.now - timedelta(seconds=self.header_shift),
            None,
            self.age,
        )

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def fetch_live(self, seconds=0):
        self.advance(seconds)
        self.assertTrue(self.tape.poll())

    def test_cold_start_uses_real_minute_clock_and_keeps_receipt_tape_independent(self):
        self.assertFalse(self.tape.poll_history())
        self.fetch_live()
        pre = self.tape.state()
        self.assertEqual(pre["history_status"], "waiting")
        self.assertEqual(pre["markets"][0]["candles"], [])
        self.assertTrue(self.tape.poll_history())
        s = self.tape.state()
        end = candles.MINUTE.completed_at(self.now)
        self.assertEqual(s["history_status"], "ready")
        self.assertEqual(s["history_source_url"], candles.SOURCE)
        self.assertEqual(s["history_last_success_at"], weather.iso(self.now))
        self.assertEqual(s["history_last_checked_at"], weather.iso(self.now))
        self.assertEqual(
            (s["history_window_minutes"], s["history_period_seconds"]), (240, 60)
        )
        (row,) = s["markets"]
        self.assertEqual(row["history"], pre["markets"][0]["history"])
        self.assertEqual((row["bid_cents"], row["ask_cents"]), (31.75, 40))
        self.assertEqual(
            row["candles"],
            [
                {
                    "at": utc_at(end - candles.MINUTE.span + 60),
                    "bid_cents": 17,
                    "ask_cents": 32.5,
                },
                {"at": utc_at(end), "bid_cents": 17, "ask_cents": 32.5},
            ],
        )
        self.fetch_live(15)
        self.assertFalse(self.tape.poll_history())
        self.assertEqual(len(self.calls), 1)
        self.fetch_live(60)
        self.assertTrue(self.tape.poll_history())
        self.assertEqual(self.calls[-1], ((self.tick,), end - 120, end + 60))
        self.assertNotIn(row["candles"][0], self.tape.state()["markets"][0]["candles"])

    def test_new_market_gets_full_window_existing_market_reuses_cache_and_sleep_catches_up(
        self,
    ):
        self.fetch_live()
        self.tape.poll_history()
        new = market(floor=46)
        self.listings.append(new)
        self.fetch_live(60)
        self.assertEqual(self.tape.state()["history_status"], "partial")
        self.tape.poll_history()
        end = candles.MINUTE.completed_at(self.now)
        self.assertEqual(
            self.calls[-2:],
            [
                ((new["ticker"],), end - candles.MINUTE.span, end),
                ((self.tick,), end - 180, end),
            ],
        )
        self.fetch_live(2 * 3600)
        self.tape.poll_history()
        end = candles.MINUTE.completed_at(self.now)
        self.assertEqual(self.calls[-1][1:], (end - 2 * 3600 - 120, end))
        self.fetch_live(4 * 3600 + 1)
        self.tape.poll_history()
        end = candles.MINUTE.completed_at(self.now)
        self.assertEqual(self.calls[-1][1:], (end - candles.MINUTE.span, end))
        self.listings = [new]
        self.fetch_live(15)
        self.assertEqual(
            [r["id"] for r in self.tape.state()["markets"]], [new["ticker"]]
        )

    def test_missing_markets_have_bounded_backfill_and_empty_candles_stay_empty(self):
        newcomer = market(floor=46)
        self.listings.append(newcomer)
        self.missing = {newcomer["ticker"]}
        self.fetch_live()
        self.tape.poll_history()
        results = self.tape.state()
        self.assertEqual(results["history_status"], "partial")
        self.assertTrue(results["markets"][0]["candles"])
        self.assertFalse(results["markets"][1]["candles"])
        self.fetch_live(60)
        self.tape.poll_history()
        self.assertNotIn(newcomer["ticker"], self.calls[-1][0])
        self.missing.clear()
        self.fetch_live(240)
        self.tape.poll_history()
        self.assertIn(
            (
                (newcomer["ticker"],),
                candles.MINUTE.completed_at(self.now) - candles.MINUTE.span,
                candles.MINUTE.completed_at(self.now),
            ),
            self.calls,
        )
        self.assertEqual(self.tape.state()["history_status"], "ready")
        self.no_close = True
        self.fetch_live(4 * 3600)
        self.tape.poll_history()
        s = self.tape.state()
        self.assertEqual(s["history_status"], "empty")
        self.assertEqual(s["status"], "live")
        self.assertTrue(all(not row["candles"] for row in s["markets"]))

    def test_history_rate_limit_and_old_server_clock_keep_live_healthy(self):
        self.fetch_live()
        self.tape.poll_history()
        previous = self.tape.state()
        self.error = feeds.Unavailable("rate limit", 429, 80)
        self.fetch_live(60)
        self.tape.poll_history()
        s = self.tape.state()
        self.assertEqual((s["status"], s["history_status"]), ("live", "rate_limited"))
        self.assertEqual(
            s["history_last_success_at"], previous["history_last_success_at"]
        )
        self.assertIn(previous["markets"][0]["candles"][-1], s["markets"][0]["candles"])
        self.fetch_live(60)
        self.assertFalse(self.tape.poll_history())
        self.fetch_live(20)
        self.error = None
        self.header_shift = 125
        self.tape.poll_history()
        s = self.tape.state()
        self.assertEqual((s["status"], s["history_status"]), ("live", "error"))
        self.assertEqual(
            s["history_last_success_at"], previous["history_last_success_at"]
        )
        self.fetch_live(30)
        self.header_shift = 0
        self.tape.poll_history()
        self.assertEqual(self.tape.state()["history_status"], "ready")

    def test_blocked_history_does_not_delay_live_quotes_and_revoke_discards_response(
        self,
    ):
        self.fetch_live()
        self.block = True
        thread = threading.Thread(target=self.tape.poll_history)
        thread.start()
        try:
            self.assertTrue(self.started.wait(1))
            self.assertEqual(self.tape.state()["history_status"], "loading")
            self.assertFalse(self.tape.poll_history())
            self.fetch_live(15)
            self.assertEqual(len(self.tape.state()["markets"][0]["history"]), 2)
            with self.lock:
                self.generation += 1
                self.mode = "disabled"
                self.tape.permission_changed()
                private = self.tape.state()
                self.assertEqual(private["history_status"], "disabled")
                self.assertEqual(private["markets"], [])
                self.assertIsNone(private["history_last_checked_at"])
                self.generation += 1
                self.mode = "enabled"
                self.tape.permission_changed()
                self.fetch_live(15)
        finally:
            self.release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        s = self.tape.state()
        self.assertEqual((s["status"], s["history_status"]), ("live", "waiting"))
        self.assertIsNone(s["history_last_success_at"])
        self.assertEqual(s["markets"][0]["candles"], [])
        self.mode = "offline"
        self.tape.permission_changed()
        self.assertFalse(self.tape.poll_history())
        self.assertEqual(self.tape.state()["history_status"], "offline")

    def test_closed_market_removed_during_historical_request_cannot_be_restored(self):
        self.fetch_live()
        self.block = True
        thread = threading.Thread(target=self.tape.poll_history)
        thread.start()
        try:
            self.assertTrue(self.started.wait(1))
            self.listings.clear()
            self.fetch_live(15)
        finally:
            self.release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        result = self.tape.state()
        self.assertEqual(
            (result["status"], result["history_status"]), ("empty", "empty")
        )
        self.assertEqual(result["markets"], [])
        self.assertIsNone(result["history_last_success_at"])

    def test_hourly_history_is_distinct_and_hits_the_venue_only_when_due(self):
        hour = candles.HOURLY
        end = hour.completed_at(self.now)
        self.fetch_live()
        self.assertTrue(self.tape.poll_history())
        self.assertTrue(self.tape.poll_history(hour))
        result = self.tape.state()
        self.assertEqual(result["hourly_history_status"], "ready")
        self.assertEqual(
            result["hourly_history_last_checked_at"], weather.iso(self.now)
        )
        self.assertEqual(
            result["hourly_history_last_success_at"], weather.iso(self.now)
        )
        self.assertEqual(result["hourly_history_period_seconds"], 3600)
        self.assertEqual(result["hourly_history_window_minutes"], 7 * 24 * 60)
        self.assertEqual(result["hourly_history_source_url"], candles.SOURCE)
        (m,) = result["markets"]
        self.assertEqual(m["open_at"], self.listings[0]["open_time"])
        self.assertEqual(len(m["history"]), 1)
        self.assertEqual(len(m["candles"]), 2)
        # The fixture also sends a week-old bar that predates this listing.
        self.assertEqual(
            m["hourly_candles"],
            [{"at": utc_at(end), "bid_cents": 17, "ask_cents": 32.5}],
        )
        self.assertEqual(self.hourly_calls, [((self.tick,), end - 7 * 86400, end)])
        self.fetch_live(15 * 60)
        self.assertFalse(self.tape.poll_history(hour))
        self.assertEqual(self.tape.state()["hourly_history_status"], "ready")
        self.assertTrue(self.tape.poll_history())
        self.fetch_live(45 * 60)
        self.assertTrue(self.tape.poll_history(hour))
        self.assertEqual(
            self.hourly_calls[-1], ((self.tick,), end - 2 * 3600, end + 3600)
        )

    def test_new_listing_skips_hourly_requests_until_a_period_actually_closes(self):
        hour = candles.HOURLY
        self.listings[0]["open_time"] = weather.iso(self.now - timedelta(seconds=1))
        self.fetch_live()
        self.assertFalse(self.tape.poll_history(hour))
        r = self.tape.state()
        self.assertEqual(r["hourly_history_status"], "empty")
        self.assertFalse(r["markets"][0]["hourly_candles"])
        self.assertIsNone(r["hourly_history_last_success_at"])
        self.assertFalse(self.hourly_calls)
        self.fetch_live(3600)
        self.assertTrue(self.tape.poll_history(hour))
        end = hour.completed_at(self.now)
        self.assertEqual(
            [p["at"] for p in self.tape.state()["markets"][0]["hourly_candles"]],
            [utc_at(end)],
        )
        self.listings.append(market(floor=46))
        self.fetch_live(15)
        self.assertEqual(self.tape.state()["hourly_history_status"], "partial")
        self.assertTrue(self.tape.poll_history(hour))
        self.assertEqual(
            self.hourly_calls[-1],
            ((self.listings[-1]["ticker"],), end - hour.span, end),
        )

    def test_hourly_throttle_and_missing_group_leave_other_quote_streams_healthy(self):
        hour = candles.HOURLY
        self.fetch_live()
        self.tape.poll_history(hour)
        recorded = self.tape.state()["markets"][0]["hourly_candles"]
        saved_at = self.tape.state()["hourly_history_last_success_at"]
        self.error = feeds.Unavailable("rate limit", 429, 80)
        self.error_period = 60
        self.fetch_live(3600)
        self.assertTrue(self.tape.poll_history(hour))
        self.assertTrue(self.tape.poll_history())
        s = self.tape.state()
        self.assertEqual(
            (s["status"], s["history_status"], s["hourly_history_status"]),
            ("live", "ready", "rate_limited"),
        )
        self.assertEqual(s["markets"][0]["hourly_candles"], recorded)
        self.assertEqual(s["hourly_history_last_success_at"], saved_at)
        self.fetch_live(60)
        self.assertFalse(self.tape.poll_history(hour))
        self.assertTrue(self.tape.poll_history())
        self.fetch_live(20)
        self.error = None
        self.missing = {self.tick}
        self.assertTrue(self.tape.poll_history(hour))
        s = self.tape.state()
        self.assertEqual(s["hourly_history_status"], "partial")
        self.assertEqual(s["markets"][0]["hourly_candles"], recorded)
        self.fetch_live(60)
        self.assertFalse(self.tape.poll_history(hour))
        self.missing.clear()
        self.fetch_live(candles.MISSING_RETRY - 60)
        self.assertTrue(self.tape.poll_history(hour))
        self.assertEqual(self.tape.state()["hourly_history_status"], "ready")

    def test_blocked_hourly_fetch_spares_minute_and_live_and_cannot_survive_revoke(
        self,
    ):
        hour = candles.HOURLY
        self.fetch_live()
        self.block = True
        self.block_period = 60
        t = threading.Thread(target=self.tape.poll_history, args=(hour,))
        t.start()
        try:
            self.assertTrue(self.started.wait(1))
            self.assertEqual(self.tape.state()["hourly_history_status"], "loading")
            self.assertTrue(self.tape.poll_history())
            self.assertEqual(self.tape.state()["history_status"], "ready")
            self.fetch_live(15)
            self.assertFalse(self.tape.poll_history(hour))
            self.assertEqual(len(self.tape.state()["markets"][0]["history"]), 2)
            with self.lock:
                self.mode = "disabled"
                self.generation += 1
                self.tape.permission_changed()
                s = self.tape.state()
                self.assertEqual(
                    (s["history_status"], s["hourly_history_status"]),
                    ("disabled", "disabled"),
                )
                self.assertIsNone(s["hourly_history_last_checked_at"])
                self.assertEqual(s["markets"], [])
                self.mode = "enabled"
                self.generation += 1
                self.tape.permission_changed()
                self.fetch_live(15)
        finally:
            self.release.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        s = self.tape.state()
        self.assertEqual(s["hourly_history_status"], "waiting")
        self.assertIsNone(s["hourly_history_last_success_at"])
        self.assertEqual(s["markets"][0]["hourly_candles"], [])
        self.assertEqual(s["markets"][0]["candles"], [])

    def test_changed_listing_lifetime_discards_inflight_hourly_data(self):
        hour = candles.HOURLY
        self.fetch_live()
        self.block = True
        self.block_period = 60
        t = threading.Thread(target=self.tape.poll_history, args=(hour,))
        t.start()
        try:
            self.assertTrue(self.started.wait(1))
            self.listings[0]["open_time"] = weather.iso(self.now - timedelta(minutes=2))
            self.fetch_live(15)
        finally:
            self.release.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        s = self.tape.state()
        self.assertIsNone(s["hourly_history_last_success_at"])
        self.assertEqual(s["markets"][0]["hourly_candles"], [])
        self.assertEqual(s["markets"][0]["open_at"], self.listings[0]["open_time"])
