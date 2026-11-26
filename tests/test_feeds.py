import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from worker import feeds as f, weather as w

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/central_park_feed.json").read_text()
)
NOW = w.instant(FIXTURE["now"])


def market(day=date(2026, 11, 28), strike="between"):
    low, high = FIXTURE["low"], FIXTURE["high"]
    short = day.strftime("%b") + f" {day.day}, {day.year}"
    long = day.strftime("%B") + f" {day.day}, {day.year}"
    event = "KXHIGHNY-" + day.strftime("%y%b%d").upper()
    code = {"between": "B77.5", "less": "T77", "greater": "T78"}[strike]
    phrase = {
        "between": "between 77-78°",
        "less": "less than 77°",
        "greater": "greater than 78°",
    }[strike]
    return {
        "market_type": "binary",
        "price_level_structure": "linear_cent",
        "notional_value_dollars": "1.0000",
        "ticker": event + "-" + code,
        "event_ticker": event,
        "title": "Synthetic Central Park high",
        "rules_primary": f"If the maximum temperature recorded at New York City (CLINYC) for {short}, "
        f"is {phrase} fahrenheit according to The Weather Company, then the market resolves to Yes.",
        "rules_secondary": FIXTURE["secondary"],
        "early_close_condition": f"The Last Trading Time will be 11:59 PM local time on {long} regardless of any data releases or "
        f"events occurring. Expiration will occur on the sooner of the first 7:00 or 8:00 AM ET following "
        f"the release of the data for {long}, or one week after {long}.",
        "open_time": w.iso(w.midnight(day) - timedelta(days=1)),
        "updated_time": w.iso(w.midnight(day) - timedelta(days=1)),
        "close_time": w.iso(w.event_end(day) + timedelta(hours=1)),
        "status": "active",
        "strike_type": strike,
        "floor_strike": (
            low if strike == "between" else high if strike == "greater" else None
        ),
        "cap_strike": (
            high if strike == "between" else low if strike == "less" else None
        ),
    }


def cli(preliminary):
    asof = "VALID TODAY AS OF 0400 PM LOCAL TIME.\n" if preliminary else ""
    coverage = "TODAY" if preliminary else "YESTERDAY"
    issued = "2026-11-26T21:45:00Z" if preliminary else "2026-11-27T06:15:00Z"
    return {
        "issuingOffice": "KOKX",
        "productCode": "CLI",
        "issuanceTime": issued,
        "productText": f"000\nCDUS41 KOKX\nCLINYC\nTHE CENTRAL PARK NY CLIMATE SUMMARY FOR NOVEMBER 26 2026\n"
        f"{asof}\nTEMPERATURE (F)\n {coverage}\n  MAXIMUM        79     113 PM\n",
    }


def nbm(run):
    leads = list(range(5, 72, 3))
    hours = [(run + timedelta(hours=i)).hour for i in leads]
    return (
        f" KNYC    NBM  NBS GUIDANCE    {run:%m/%d/%Y}  {run:%H}00 UTC\n"
        + " UTC "
        + " ".join(map(str, hours))
        + "\n FHR "
        + " ".join(map(str, leads))
        + "\n TMP "
        + " ".join("77" for _ in leads)
        + "\n\n"
    )


class SyntheticSources:
    def __init__(self):
        self.now = NOW
        self.calls = []
        self.markets = [market()]
        self.settled = []
        self.depth = copy.deepcopy(FIXTURE["book"])
        self.book_age = 0
        self.lookup = {}
        self.block = None
        self.overrides = []
        obs = NOW - timedelta(minutes=9)
        self.aviation = [
            {
                "icaoId": "KNYC",
                "metarType": "SPECI",
                "obsTime": int(obs.timestamp()),
                "reportTime": w.iso(obs),
                "receiptTime": w.iso(obs + timedelta(seconds=73)),
                "rawOb": f"SPECI KNYC {obs:%d%H%M}Z AUTO 01012G22KT 10SM 07/02 A3001",
                "temp": 7.2,
                "wspd": 12,
                "wgst": 22,
            }
        ]

    def __call__(self, url, **options):
        self.calls.append(url)
        if self.block and self.block in url:
            raise f.Unavailable("HTTP 429", 429, 65)
        n = self.now
        if url == f.POINT:
            data = {
                "properties": {
                    "gridId": "OKX",
                    "gridX": 34,
                    "gridY": 45,
                    "timeZone": "America/New_York",
                    "forecast": f.NWS + "/gridpoints/OKX/34,45/forecast",
                }
            }
        elif url.endswith("/34,45/forecast"):
            data = {
                "properties": {
                    "updateTime": w.iso(n - timedelta(hours=3)),
                    "generatedAt": w.iso(n - timedelta(hours=2)),
                    "periods": [
                        {
                            "startTime": "2026-11-28T06:00:00-05:00",
                            "endTime": "2026-11-28T18:00:00-05:00",
                            "isDaytime": True,
                            "temperature": 78,
                            "temperatureUnit": "F",
                        }
                    ],
                }
            }
        elif url == f.OBS:
            data = {
                "properties": {
                    "station": f.NWS + "/stations/KNYC",
                    "timestamp": w.iso(n - timedelta(minutes=9)),
                    "temperature": {
                        "unitCode": "wmoUnit:degC",
                        "value": 7.2,
                        "qualityControl": "V",
                    },
                }
            }
        elif url == f.METAR:
            if not self.aviation:
                return f.Reply(b"", n, n, None, status=204)
            data = self.aviation
        elif url == f.CLIS:
            data = {
                "@graph": [
                    {"id": x, "issuingOffice": "KOKX"} for x in FIXTURE["product_ids"]
                ]
            }
        elif url.startswith(f.NWS + "/products/"):
            data = cli(url.endswith("000001"))
        elif url.startswith(f.NBM):
            run = datetime.strptime(url.split("/blend.")[1][:11], "%Y%m%d/%H").replace(
                tzinfo=w.UTC
            )
            return f.Reply(nbm(run).encode(), n, n, run + timedelta(hours=1))
        elif url == f.TERMS:
            return f.Reply(FIXTURE["certificate"].encode(), n, n, n - timedelta(days=6))
        elif url == f.SERIES:
            data = {
                "series": {
                    "ticker": "KXHIGHNY",
                    "contract_terms_url": f.TERMS,
                    "settlement_sources": [
                        {
                            "name": "The Weather Company",
                            "url": "https://weather.com/kalshi",
                        }
                    ],
                    "last_updated_ts": w.iso(n - timedelta(days=1)),
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                }
            }
        elif url == f.SERIES_FEES:
            data = {"series_fee_change_arr": []}
        elif "/events/fee_changes" in url:
            data = {"event_fee_changes": self.overrides, "cursor": ""}
        elif url == f.OPEN:
            data = {"markets": self.markets, "cursor": ""}
        elif url == f.RECENT:
            data = {"markets": self.settled, "cursor": ""}
        elif "/orderbook?" in url:
            return f.Reply(
                json.dumps(self.depth).encode(),
                n,
                n - timedelta(seconds=self.book_age),
                None,
            )
        elif url in self.lookup:
            data = self.lookup[url]
        else:
            raise f.Unavailable("HTTP 404", 404)
        return f.Reply(json.dumps(data).encode(), n, n, None)


class SourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.src = SyntheticSources()
        patchers = (
            patch.object(f, "download", side_effect=self.src),
            patch.object(f, "clock", lambda: self.src.now),
            patch.object(
                f,
                "TERMS_SHA",
                hashlib.sha256(FIXTURE["certificate"].encode()).hexdigest(),
            ),
            patch.object(
                f,
                "SECONDARY_SHA",
                hashlib.sha256(FIXTURE["secondary"].lower().encode()).hexdigest(),
            ),
        )
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def poll(self, allow=True):
        return f.collect(self.path, w.iso(self.src.now), allow)

    def test_private_mode_reads_only_noaa_and_preserves_two_bulletins(self):
        first = self.poll(False)
        self.assertFalse(first["packets"])
        self.assertFalse(first["settlements"])
        self.assertTrue(
            all(u.startswith((f.NBM, f.NWS)) or u == f.METAR for u in self.src.calls)
        )
        obs = next(x for x in first["evidence"] if x["kind"] == "observation")
        self.assertIsNone(obs["published_at"])
        with closing(f.database(self.path)) as db:
            reports = db.execute(
                "SELECT kind,value,first_seen,issued FROM weather_records WHERE model='CLINYC' ORDER BY issued"
            ).fetchall()
            self.assertEqual(
                [(r["kind"], r["value"]) for r in reports],
                [("preliminary", 79), ("complete", 79)],
            )
            self.assertTrue(all(r["first_seen"] == w.iso(NOW) for r in reports))
            self.assertFalse(w.records(db, NOW - timedelta(minutes=1)))
        self.src.now += timedelta(minutes=1)
        self.poll(False)
        with closing(f.database(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM weather_records WHERE model='CLINYC'"
                ).fetchone()[0],
                2,
            )

    def test_aviation_relay_preserves_measurement_availability_and_same_station(self):
        first = self.poll()
        aviation = next(v for v in first["evidence"] if v.get("station") == "KNYC")
        self.assertEqual(aviation["issued_at"], self.src.aviation[0]["reportTime"])
        self.assertEqual(
            aviation["source_received_at"], self.src.aviation[0]["receiptTime"]
        )
        self.assertIsNone(aviation["published_at"])
        self.assertEqual(aviation["available_at"], w.iso(NOW))
        self.assertIn("same KNYC sensor", aviation["detail"])
        self.assertIn("gusting to 22 kt", aviation["detail"])
        self.assertTrue(aviation["context_only"])
        packet = first["packets"][0]
        self.assertIn(aviation, packet["evidence"])
        self.assertNotEqual(aviation["event_date"], packet["event_date"])
        self.src.now += timedelta(seconds=30)
        self.poll()
        self.assertEqual(self.src.calls.count(f.METAR), 1)
        self.src.aviation[0]["receiptTime"] = w.iso(self.src.now)
        self.src.now += timedelta(minutes=5)
        self.poll()
        with closing(f.database(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM weather_records WHERE model=?", (w.AWC_MODEL,)
                ).fetchone()[0],
                1,
            )
            self.assertFalse(
                any(e.get("station") for e in w.records(db, NOW - timedelta(seconds=1)))
            )
        self.assertEqual(self.src.calls.count(f.METAR), 2)

    def test_missing_or_late_aviation_context_does_not_stop_other_feeds(self):
        self.src.aviation.clear()
        optional = self.poll()
        self.assertIn("Optional NOAA aviation report unavailable", optional["status"])
        self.assertFalse(optional["degraded"])
        self.assertTrue(optional["packets"][0]["book_executable"])
        self.assertTrue(any(v["kind"] == "forecast" for v in optional["evidence"]))
        self.assertFalse(any(v.get("station") for v in optional["evidence"]))

    def test_aviation_special_or_routine_validation_and_soft_failure(self):
        item = self.src.aviation[0]
        with closing(f.database(self.path)) as db:
            for update in (
                {"icaoId": "KLGA"},
                {"obsTime": int(NOW.timestamp()) + 10},
                {"receiptTime": w.iso(NOW + timedelta(minutes=1))},
                {"receiptTime": "bad time"},
                {"obsTime": int((NOW - timedelta(hours=4)).timestamp())},
                {"rawOb": "METAR KLGA 271851Z AUTO"},
                {"temp": float("nan")},
            ):
                with self.subTest(update=update), self.assertRaises(ValueError):
                    w.read_metar(db, {**item, **update}, f.METAR, NOW)
            routine = {
                **item,
                "metarType": "METAR",
                "rawOb": item["rawOb"].replace("SPECI ", ""),
                "reportTime": w.iso(NOW + timedelta(minutes=8)),
            }
            w.read_metar(db, routine, f.METAR, NOW)
            self.assertIn("Routine METAR", w.records(db, NOW)[0]["detail"])
        self.src.block = "aviationweather.gov"
        first = self.poll()
        self.assertFalse(first["degraded"])
        self.assertIn("HTTP 429", first["status"])
        self.assertEqual(len(first["packets"]), 1)

    def test_source_to_book_saves_independent_null_forecast_and_exact_cutoff(self):
        run = self.poll()
        self.assertNotIn("degraded", run["status"])
        (packet,) = run["packets"]
        self.assertEqual(packet["close_at"], "2026-11-29T04:59:00.000000Z")
        self.assertEqual(packet["forecast"]["source"], w.NBM_MODEL)
        self.assertEqual(packet["forecast"]["samples"], 0)
        self.assertIsNone(packet["forecast"]["probability"])
        self.assertEqual(
            packet["levels"], {"yes": [[48, 1], [51, 19]], "no": [[60, 4], [97, 99]]}
        )
        self.assertEqual(packet["market_baseline"], 0.44)
        self.assertEqual(packet["fee_rate"], "0.07")
        self.assertFalse(packet["tradable"])
        self.assertTrue(packet["book_executable"])
        orig_book_stamp = packet["observed_at"]
        self.src.now += timedelta(minutes=2)
        self.src.book_age = 601
        (later,) = self.poll()["packets"]
        self.assertEqual(later["observed_at"], orig_book_stamp)
        self.assertFalse(later["tradable"])
        self.assertFalse(later["book_executable"])
        self.assertTrue(any("last captured" in s for s in later["missing"]))

    def test_missing_book_and_stale_caller_never_produce_new_quote(self):
        self.src.book_age = 40
        fresh = self.poll()["packets"][0]
        self.assertIsNone(fresh["observed_at"])
        self.assertFalse(fresh["book_executable"])
        self.assertEqual(fresh["levels"], {"yes": [], "no": []})
        old_count = len(self.src.calls)
        old = f.collect(self.path, w.iso(NOW - timedelta(hours=3)), True)
        self.assertFalse(old["packets"])
        self.assertEqual(len(self.src.calls), old_count)

    def test_failed_source_backs_off_but_other_providers_continue(self):
        self.src.block = f.POINT
        first = self.poll()
        self.assertIn("HTTP 429", first["status"])
        self.assertEqual(len(first["packets"]), 1)
        self.assertTrue(any(u.startswith(f.NBM) for u in self.src.calls))
        self.assertNotIn(f.OBS, self.src.calls)
        self.src.calls.clear()
        self.src.now += timedelta(seconds=30)
        self.poll()
        self.assertFalse(any(u.startswith(f.NWS) for u in self.src.calls))
        self.src.now += timedelta(seconds=40)
        self.src.block = None
        self.poll()
        self.assertIn(f.OBS, self.src.calls)

    def test_revoking_permission_in_flight_discards_the_whole_venue_batch(self):
        self.src.markets.append(market(strike="less"))
        self.src.settled = [
            {
                **market(date(2026, 11, 26)),
                "result": "yes",
                "status": "finalized",
                "expiration_value": "77",
                "settlement_value_dollars": "1.00",
                "settlement_ts": "2026-11-27T12:08:00Z",
            }
        ]
        permitted = True

        def revoke_in_flight(url, **options):
            nonlocal permitted
            reply = self.src(url, **options)
            if "/orderbook?" in url:
                permitted = False
            return reply

        with patch.object(f, "download", side_effect=revoke_in_flight):
            result = f.collect(
                self.path, w.iso(NOW), True, permission_check=lambda: permitted
            )
        self.assertIn("discarded", result["status"])
        self.assertFalse(result["packets"])
        self.assertFalse(result["settlements"])
        self.assertFalse(
            any(item.get("kind") == "settlement" for item in result["evidence"])
        )
        self.assertEqual(sum("/orderbook?" in url for url in self.src.calls), 1)
        self.assertIn("/orderbook?", self.src.calls[-1])
        with closing(f.database(self.path)) as db:
            for table in (
                "venue_highs",
                "market_registry",
                "estimates",
                "books",
                "packets",
                "exceptional_results",
            ):
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0
                )
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM cache WHERE name LIKE '%kalshi%' OR name='certified-terms'"
                ).fetchone()
            )
            self.assertTrue(db.execute("SELECT 1 FROM weather_records").fetchone())

    def test_final_permission_check_and_commit_share_the_owner_settings_lock(self):
        self.poll()
        self.src.now += timedelta(minutes=5)
        self.src.depth["orderbook_fp"]["no_dollars"] = [["0.5000", "25.00"]]
        generation = 1

        @contextmanager
        def owner_lock():
            nonlocal generation
            # Off then on while this collection awaited the commit lock.
            generation += 2
            yield

        result = f.collect(
            self.path,
            w.iso(self.src.now),
            True,
            permission_check=lambda: generation == 1,
            permission_lock=owner_lock(),
        )
        self.assertFalse(result["packets"])
        with closing(f.database(self.path)) as db:
            row = db.execute(
                "SELECT seen,levels FROM books WHERE id=?", (FIXTURE["market"],)
            ).fetchone()
            self.assertEqual(row["seen"], w.iso(NOW))
            self.assertEqual(json.loads(row["levels"])["yes"][0], [48, 1])
            self.assertEqual(
                db.execute("SELECT count(*) FROM packets").fetchone()[0], 1
            )

    def test_archive_reconciles_disappeared_markets_after_restart(self):
        packet = self.poll()["packets"][0]
        self.src.now = w.instant("2026-12-04T20:00:00Z")
        final = {
            **self.src.markets[0],
            "result": "yes",
            "status": "finalized",
            "expiration_value": "78.00",
            "settlement_value_dollars": "1.0000",
            "settlement_ts": "2026-11-29T12:08:00Z",
        }
        self.src.markets = []
        url = f.API + "/historical/markets/" + packet["id"]
        self.src.lookup[url] = {"market": final}
        (item,) = self.poll()["settlements"]
        self.assertEqual(item["source"], url)
        self.assertEqual(item["observed_at"], w.iso(self.src.now))
        self.assertEqual(item["result"], "yes")
        self.assertEqual(self.poll()["settlements"], [item])
        with closing(f.database(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT high FROM venue_highs").fetchall()[0][0], 78
            )

    def test_no_weather_report_or_scalar_can_settle_binary(self):
        self.src.settled = [
            {
                **market(date(2026, 11, 26)),
                "result": "scalar",
                "status": "finalized",
                "settlement_value_dollars": "0.4360",
                "settlement_ts": "2026-11-27T12:08:00Z",
            }
        ]
        result = self.poll()
        self.assertTrue(any(item["kind"] == "complete" for item in result["evidence"]))
        self.assertFalse(result["settlements"])
        self.assertIn("exceptional payout", result["status"])
        with closing(f.database(self.path)) as db:
            self.assertFalse(db.execute("SELECT * FROM venue_highs").fetchall())
            self.assertEqual(
                db.execute("SELECT payout FROM exceptional_results").fetchone()[0],
                "0.4360",
            )
        self.src.settled = []
        self.src.now += timedelta(minutes=20)
        self.assertTrue(
            any(e["kind"] == "exceptional_settlement" for e in self.poll()["evidence"])
        )

    def test_changed_rules_override_fee_and_crossed_book(self):
        self.src.markets[0]["rules_secondary"] += " A new adjustment applies."
        self.src.overrides = [
            {
                "scheduled_ts": "2026-11-25T00:00:00Z",
                "fee_type_override": "quadratic",
                "fee_multiplier_override": 2,
                "event_ticker": FIXTURE["event"],
                "series_ticker": "KXHIGHNY",
            }
        ]
        out = self.poll()["packets"][0]
        self.assertEqual(out["fee_rate"], "0.14")
        self.assertTrue(any("settlement terms" in x for x in out["missing"]))
        self.src.now += timedelta(minutes=2)
        self.src.overrides[0]["fee_type_override"] = "flat"
        self.src.depth["orderbook_fp"]["yes_dollars"] = [["0.9800", "6.00"]]
        out = self.poll()["packets"][0]
        self.assertIsNone(out["fee_rate"])
        self.assertNotEqual(out["observed_at"], w.iso(self.src.now))

    def test_forecast_outage_preserves_rules_and_venue_settlements(self):
        self.src.block = f.NWS
        self.src.settled = [
            {
                **market(date(2026, 11, 26)),
                "result": "yes",
                "status": "finalized",
                "expiration_value": "77",
                "settlement_value_dollars": "1.00",
                "settlement_ts": "2026-11-27T12:08:00Z",
            }
        ]
        result = self.poll()
        self.assertEqual(result["settlements"][0]["result"], "yes")
        self.assertIn("degraded", result["status"])
        self.assertEqual(result["packets"][0]["forecast"]["source"], w.NBM_MODEL)

    def test_event_types_and_windows_fallback_for_windows(self):
        for key, want in (
            ("between", (77, 78)),
            ("less", (None, 76)),
            ("greater", (79, None)),
        ):
            info = f.rule_data(market(strike=key), True)
            self.assertEqual(info[2], want)
            self.assertEqual(info[-1], [])
        cold = market(strike="less")
        cold["ticker"] = FIXTURE["event"] + "-T-3"
        cold["cap_strike"] = -3
        cold["rules_primary"] = cold["rules_primary"].replace("77°", "-3°")
        self.assertEqual(f.rule_data(cold, True)[2], (None, -4))
        with patch.object(w, "EASTERN", None):
            self.assertEqual(
                w.event_end(date(2026, 3, 8)) - w.midnight(date(2026, 3, 8)),
                timedelta(hours=23),
            )
            self.assertEqual(
                w.event_end(date(2026, 11, 1)) - w.midnight(date(2026, 11, 1)),
                timedelta(hours=25),
            )

    def test_ambiguous_or_corrupt_products_are_rejected(self):
        with closing(f.database(self.path)) as db:
            product = cli(False)
            product["productText"] = product["productText"].replace("CLINYC", "CLILGA")
            with self.assertRaises(ValueError):
                w.read_cli(db, product, f.NWS, NOW)
            with self.assertRaises(ValueError):
                w.read_cli(db, cli(False), f.NWS, w.instant("2026-11-27T03:00:00Z"))
            run = NOW.replace(hour=13, minute=0)
            # The first target day's seven preceding hours predate this run.
            # Remove a slot on the next target day to prove partial cards abstain.
            complete = nbm(run)
            hourly = complete.split(" TMP ")[1].split()
            hourly[6] = "999"
            incomplete = (
                complete.split(" TMP ")[0] + " TMP " + " ".join(hourly) + "\n\n"
            )
            w.read_nbm(db, incomplete, f.NBM, run, run, NOW)
            days = [r[0] for r in db.execute("SELECT event_day FROM weather_records")]
            self.assertNotIn("2026-11-27", days)
            self.assertNotIn("2026-11-28", days)
            self.assertIn("2026-11-29", days)
            with self.assertRaises(ValueError):
                w.read_nbm(db, nbm(run).replace("KNYC", "KLGA"), f.NBM, run, run, NOW)

    def test_model_uses_distinct_prior_days_and_historical_receipt_cutoffs(self):
        with closing(f.database(self.path)) as db:
            day = date(2026, 11, 28)
            asof = NOW
            issued = NOW - timedelta(hours=4)

            def forecast(d, iss, seen, value, tag="A"):
                w.save(
                    db,
                    "forecast",
                    d,
                    w.POINT_MODEL,
                    iss,
                    seen,
                    f.NWS,
                    value,
                    "Synthetic",
                    tag,
                )

            forecast(day, issued, asof, 77)
            for i in range(1, 33):
                d = day - timedelta(days=i)
                old_clock = w.event_end(d) - (w.event_end(day) - asof)
                old_issue = w.event_end(d) - (w.event_end(day) - issued)
                # One distinct station day per outcome; duplicate forecasts give no extra weight.
                forecast(d, old_issue, old_clock - timedelta(minutes=1), 77, "A")
                forecast(d, old_issue, old_clock - timedelta(minutes=1), 77, "B")
                actual = 78 if i % 2 else 79
                first_seen = (
                    asof + timedelta(hours=2)
                    if i == 1
                    else old_clock + timedelta(days=1)
                )
                db.execute(
                    "INSERT INTO venue_highs VALUES(?,?,?,?,?,?)",
                    (
                        str(d),
                        w.AUTHORITY,
                        actual,
                        w.iso(first_seen),
                        w.iso(first_seen),
                        f.API,
                    ),
                )
                # A later revision from that day was not available at its matched decision clock.
                forecast(
                    d,
                    old_issue + timedelta(minutes=1),
                    old_clock + timedelta(minutes=1),
                    115,
                    "late",
                )
            est, gap = w.estimate(db, day, (77, 78), asof)
            self.assertIsNone(gap)
            self.assertEqual(est["samples"], 31)
            self.assertAlmostEqual(est["probability"], 15 / 31)
            self.assertLess(est["lower"], est["probability"])
            self.assertLess(est["probability"], est["upper"])
            parts = [
                w.estimate(db, day, b, asof)[0]["probability"]
                for b in [(None, 76), (77, 78), (79, None)]
            ]
            self.assertAlmostEqual(sum(parts), 1)
            db.execute(
                "UPDATE weather_records SET first_seen=? WHERE event_day<? AND kind='forecast'",
                (w.iso(asof), str(day)),
            )
            est, gap = w.estimate(db, day, (77, 78), asof)
            self.assertEqual(est["samples"], 0)
            self.assertIsNone(est["probability"])
            self.assertIn("Calibration", gap)

    def test_mature_forward_packet_passes_ledger_input_checks_and_expires_on_fee_change(
        self,
    ):
        from worker.ledger import _inputs

        day = date(2026, 11, 28)
        run = NOW.replace(hour=13, minute=0)
        with closing(f.database(self.path)) as db, db:
            for offset in range(2, 34):
                past = day - timedelta(days=offset)
                old_run = w.event_end(past) - (w.event_end(day) - run)
                w.save(
                    db,
                    "forecast",
                    past,
                    w.NBM_MODEL,
                    old_run,
                    old_run + timedelta(hours=1),
                    f.NBM,
                    77,
                    "Synthetic KNYC guidance.",
                    "Synthetic unversioned KNYC station card.",
                )
                resolved = w.event_end(past) + timedelta(hours=7)
                db.execute(
                    "INSERT INTO venue_highs VALUES(?,?,?,?,?,?)",
                    (
                        str(past),
                        w.AUTHORITY,
                        78,
                        w.iso(resolved),
                        w.iso(resolved),
                        f.API,
                    ),
                )
        change = NOW + timedelta(seconds=15)
        self.src.overrides = [
            {
                "scheduled_ts": w.iso(change),
                "fee_type_override": "flat",
                "fee_multiplier_override": 2,
                "event_ticker": FIXTURE["event"],
                "series_ticker": "KXHIGHNY",
            }
        ]
        packet = self.poll()["packets"][0]
        self.assertEqual(packet["missing"], [])
        self.assertTrue(packet["tradable"])
        self.assertEqual(packet["forecast"]["samples"], 32)
        self.assertEqual(packet["fee_valid_until"], w.iso(change))
        _inputs(packet, NOW)
        self.src.now += timedelta(seconds=21)
        next_packet = self.poll()["packets"][0]
        self.assertFalse(next_packet["tradable"])
        self.assertIsNone(next_packet["fee_rate"])


if __name__ == "__main__":
    unittest.main()
