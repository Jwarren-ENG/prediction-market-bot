import json
import threading
import unittest
from datetime import timedelta

from tests.test_bitcoin_contracts import book as kalshi_book
from tests.test_bitcoin_contracts import market as kalshi_market
from worker import bitcoin_contracts as k
from worker import btc_venues as v
from worker import feeds, weather

OPEN = weather.instant("2026-11-26T21:00:00Z")
NOW = OPEN + timedelta(minutes=2)


def iso(t):
    return weather.iso(t)


def event(minutes=5, opened=OPEN):
    end = opened + timedelta(minutes=minutes)
    horizon = {5: "5m", 60: "1h"}[minutes]
    eid = f"btc-updown-{horizon}-" + opened.strftime("%Y-%m-%d-%H%Mz")
    slug = f"cpc-{eid}"

    def t(at):
        local = weather.eastern(at)
        return (
            local.strftime("%I:%M%p ET on %b ").lstrip("0")
            + f"{local.day}, {local.year}"
        )

    m = {
        "id": "101",
        "slug": slug,
        "category": "crypto",
        "status": "MARKET_STATUS_OPEN",
        "active": True,
        "closed": False,
        "archived": False,
        "updatedAt": iso(opened),
        "startDate": iso(opened - timedelta(hours=12)),
        "endDate": iso(end + timedelta(minutes=30)),
        "outcomes": '["Yes","No"]',
        "feeCoefficient": 0.11,
        "minimumTradeQty": 0.01,
        "orderPriceMinTickSize": 0.01,
        "contractTermsUrl": v.US_RULES,
        "description": f"This market will settle to Up if the Bitcoin price at {t(end)} is greater than or equal to the price at {t(opened)}. Otherwise, this market will settle to Down. "
        "Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI). In the last minute before each of those two times, "
        "60 BRTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places.",
        "marketSides": [
            {
                "identifier": slug,
                "marketSideType": "MARKET_SIDE_TYPE_INSTRUMENT",
                "marketId": 101,
                "long": long,
                "description": side,
                "price": price,
                "tradable": True,
            }
            for long, side, price in ((True, "Yes", "0.47"), (False, "No", "0.53"))
        ],
        "assetPriceTerms": {
            "asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"},
            "indexSymbol": "BRTI",
            "priceDecimals": 2,
            "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            "horizon": horizon,
            "windowStart": iso(opened),
            "windowEnd": iso(end),
            "priceToBeat": {"currency": "USD", "value": "82001.34"},
        },
    }
    return {
        "id": "301",
        "ticker": eid,
        "slug": eid,
        "seriesSlug": "btc-updown-" + horizon,
        "title": "Fictional BTC window",
        "category": "crypto",
        "startDate": iso(opened),
        "endDate": iso(end),
        "updatedAt": iso(opened),
        "active": True,
        "archived": False,
        "closed": False,
        "markets": [m],
    }


def book(m, at):
    def levels(rows):
        return [
            {"px": {"currency": "USD", "value": price}, "qty": qty}
            for price, qty in rows
        ]

    return {
        "marketData": {
            "marketSlug": m["slug"],
            "state": "MARKET_STATE_OPEN",
            "transactTime": iso(at),
            "bids": levels([("0.4700", "4.1234"), ("0.4600", "20.0000")]),
            "offers": levels([("0.4900", "2.7600"), ("0.5100", "32.0000")]),
        }
    }


def reply(value, now, age=0):
    return feeds.Reply(
        json.dumps(value).encode(), now, now.replace(microsecond=0), None, age
    )


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.mono = 1000
        self.mode, self.generation = "enabled", 1
        self.errors = {}
        self.calls = []
        self.catalog = {}
        self.books = {}
        self.hook = None
        self.register(5)
        self.register(60)

    def register(self, minutes, opened=OPEN):
        e = event(minutes, opened)
        m = e["markets"][0]
        self.catalog[v.us_catalog(v.HORIZONS[minutes])] = {"events": [e]}
        self.books[v.us_market_url(m["slug"], "book")] = book(
            m, self.now - timedelta(seconds=4)
        )
        return e, m

    def fetch(self, url, *, permission_guard, max_bytes):
        permission_guard()
        self.calls.append(url)
        if self.hook:
            self.hook(url)
        if url in self.errors:
            raise self.errors[url]
        source = self.catalog if url in self.catalog else self.books
        return reply(source[url], self.now)

    def create(self, minutes=5):
        return v.PolymarketContracts(
            lambda: (self.mode, self.generation),
            threading.RLock(),
            interval_minutes=minutes,
            fetch=self.fetch,
            now=lambda: self.now,
            monotonic=lambda: self.mono,
        )

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def test_three_distinct_economic_times_real_sides_fees_and_current_windows(self):
        a, b = self.create(), self.create(60)
        for instance, n in ((a, 5), (b, 60)):
            self.assertTrue(instance.poll())
            s = instance.state()
            m = s["current"]
            self.assertEqual(
                (s["status"], s["interval_minutes"], m["venue"]),
                ("live", n, "polymarket_us"),
            )
            self.assertEqual(
                (
                    m["yes_bid_cents"],
                    m["yes_ask_cents"],
                    m["no_bid_cents"],
                    m["no_ask_cents"],
                ),
                (47, 49, 51, 53),
            )
            self.assertEqual(m["depth"]["no"]["asks"], [[53, 4.1234], [54, 20]])
            self.assertEqual(m["target_usd"], "82001.34")
            self.assertEqual(
                (m["fee_rate"], m["fee_source"], m["fee_verified_at"]),
                ("0.11", v.US_FEES, iso(NOW)),
            )
            self.assertEqual(m["close_at"], iso(OPEN + timedelta(minutes=n)))
            self.assertEqual(m["source_at"], iso(NOW - timedelta(seconds=4)))
            self.assertEqual(m["history"][0]["source_at"], m["source_at"])
            self.assertEqual(m["source_time_kind"], "venue_book_transact_time")
            self.assertEqual(m["observed_at"], iso(NOW))
            self.assertEqual(m["book_url"], self.calls[-1])
            self.assertEqual(m["tick_size_cents"], 1)
            self.assertTrue(m["rules_verified"])
        self.assertNotEqual(a.state()["current"]["id"], b.state()["current"]["id"])
        self.advance(17)
        for instance in (a, b):
            state = instance.state()
            self.assertEqual(
                state["status"], "stale"
            )  # venue age 21s, local receipt only 17s
            self.assertIsNone(state["current"]["yes_ask_cents"])

    def test_rollover_does_not_join_books_between_products_or_windows(self):
        f = self.create()
        f.poll()
        self.advance(3 * 60)
        _, m = self.register(5, OPEN + timedelta(minutes=5))
        self.books[v.us_market_url(m["slug"], "book")]["marketData"]["transactTime"] = (
            iso(self.now)
        )
        self.assertTrue(f.poll())
        state = f.state()
        self.assertEqual(len(state["recent"]), 1)
        self.assertEqual(state["recent"][0]["close_at"], state["current"]["open_at"])
        self.assertEqual(len(state["current"]["history"]), 1)
        self.assertFalse(state["recent"][0]["depth"]["yes"]["asks"])
        self.advance(10)
        self.catalog[f.catalog_url] = {"events": [event(60)]}
        self.assertTrue(f.poll())
        self.assertEqual(f.state()["status"], "empty")
        self.assertEqual(self.calls[-1], f.catalog_url)
        with self.assertRaises(ValueError):
            self.create(15)

    def test_rules_target_source_and_posted_price_quality_fail_closed(self):
        at = reply({}, NOW)
        adapter = self.create()
        changed = event()
        changed["markets"][0]["assetPriceTerms"]["priceToBeat"] = None
        changed["markets"][0]["feeCoefficient"] = 0.37
        meta = adapter.select_market({"events": [changed]}, at).meta
        self.assertIsNone(meta["target_usd"])
        self.assertIsNone(meta["fee_rate"])
        changed["markets"][0]["feeCoefficient"] = "unverified"
        self.assertIsNone(
            adapter.select_market({"events": [changed]}, at).meta["fee_rate"]
        )
        for mutation in (
            lambda m: m["marketSides"][0].update(long=False),
            lambda m: m["assetPriceTerms"].update(indexSymbol="OTHER"),
            lambda m: m["assetPriceTerms"].update(windowEnd=iso(NOW)),
            lambda m: m.update(
                description=m["description"].replace(
                    "greater than or equal to", "greater than"
                )
            ),
            lambda m: m.update(
                description=m["description"].replace(
                    "is the simple average", "is twice the simple average"
                )
            ),
            lambda m: m.update(slug="cpc-alt-55"),
            lambda m: m.update(contractTermsUrl="https://example.com/terms"),
        ):
            with self.subTest(mutation=mutation):
                e = event()
                mutation(e["markets"][0])
                with self.assertRaises(ValueError):
                    adapter.select_market({"events": [e]}, at)
        candidate = adapter.select_market({"events": [event()]}, at)
        for mutate in (
            lambda b: b.update(marketSlug="wrong"),
            lambda b: b.update(state="MARKET_STATE_EXPIRED"),
            lambda b: b.update(transactTime=iso(NOW + timedelta(seconds=1))),
            lambda b: b["bids"][0]["px"].update(currency="OTHER"),
            lambda b: b["bids"][0]["px"].update(value="0.5350"),
            lambda b: b["bids"][0]["px"].update(value="0.4900"),
        ):
            with self.subTest(mutate=mutate):
                data = book(event()["markets"][0], NOW)
                mutate(data["marketData"])
                with self.assertRaises(ValueError):
                    adapter.read_depth(candidate, reply(data, NOW))

    def test_stale_response_regains_eligibility_and_permission_discards_inflight(self):
        f = self.create()
        f.poll()
        eid = f.state()["current"]["id"]
        u = v.us_market_url(eid, "book")
        self.advance(17)
        self.assertTrue(f.poll())
        self.assertEqual(f.state()["status"], "stale")
        self.assertIsNone(f.state()["current"]["history"][-1]["source_at"])
        self.advance(10)
        self.books[u]["marketData"]["transactTime"] = iso(self.now)
        self.assertTrue(f.poll())
        self.assertEqual(f.state()["status"], "live")
        self.advance(10)
        self.errors[f.catalog_url] = feeds.Unavailable("denied", 451)
        self.assertTrue(f.poll())
        self.assertEqual(f.state()["status"], "restricted")
        self.assertIsNone(f.state()["current"]["yes_ask_cents"])
        self.errors.clear()
        self.advance(300)
        self.now = OPEN + timedelta(minutes=7)
        _, m = self.register(5, OPEN + timedelta(minutes=5))
        self.books[v.us_market_url(m["slug"], "book")]["marketData"]["transactTime"] = (
            iso(self.now)
        )

        def revoke(url):
            if url.endswith("/book"):
                self.mode = "disabled"
                f.permission_changed()
                self.mode = "enabled"
                self.generation += 1
                f.permission_changed()

        self.hook = revoke
        self.assertTrue(f.poll())
        s = f.state()
        self.assertIsNone(s["current"])
        self.assertIsNone(s["last_success_at"])
        self.assertFalse(s["recent"])

    def test_endpoint_allowlist_and_offline(self):
        for bad in (
            "https://clob.polymarket.com/markets",
            v.US_API + "/v1/orders",
            v.us_catalog("15m"),
            v.US_API + "/v1/market/slug/cpc-btc-updown-5m-2026-11-26-2100z?other=1",
        ):
            with self.assertRaises(ValueError):
                v.us_download(bad, max_bytes=30, permission_guard=lambda: True)
        f = self.create(60)
        self.mode = "offline"
        self.assertFalse(f.poll())
        self.assertFalse(self.calls)
        self.assertEqual(f.state()["status"], "offline")

    def test_conflicting_cached_us_book_has_no_executable_depth(self):
        f = self.create()
        f.poll()
        m = f.state()["current"]
        self.advance(10)
        self.books[m["book_url"]]["marketData"]["bids"][0]["qty"] = "99.0000"
        self.assertTrue(f.poll())
        s = f.state()
        self.assertEqual(s["status"], "error")
        self.assertIsNone(s["current"]["yes_bid_size"])
        self.assertEqual(s["current"]["history"][-1]["kind"], "gap")


class SettlementTest(unittest.TestCase):
    def setUp(self):
        e = event()
        self.market = e["markets"][0]
        self.id = self.market["slug"]
        self.market.update(closed=True, status="MARKET_STATUS_RESOLVED")
        for side in self.market["marketSides"]:
            side["price"] = "1" if side["long"] else "0"
        self.now = OPEN + timedelta(hours=2)
        self.result = 1
        self.calls = []
        self.guard = lambda: True

    def fetch(self, url, *, permission_guard, max_bytes):
        permission_guard()
        self.calls.append(url)
        if url.endswith("/settlement"):
            if isinstance(self.result, Exception):
                raise self.result
            return reply({"slug": self.id, "settlement": self.result}, self.now)
        return reply({"market": self.market}, self.now)

    def lookup(self):
        return v.official_outcome(
            "polymarket_us",
            self.id,
            permission_guard=self.guard,
            fetch=self.fetch,
            now=self.now,
        )

    def test_exact_official_payout_and_final_metadata_required(self):
        out = self.lookup()
        self.assertEqual(out["result"], "YES")
        self.assertEqual(out["observed_at"], iso(self.now))
        self.assertIsNone(out["resolved_at"])
        self.assertEqual(out["source"], v.us_market_url(self.id, "settlement"))
        self.result = 0
        self.assertIsNone(
            self.lookup()
        )  # The official metadata and settlement disagree.
        for s in self.market["marketSides"]:
            s["price"] = "0" if s["long"] else "1"
        self.assertEqual(self.lookup()["result"], "NO")
        self.result = 0.5
        self.assertIsNone(self.lookup())
        self.result = feeds.Unavailable("not yet", 404)
        self.assertIsNone(self.lookup())
        self.calls = []
        self.market.update(status="MARKET_STATUS_RESOLVING")
        self.assertIsNone(self.lookup())
        self.assertEqual(len(self.calls), 1)
        self.guard = lambda: False
        with self.assertRaises(feeds.PermissionRevoked):
            self.lookup()
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            v.official_outcome(
                "polymarket_us_hourly",
                self.id,
                permission_guard=self.guard,
                fetch=self.fetch,
                now=self.now,
            )

    def test_midread_revoke_never_publishes_outcome(self):
        def fetch_and_revoke(url, **kwargs):
            out = self.fetch(url, **kwargs)
            self.guard = lambda: False
            return out

        with self.assertRaises(feeds.PermissionRevoked):
            v.official_outcome(
                "polymarket_us",
                self.id,
                permission_guard=lambda: self.guard(),
                fetch=fetch_and_revoke,
                now=self.now,
            )
        self.assertEqual(len(self.calls), 1)

    def test_official_kalshi_historical_fallback_and_consistent_final_value(self):
        m = kalshi_market()
        eid = m["ticker"]
        m.update(
            status="finalized",
            result="yes",
            settlement_value_dollars="1.0000",
            settlement_ts=iso(OPEN + timedelta(minutes=16)),
        )
        calls = []

        def reader(url, **kwargs):
            calls.append(url)
            if "/historical/" not in url:
                raise feeds.Unavailable("archived", 404)
            return reply({"market": m}, self.now)

        def lookup():
            return v.official_outcome(
                "kalshi", eid, permission_guard=lambda: True, fetch=reader, now=self.now
            )

        s = lookup()
        self.assertEqual(s["source"], calls[-1])
        self.assertIn("/historical/markets/", s["source"])
        self.assertEqual(s["resolved_at"], m["settlement_ts"])
        for change in (
            {"settlement_value_dollars": "0"},
            {"status": "determined"},
            {"result": "scalar"},
            {"is_provisional": True},
            {"is_provisional": 0},
            {"settlement_ts": iso(OPEN)},
            {"close_time": iso(self.now)},
            {
                "open_time": iso(OPEN + timedelta(seconds=1)),
                "close_time": iso(OPEN + timedelta(minutes=15, seconds=1)),
            },
        ):
            with self.subTest(change=change):
                prior = m.copy()
                m.update(change)
                self.assertIsNone(lookup())
                m.clear()
                m.update(prior)
        for bad in ("ABC", "KXBTC15M-26NOV261615-15/../../orders"):
            with self.assertRaises(ValueError):
                v.official_outcome(
                    "kalshi",
                    bad,
                    permission_guard=lambda: True,
                    fetch=reader,
                    now=self.now,
                )


class FeeTest(unittest.TestCase):
    def test_fee_read_discards_old_generation_and_invalidates_changed_waiver(self):
        m = kalshi_market()
        event_id = m["event_ticker"]
        data = {
            k.CATALOG: {"markets": [m], "cursor": ""},
            k.book_url(m["ticker"]): kalshi_book(),
            k.KALSHI_SERIES: {
                "series": {
                    "ticker": "KXBTC15M",
                    "contract_terms_url": k.RULES,
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                    "last_updated_ts": iso(OPEN),
                }
            },
            k.KALSHI_CHANGES: {"series_fee_change_arr": []},
            k.KALSHI_EVENTS + event_id: {"cursor": "", "event_fee_changes": []},
        }
        generation, received, revoke = 1, NOW, True

        def fetch(url, *, permission_guard, max_bytes):
            nonlocal generation, revoke
            permission_guard()
            if revoke and url == k.KALSHI_EVENTS + event_id:
                revoke = False
                generation += 1  # Off/on while the venue response was in flight.
                f.permission_changed()
            return reply(data[url], received)

        f = k.BitcoinContracts(
            lambda: ("enabled", generation),
            threading.RLock(),
            fetch=fetch,
            now=lambda: received,
            monotonic=lambda: received.timestamp(),
        )
        self.assertTrue(f.poll())
        self.assertTrue(f.poll_fees())
        self.assertIsNone(f.state()["current"])
        received += timedelta(seconds=11)
        self.assertTrue(f.poll())
        self.assertIsNone(f.state()["current"]["fee_rate"])
        received += timedelta(seconds=21)
        self.assertTrue(f.poll())
        self.assertTrue(f.poll_fees())
        self.assertEqual(f.state()["current"]["fee_rate"], "0.07")
        received += timedelta(seconds=11)
        m["fee_waiver_expiration_time"] = iso(received + timedelta(minutes=1))
        self.assertTrue(f.poll())
        self.assertIsNone(f.state()["current"]["fee_rate"])

    def test_series_event_waiver_and_expiry(self):
        event_id = "KXBTC15M-26NOV261615"
        close = OPEN + timedelta(minutes=15)
        future = NOW + timedelta(seconds=65)
        sources = {
            k.KALSHI_SERIES: {
                "series": {
                    "ticker": "KXBTC15M",
                    "last_updated_ts": iso(OPEN),
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                    "contract_terms_url": k.RULES,
                }
            },
            k.KALSHI_CHANGES: {
                "series_fee_change_arr": [
                    {"series_ticker": "KXBTC15M", "scheduled_ts": iso(future)}
                ]
            },
            k.KALSHI_EVENTS + event_id: {"cursor": "", "event_fee_changes": []},
        }

        def verify(waiver=None):
            return v.kalshi_fee_snapshot(
                lambda u: reply(sources[u], NOW), event_id, close, waiver
            )

        first = verify()
        self.assertEqual(
            (first["fee_rate"], first["fee_valid_until"]), ("0.07", iso(future))
        )
        override = {
            "series_ticker": "KXBTC15M",
            "event_ticker": event_id,
            "scheduled_ts": iso(OPEN),
            "fee_multiplier_override": 2,
        }
        sources[k.KALSHI_EVENTS + event_id]["event_fee_changes"] = [override]
        self.assertEqual(verify()["fee_rate"], "0.14")
        waived = verify(iso(NOW + timedelta(seconds=20)))
        self.assertEqual(waived["fee_rate"], "0")
        self.assertEqual(waived["fee_valid_until"], iso(NOW + timedelta(seconds=20)))
        sources[k.KALSHI_EVENTS + event_id]["cursor"] = "more"
        self.assertIsNone(verify())
        sources[k.KALSHI_EVENTS + event_id]["cursor"] = ""
        sources[k.KALSHI_CHANGES]["series_fee_change_arr"][0]["scheduled_ts"] = iso(NOW)
        self.assertIsNone(verify())
