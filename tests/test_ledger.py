"""Safety and recovery checks against the public paper-account interface."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from worker.ledger import Ledger
from worker.replay import replay_demo


WHEN = datetime(2026, 11, 25, 18, tzinfo=timezone.utc)


def time_at(seconds=0):
    return (WHEN + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def packet(market="day-A", event="day", tick=0):
    return {
        "id": market,
        "event_id": event,
        "title": "Central Park daily high",
        "label": "44 to 45°F",
        "station": "KNYC",
        "event_date": "2026-11-25",
        "close_at": time_at(86400 * 8),
        "as_of": time_at(tick),
        "observed_at": time_at(tick),
        "book_source": "fixture",
        "levels": {"yes": [[45, 35], [47, 85]], "no": [[56, 100]]},
        "forecast": {
            "probability": 0.66,
            "lower": 0.61,
            "upper": 0.71,
            "baseline": 0.28,
            "model": "KNYC empirical calibration",
            "samples": 120,
            "issued_at": time_at(-3600),
        },
        "rules": "Inclusive daily maximum from final venue determination.",
        "rule_version": "v1",
        "evidence": [
            {
                "title": "Synthetic issued forecast",
                "url": "fixture://issued",
                "published_at": time_at(-3600),
                "available_at": time_at(-3597),
                "retrieved_at": time_at(-3596),
                "detail": "Central Park KNYC.",
            }
        ],
        "fee_rate": "0.07",
        "tradable": True,
        "missing": [],
    }


def expensive(market, event):
    p = packet(market, event)
    p["forecast"].update(probability=0.985, lower=0.975, upper=0.995)
    p["levels"] = {"yes": [[90, 100]], "no": [[11, 100]]}
    return p


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "account.sqlite"
        self.ledger = self.open()

    def open(self, path=None, initial=1_000_000):
        ledger = Ledger(self.path if path is None else path, initial)
        self.addCleanup(ledger.close)
        return ledger

    def test_yes_depth_expected_dollars_and_accounting(self):
        p = packet()
        self.ledger.ingest([p], time_at())
        state = self.ledger.state()
        market = state["markets"][0]
        self.assertEqual(
            (market["decision"], market["side"], market["qty"]),
            ("Paper buy", "YES", 100),
        )
        self.assertEqual(market["premium_cents"], 35 * 45 + 65 * 47)
        self.assertEqual(market["fee_cents"], 174)
        self.assertEqual(market["net_cents"], 6100 - 4630 - 174 - 100)
        self.assertEqual(market["worst_acceptable_ask_cents"], 58)
        self.assertEqual(market["market_baseline"], 0.445)
        portfolio = state["portfolio"]
        self.assertEqual(portfolio["cash_cents"], 1_000_000 - 4804)
        self.assertEqual(portfolio["reserved_cents"], 0)
        self.assertEqual(portfolio["positions"][0]["marked_qty"], 100)
        self.assertEqual(
            portfolio["equity_cents"],
            portfolio["cash_cents"] + portfolio["open_value_cents"],
        )
        self.ledger.ingest([p], time_at())
        self.assertEqual(state, self.ledger.state())

    def test_no_uses_own_ladder_and_complements_upper_bound(self):
        p = packet()
        p["forecast"].update(probability=0.31, lower=0.23, upper=0.41)
        p["levels"] = {"yes": [[62, 200]], "no": [[40, 20], [43, 100]]}
        self.ledger.ingest([p], time_at())
        m = self.ledger.state()["markets"][0]
        self.assertEqual(
            (m["side"], m["conservative_probability"], m["avg_ask_cents"]),
            ("NO", 0.59, 42.4),
        )
        self.assertEqual(
            m["net_cents"],
            5900 - m["premium_cents"] - m["fee_cents"] - m["allowance_cents"],
        )

    def test_economic_size_can_be_smaller_than_visible_depth(self):
        p = packet()
        p["levels"] = {"yes": [[21, 3], [85, 180]], "no": [[80, 100]]}
        self.ledger.ingest([p], time_at())
        m = self.ledger.state()["markets"][0]
        self.assertEqual(
            (m["qty"], m["avg_ask_cents"], m["available_qty"]), (3, 21, 183)
        )

    def test_bad_clocks_uncalibrated_inputs_and_unknown_fees_stay_watch(self):
        cases = []
        for key, value in (
            ("observed_at", time_at(-121)),
            ("observed_at", time_at(1)),
            ("as_of", time_at(1)),
            ("as_of", None),
            ("close_at", time_at()),
            ("fee_rate", None),
            ("fee_rate", "Infinity"),
            ("fee_model", "unrecognized"),
            ("fee_valid_until", time_at()),
            ("levels", {"yes": [[42, 100]], "no": [[48, 2]]}),
            ("levels", {"yes": [[42, 100]], "no": [[58, 2]]}),
            ("levels", {"yes": [[42, 20], [42, 20]], "no": [[59, 2]]}),
            ("levels", {"yes": [[42.5, 100]], "no": [[59, 2]]}),
            ("levels", {"yes": [], "no": [[59, 2]]}),
            ("tradable", False),
            ("rule_version", ""),
        ):
            p = packet()
            p[key] = value
            cases.append(p)
        for key, value in (
            ("samples", 0),
            ("samples", 29),
            ("upper", 0.4),
            ("lower", None),
            ("issued_at", time_at(-49 * 3600)),
            ("available_at", time_at(2)),
            ("estimated_at", time_at(3)),
        ):
            p = packet()
            p["forecast"][key] = value
            cases.append(p)
        for field in ("published_at", "available_at", "retrieved_at"):
            p = packet()
            p["evidence"][0][field] = time_at(1)
            cases.append(p)
        for idx, p in enumerate(cases):
            p["id"] = f"m{idx}"
        self.ledger.ingest(cases, time_at())
        state = self.ledger.state()
        self.assertEqual(
            [
                (m["id"], m["decision"])
                for m in state["markets"]
                if m["decision"] != "Watch"
            ],
            [],
        )
        self.assertEqual(state["portfolio"]["cash_cents"], 1_000_000)
        self.assertEqual(state["portfolio"]["orders"], [])

    def test_zero_rate_is_a_known_fee_and_context_receipt_preserves_unknown_publication(
        self,
    ):
        p = packet()
        p["fee_rate"] = "0.00"
        p["evidence"].append(
            {
                "title": "Station reading",
                "url": "fixture://reading",
                "kind": "observation",
                "published_at": None,
                "issued_at": time_at(-50),
                "available_at": time_at(-40),
            }
        )
        self.ledger.ingest([p], time_at())
        m = self.ledger.state()["markets"][0]
        self.assertEqual((m["decision"], m["fee_cents"]), ("Paper buy", 0))
        self.assertIsNone(m["evidence"][-1]["published_at"])

    def test_duplicate_partial_delivery_restart_and_completion_match_one_fill(self):
        p = packet()
        self.ledger.ingest([p], time_at(), auto_fill=False)
        original = self.ledger.state()
        intent_id = original["markets"][0]["intent_id"]
        self.assertEqual(original["portfolio"]["reserved_cents"], 4904)
        self.assertEqual(original["portfolio"]["filled_orders"], 0)
        self.assertTrue(self.ledger.apply_fill(intent_id, 20, time_at(1))["applied"])
        partial = self.ledger.state()["portfolio"]
        self.assertEqual(
            (partial["positions"][0]["qty"], partial["orders"][0]["outstanding_qty"]),
            (20, 80),
        )
        self.assertGreater(partial["reserved_cents"], 0)
        self.assertEqual((partial["filled_orders"], partial["settled_orders"]), (1, 0))
        self.assertEqual(partial["net_pnl_cents"], partial["equity_cents"] - 1_000_000)

        restarted = self.open()
        self.assertEqual(restarted.state()["portfolio"], partial)
        self.assertFalse(restarted.apply_fill(intent_id, 20, time_at(2))["applied"])
        self.assertFalse(restarted.apply_fill(intent_id, 5, time_at(2))["applied"])
        restarted.apply_fill(intent_id, 47, time_at(3))
        with self.assertRaises(ValueError):
            restarted.apply_fill(intent_id, 101, time_at(3))
        restarted.apply_fill(intent_id, 100, time_at(4))
        one = self.open(Path(self.directory.name) / "single.sqlite")
        one.ingest([p], time_at())
        for key in (
            "cash_cents",
            "reserved_cents",
            "fees_cents",
            "open_cost_cents",
            "equity_cents",
            "net_pnl_cents",
            "filled_orders",
            "settled_orders",
        ):
            self.assertEqual(
                restarted.state()["portfolio"][key], one.state()["portfolio"][key]
            )
        self.assertEqual(
            sum(e["qty"] for e in restarted.state()["activity"] if e["kind"] == "fill"),
            100,
        )

    def test_unknown_blocks_conflict_and_defers_settlement_until_receipt(self):
        p = packet()
        self.ledger.ingest([p], time_at(), auto_fill=False)
        iid = self.ledger.state()["markets"][0]["intent_id"]
        self.ledger.apply_fill(iid, 20, time_at(1))
        self.ledger.mark_unknown(iid, time_at(2))
        newer = packet(tick=3)
        self.ledger.ingest([newer], time_at(3))
        self.ledger.set_paused(True, time_at(4))
        s = self.ledger.state()
        self.assertTrue(s["portfolio"]["orders"][0]["cancel_requested"])
        self.assertEqual([a["kind"] for a in s["activity"]].count("intent"), 1)
        self.assertEqual(s["markets"][0]["decision"], "Held")
        close = 8 * 86400
        receipt = self.ledger.settle(
            p["id"], "YES", time_at(close), "fixture:venue-final"
        )
        self.assertEqual(receipt["status"], "pending")
        pending = self.ledger.state()["portfolio"]
        self.assertGreater(pending["reserved_cents"], 0)
        self.assertEqual((pending["filled_orders"], pending["settled_orders"]), (1, 0))
        restarted = self.open()
        restarted.reconcile_intent(iid, 35, time_at(close + 1), terminal=True)
        s = restarted.state()
        self.assertTrue(s["paused"])
        self.assertEqual(s["portfolio"]["reserved_cents"], 0)
        self.assertEqual(s["markets"][0]["settlement"]["payout_cents"], 3500)
        self.assertEqual(s["portfolio"]["positions"], [])
        self.assertEqual(
            (s["portfolio"]["filled_orders"], s["portfolio"]["settled_orders"]), (1, 1)
        )
        receipt = restarted.settle(p["id"], "yes", time_at(close + 2), "fixture")
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(s, restarted.state())
        with self.assertRaises(ValueError):
            restarted.settle(p["id"], "no", time_at(close + 2), "fixture")

    def test_entry_pause_cancels_known_remainder_and_allows_settlement(self):
        p = packet()
        self.ledger.ingest([p], time_at(), auto_fill=False)
        iid = self.ledger.state()["markets"][0]["intent_id"]
        self.ledger.apply_fill(iid, 10, time_at(1))
        self.ledger.set_paused(True, time_at(2))
        self.assertEqual(self.ledger.state()["portfolio"]["reserved_cents"], 0)
        self.assertEqual(self.open().state()["portfolio"]["positions"][0]["qty"], 10)
        with self.assertRaises(ValueError):
            self.ledger.apply_fill(iid, 12, time_at(3))
        with self.assertRaises(ValueError):
            self.ledger.settle(p["id"], "yes", time_at(4), "fixture")
        with self.assertRaises(ValueError):
            self.ledger.settle(
                p["id"], "yes", time_at(8 * 86400), "NWS complete bulletin"
            )
        self.ledger.settle(p["id"], "void", time_at(3), "fixture")
        port = self.ledger.state()["portfolio"]
        self.assertEqual(port["realized_cents"], -port["fees_cents"])
        self.assertEqual((port["filled_orders"], port["settled_orders"]), (1, 1))
        self.assertEqual(port["net_pnl_cents"], port["realized_cents"])
        self.assertEqual(port["cash_cents"], 1_000_000 - port["fees_cents"])
        self.assertTrue(self.ledger.state()["paused"])

    def test_no_settles_a_winning_no_position_and_losing_yes_position(self):
        a, b = packet("A"), packet("B")
        b["forecast"].update(probability=0.21, lower=0.15, upper=0.25)
        self.ledger.ingest([a, b], time_at())
        cash = self.ledger.state()["portfolio"]["cash_cents"]
        self.ledger.settle("A", "no", time_at(8 * 86400), "fixture")
        self.ledger.settle("B", "no", time_at(8 * 86400), "fixture")
        s = self.ledger.state()
        self.assertEqual(s["portfolio"]["cash_cents"], cash + 100 * 100)
        self.assertEqual(
            s["portfolio"]["cash_cents"], 1_000_000 + s["portfolio"]["realized_cents"]
        )

    def test_clock_regression_old_snapshots_and_same_time_conflict_do_not_reorder(self):
        p = packet(tick=15)
        p["fee_rate"] = None
        self.ledger.ingest([p], time_at(15))
        self.ledger.ingest([packet(tick=1)], time_at(1))
        self.ledger.ingest([packet(tick=5)], time_at(20))
        self.assertEqual(self.ledger.state()["markets"][0]["as_of"], time_at(15))
        conflict = packet(tick=15)
        conflict["levels"]["yes"] = [[42, 100]]
        self.ledger.ingest([conflict], time_at(20))
        self.assertIn("Conflicting", self.ledger.state()["markets"][0]["reason"])
        self.ledger.ingest([packet(tick=21)], time_at(21))
        self.assertEqual(self.ledger.state()["markets"][0]["decision"], "Paper buy")

    def test_mixed_sources_refused_and_atomic_batch_rolls_back_on_fault(self):
        p = packet()
        wrong = {**p, "book_source": "kalshi"}
        with self.assertRaises(ValueError):
            self.ledger.ingest([p, wrong], time_at())
        with patch.object(
            self.ledger, "_fill", side_effect=RuntimeError("synthetic interruption")
        ):
            with self.assertRaises(RuntimeError):
                self.ledger.ingest([p], time_at())
        s = self.ledger.state()
        self.assertEqual(
            (s["account_source"], s["markets"], s["activity"]), (None, [], [])
        )
        self.ledger.ingest([p], time_at())
        with self.assertRaises(ValueError):
            self.ledger.ingest([wrong], time_at())
        self.assertEqual(len(self.ledger.state()["markets"]), 1)

    def test_budget_and_gross_event_portfolio_caps_include_unfilled_reservations(self):
        small = self.open(Path(self.directory.name) / "small.sqlite", initial=104)
        small.ingest([packet()], time_at(), auto_fill=False)
        port = small.state()["portfolio"]
        self.assertGreater(port["reserved_cents"], 0)
        self.assertLessEqual(port["reserved_cents"], 104)
        self.assertEqual(port["available_cash_cents"] + port["reserved_cents"], 104)
        markets = [
            expensive(f"{e}-{i}", e) for e in ("one", "two", "three") for i in range(4)
        ]
        self.ledger.ingest(markets, time_at(), auto_fill=False)
        port = self.ledger.state()["portfolio"]
        self.assertLessEqual(port["max_loss_cents"], 50_000)
        self.assertGreater(port["reserved_cents"], 49_800)
        self.assertTrue(
            all(e["charged_exposure_cents"] <= 20_000 for e in port["events"])
        )
        self.assertTrue(all(row["reserved_cents"] <= 10_000 for row in port["orders"]))
        self.assertTrue(all(row["requested_qty"] <= 100 for row in port["orders"]))

    def test_stale_and_thin_exit_marks_and_durable_daily_pause(self):
        books = [expensive("A", "one"), expensive("B", "two")]
        self.ledger.ingest(books, time_at())
        one = {
            **books[0],
            "levels": {"yes": [[90, 100]], "no": [[12, 15]]},
            "as_of": time_at(5),
            "observed_at": time_at(5),
        }
        self.ledger.ingest([one], time_at(5))
        p = self.ledger.state()["portfolio"]["positions"][0]
        self.assertEqual((p["mark_status"], p["marked_qty"]), ("thin", 15))
        self.ledger.ingest([], time_at(200))
        s = self.ledger.state()
        self.assertEqual(s["portfolio"]["stale_marks"], 2)
        self.assertGreaterEqual(s["portfolio"]["daily_loss_cents"], 15_000)
        self.assertTrue(s["paused"])
        res = self.open().set_paused(False, time_at(201))
        self.assertTrue(res["paused"])
        self.assertIn("$150", res["risk_reason"])
        recovered = [
            {**m, "as_of": time_at(202), "observed_at": time_at(202)} for m in books
        ]
        self.ledger.ingest(recovered, time_at(202))
        self.assertTrue(
            self.open().set_paused(False, time_at(202), acknowledge_risk=False)[
                "paused"
            ]
        )
        self.assertFalse(self.ledger.set_paused(False, time_at(202))["paused"])

    def test_missing_forecast_does_not_destroy_a_verified_exit_bid(self):
        p = packet()
        self.ledger.ingest([p], time_at())
        quote = {
            **packet(tick=5),
            "tradable": False,
            "book_executable": True,
            "missing": ["Expired forecast."],
        }
        self.ledger.ingest([quote], time_at(5))
        s = self.ledger.state()
        self.assertEqual(s["markets"][0]["decision"], "Held")
        self.assertGreater(s["portfolio"]["open_value_cents"], 0)

    def test_peak_drawdown_spans_utc_days_and_survives_reopen(self):
        orig = [expensive(f"p{i}", f"group{i}") for i in range(5)]
        self.ledger.ingest(orig, time_at())
        for day, no_ask in enumerate((28, 46, 64, 82, 99), start=1):
            stamp = time_at(86400 * day)
            books = [
                {
                    **m,
                    "as_of": stamp,
                    "observed_at": stamp,
                    "levels": {"yes": [[90, 100]], "no": [[no_ask, 100]]},
                }
                for m in orig
            ]
            self.ledger.ingest(books, stamp)
        s = self.open().state()
        self.assertLess(s["portfolio"]["daily_loss_cents"], 15_000)
        self.assertGreaterEqual(s["portfolio"]["drawdown_cents"], 40_000)
        self.assertIn("$400", s["risk_pause_reason"])

    def test_concurrent_clients_cannot_duplicate_an_entry_or_its_settlement(self):
        other = self.open()
        p = packet()
        with ThreadPoolExecutor(max_workers=2) as pool:
            for fut in [
                pool.submit(ledger.ingest, [p], time_at())
                for ledger in (self.ledger, other)
            ]:
                fut.result()
            for fut in [
                pool.submit(
                    ledger.settle, p["id"], "yes", time_at(8 * 86400), "fixture"
                )
                for ledger in (self.ledger, other)
            ]:
                fut.result()
        s = other.state()
        for kind in ("intent", "fill", "settlement"):
            self.assertEqual(sum(a["kind"] == kind for a in s["activity"]), 1)
        self.assertEqual(s["portfolio"]["cash_cents"], 1_005_196)

    def test_restart_freezes_new_risk_on_receipt_mismatch(self):
        self.ledger.ingest([packet()], time_at())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE intents SET premium=premium+1")
        restarted = self.open()
        s = restarted.state()
        self.assertTrue(s["paused"])
        self.assertIn("reconciliation failed", s["risk_pause_reason"])
        self.assertEqual(sum(e["kind"] == "ledger_mismatch" for e in s["activity"]), 1)
        self.assertTrue(restarted.set_paused(False, time_at(5))["paused"])
        restarted.ingest([packet("new", tick=6)], time_at(6))
        self.assertEqual(len(restarted.state()["portfolio"]["positions"]), 1)
        t = time_at(8 * 86400)
        self.assertEqual(
            restarted.settle("day-A", "yes", t, "fixture")["status"], "pending"
        )
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE intents SET premium=premium-1")
        self.assertFalse(restarted.set_paused(False, t)["paused"])
        self.assertEqual(
            restarted.settle("day-A", "yes", t, "fixture")["status"], "applied"
        )

    def test_licensed_exception_requires_known_payout_rule(self):
        p = {**packet(), "book_source": "kalshi"}
        self.ledger.ingest([p], time_at())
        with self.assertRaises(ValueError):
            self.ledger.settle(p["id"], "void", time_at(1), "kalshi")
        with self.assertRaises(ValueError):
            self.ledger.settle(p["id"], "scalar", time_at(1), "kalshi")
        self.assertEqual(
            self.ledger.state(time_at())["markets"][0]["decision"], "Paper buy"
        )

    def test_scorecard_matches_time_weights_dates_and_excludes_void_or_unavailable_inputs(
        self,
    ):
        empty = self.ledger.state()["scorecard"]
        self.assertEqual(
            (empty["resolved_dates"], empty["markets"], empty["model_brier"]),
            (0, 0, None),
        )
        self.assertFalse(self.ledger.has_market("before-discovery"))
        a, b, c, missing, void = [packet(x) for x in ("A", "B", "C", "missing", "void")]
        # Two markets on date 1 and one on date 2. Every valid source packet has
        # model .66, midpoint .445 and climatology .28. A pass is eligible too.
        c["event_date"] = "2026-11-26"
        missing["evidence"][0]["available_at"] = time_at(600)
        b["fee_rate"] = None
        self.ledger.ingest([a, b, c, missing, void], time_at())
        late = packet("A", tick=2)
        late["forecast"].update(probability=0.99, lower=0.98, upper=1)
        self.ledger.ingest([late], time_at(2))
        self.assertTrue(self.ledger.has_market("A"))
        for market, result in (
            ("A", "yes"),
            ("B", "no"),
            ("C", "no"),
            ("missing", "yes"),
            ("void", "void"),
        ):
            self.ledger.settle(market, result, time_at(8 * 86400), "fixture")
        grade = self.ledger.state()["scorecard"]
        self.assertEqual(
            (grade["source"], grade["resolved_dates"], grade["markets"]),
            ("fixture", 2, 3),
        )
        for name, p in (
            ("model_brier", 0.66),
            ("market_brier", 0.445),
            ("climatology_brier", 0.28),
        ):
            expected = (((1 - p) ** 2 + p**2) / 2 + p**2) / 2
            self.assertAlmostEqual(grade[name], expected)
        self.assertEqual(self.open().state()["scorecard"], grade)

    def test_unfilled_forward_orders_and_settlements_do_not_count_as_trades(self):
        replay_demo(self.ledger)
        forward_path = Path(self.directory.name) / "forward.sqlite"
        forward = self.open(forward_path, initial=25_000)
        p = {**packet(), "book_source": "kalshi"}
        forward.ingest([p], time_at(), auto_fill=False)
        pending = forward.state(time_at())["portfolio"]
        self.assertGreater(pending["reserved_cents"], 0)
        self.assertEqual((pending["filled_orders"], pending["settled_orders"]), (0, 0))
        self.assertEqual(pending["net_pnl_cents"], 0)
        forward.set_paused(True, time_at(1))
        close_at = time_at(8 * 86400)
        forward.settle(p["id"], "yes", close_at, "kalshi")
        forward_again = self.open(forward_path).state(close_at)
        totals = forward_again["portfolio"]
        self.assertEqual(forward_again["markets"][0]["decision"], "Settled")
        self.assertEqual((totals["filled_orders"], totals["settled_orders"]), (0, 0))
        self.assertEqual(
            (
                totals["initial_cash_cents"],
                totals["equity_cents"],
                totals["net_pnl_cents"],
            ),
            (25_000, 25_000, 0),
        )
        self.assertEqual(self.ledger.state()["portfolio"]["filled_orders"], 2)

    def test_full_replay_reproduces_cash_and_persists_point_in_time_provenance(self):
        first = replay_demo(self.ledger)
        state = self.ledger.state()
        second = replay_demo(self.open())
        self.assertEqual(first, second)
        self.assertEqual(state, self.ledger.state())
        self.assertEqual(
            [e["decision"] for e in first["comparison"]], ["Pass", "Paper buy"]
        )
        self.assertEqual([e["net_cents"] for e in first["comparison"]], [-172, 172])
        self.assertEqual(
            (first["forecast_probability"], first["calibration_days"]), (0.6, 600)
        )
        self.assertEqual(
            (first["cash_cents"], first["realized_cents"], first["fees_cents"]),
            (999635, 4670, 350),
        )
        self.assertEqual(
            [m["decision"] for m in state["markets"]],
            ["Paper buy", "Pass", "Watch", "Settled"],
        )
        self.assertEqual(state["portfolio"]["equity_cents"], 1004060)
        p = state["portfolio"]
        self.assertEqual(
            (p["initial_cash_cents"], p["net_pnl_cents"]), (1_000_000, 4060)
        )
        self.assertEqual((p["filled_orders"], p["settled_orders"]), (2, 1))
        self.assertEqual(p["realized_cents"] + p["open_pnl_cents"], p["net_pnl_cents"])
        self.assertEqual(
            (state["scorecard"]["resolved_dates"], state["scorecard"]["markets"]),
            (1, 1),
        )
        self.assertAlmostEqual(state["scorecard"]["model_brier"], 0.16)
        self.assertAlmostEqual(state["scorecard"]["market_brier"], 0.2116)
        self.assertEqual(
            state["markets"][-1]["settlement"]["observed_at"],
            "2026-11-26T13:10:00.000000Z",
        )
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT packet FROM snapshots WHERE market_id=?",
                    (first["case_id"],),
                )
            ]
            self.assertFalse(any(e["kind"] == "complete" for e in rows[0]["evidence"]))
            self.assertTrue(
                any(any(e["kind"] == "complete" for e in r["evidence"]) for r in rows)
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM forecasts WHERE market_id=?",
                    (first["case_id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))


if __name__ == "__main__":
    unittest.main()
