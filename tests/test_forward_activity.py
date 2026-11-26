"""Forward paper controls, real scan receipts, and the forward activity journal."""

import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from worker.server import Desk
from worker.storage import Store, utc
from worker.weather import instant, iso


class ForwardActivityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.desk = self.open_desk()

    def open_desk(self):
        desk = Desk(self.directory, online=True)
        for account in desk.ledgers.values():
            self.addCleanup(account.close)
        return desk

    @staticmethod
    def completed(desk):
        acquired = desk.scanning.acquire(timeout=4)
        if not acquired:
            raise AssertionError("Forward scan did not finish")
        desk.scanning.release()

    def test_market_switch_controls_only_forward_and_survives_restart(self):
        self.assertEqual(self.desk.mode(), "replay")
        self.assertFalse(self.desk.state()["paper_automation"]["available"])
        with self.assertRaisesRegex(ValueError, "permission"):
            self.desk.action("resume", {"source": "markets"})
        self.desk.action("permission", True)
        self.assertTrue(self.desk.state()["paper_automation"]["enabled"])
        self.desk.action("pause", {"source": "markets"})
        self.assertFalse(self.desk.ledgers["replay"].entry_control()["manual_pause"])
        other = self.open_desk()
        self.assertTrue(other.state()["paper_automation"]["manual_pause"])
        self.assertFalse(other.state()["paper_automation"]["enabled"])
        self.assertTrue(
            other.action("resume", {"source": "markets"})["paper_automation"]["enabled"]
        )
        with other.ledgers["forward"].db:
            other.ledgers["forward"].db.execute(
                "UPDATE account SET halt='Risk stop for review' WHERE id=1"
            )
        with self.assertRaisesRegex(ValueError, "Risk stop"):
            other.action("resume", {"source": "markets"})
        self.assertFalse(other.state()["paper_automation"]["enabled"])
        self.assertEqual(
            other.state()["paper_automation"]["risk_pause_reason"],
            "Risk stop for review",
        )
        other.action("resume", None)
        self.assertTrue(other.state()["paper_automation"]["enabled"])
        other.online = False
        self.assertFalse(other.state()["paper_automation"]["available"])
        with self.assertRaisesRegex(ValueError, "online"):
            other.action("resume", {"source": "markets"})

    def test_repeated_manual_or_scheduled_scan_joins_same_receipt(self):
        released, started = threading.Event(), threading.Event()

        def slow_collect(*args, **kwargs):
            started.set()
            if not released.wait(3):
                raise AssertionError("Timed out waiting for scan test release")
            return {
                "status": "Public weather records updated.",
                "packets": [],
                "settlements": [],
                "evidence": [],
            }

        with patch("worker.server.collect", side_effect=slow_collect) as fetch:
            first = self.desk.action("scan", {"source": "markets"})["scan_id"]
            self.assertTrue(started.wait(2))
            try:
                run = self.desk.state()["health"]["scan"]
                self.assertEqual(
                    (run["id"], run["status"], run["finished_at"]),
                    (first, "running", None),
                )
                self.assertEqual(self.desk.action("scan", None)["scan_id"], first)
                self.assertEqual(self.desk.scan(scheduled=True), first)
            finally:
                released.set()
            self.completed(self.desk)
            self.assertIsNone(self.desk.scan(scheduled=True))
            fetch.assert_called_once()
        replay = self.desk.state()
        self.assertFalse(any(e["kind"] == "scan" for e in replay["activity"]))
        done = replay["health"]["scan"]
        self.assertEqual((done["id"], done["status"]), (first, "complete"))
        self.assertGreaterEqual(done["finished_at"], done["started_at"])
        self.desk.action("mode", "forward")
        rows = [e for e in self.desk.state()["activity"] if e["kind"] == "scan"]
        self.assertEqual([(e["id"], e["status"]) for e in rows], [(first, "complete")])
        self.assertEqual(self.open_desk().state()["health"]["recent_scans"][0], done)

    def test_switch_prevents_then_permits_a_fresh_eligible_paper_entry(self):
        def qualified_packet(*args, **kwargs):
            now = instant(utc())
            stamp = iso(now)
            issued = iso(now - timedelta(hours=1))
            p = {
                "id": "KXHIGHNY-TEST-A",
                "event_id": "KXHIGHNY-TEST",
                "station": "KNYC",
                "event_date": now.date().isoformat(),
                "title": "Test market",
                "label": "44 to 45°F",
                "as_of": stamp,
                "observed_at": stamp,
                "close_at": iso(now + timedelta(days=1)),
                "rules": "Inclusive Central Park high.",
                "rule_version": "test",
                "book_source": "kalshi",
                "levels": {"yes": [[45, 10]], "no": [[56, 10]]},
                "fee_rate": "0.07",
                "tradable": True,
                "missing": [],
                "forecast": {
                    "probability": 0.7,
                    "lower": 0.6,
                    "upper": 0.8,
                    "baseline": 0.5,
                    "samples": 100,
                    "model": "test error distribution",
                    "issued_at": issued,
                },
                "evidence": [
                    {
                        "url": "fixture://test/issued",
                        "published_at": issued,
                        "available_at": issued,
                        "retrieved_at": issued,
                    }
                ],
            }
            return {"packets": [p], "status": "Captured one test packet."}

        self.desk.action("permission", True)
        with patch("worker.server.collect", side_effect=qualified_packet):
            self.desk.action("pause", {"source": "markets"})
            self.desk.scan()
            self.completed(self.desk)
            first = self.desk.ledgers["forward"].state()
            self.assertEqual(first["portfolio"]["filled_orders"], 0)
            self.assertEqual(first["markets"][0]["decision"], "Watch")
            self.desk.action("resume", {"source": "markets"})
            self.desk.scan()
            self.completed(self.desk)
        second = self.desk.ledgers["forward"].state()
        self.assertEqual(second["portfolio"]["filled_orders"], 1)
        self.assertEqual(second["markets"][0]["decision"], "Paper buy")

    def test_failed_and_degraded_scans_keep_their_own_outcomes(self):
        result = {
            "status": "Feed degraded. Weather source unavailable.",
            "degraded": True,
        }
        with patch("worker.server.collect", return_value=result):
            partial = self.desk.scan()
            self.completed(self.desk)
        with (
            patch("worker.server.collect", side_effect=RuntimeError("fixture failure")),
            self.assertLogs("pmb", level="ERROR"),
        ):
            failed = self.desk.scan()
            self.completed(self.desk)
        with patch(
            "worker.server.collect", return_value={"status": "Latest scan completed."}
        ):
            success = self.desk.scan()
            self.completed(self.desk)
        outcomes = {
            s["id"]: s["status"] for s in self.desk.state()["health"]["recent_scans"]
        }
        self.assertEqual(
            outcomes, {partial: "partial", failed: "failed", success: "complete"}
        )

    def test_restart_marks_unfinished_capture_and_late_worker_cannot_overwrite_it(self):
        self.assertEqual(self.desk.state()["health"]["scan"]["status"], "idle")
        scan_id = self.desk.store.begin_scan(utc())
        other = self.open_desk()
        s = other.state()["health"]["scan"]
        self.assertEqual((s["id"], s["status"]), (scan_id, "failed"))
        self.desk.store.finish_scan(scan_id, "complete", utc(), "Late result", [])
        self.assertEqual(other.state()["health"]["scan"], s)
        stamp = utc()
        for _ in range(102):
            i = other.store.begin_scan(stamp)
            other.store.finish_scan(i, "complete", stamp, "Weather scan completed.", [])
        self.assertEqual(len(other.store.scan_history()), 100)

    def test_local_notes_enter_only_forward_activity_on_actual_completion(self):
        self.desk.action("mode", "forward")
        at = utc()
        columns = "(fingerprint,market_id,provider,status,at,workspace,text)"
        with self.desk.store.connection() as db:
            for key, mode, source in (
                ("a", "forward", "Local research"),
                ("b", "replay", "Local research"),
                ("c", "forward", "Other provider"),
            ):
                db.execute(
                    "INSERT INTO research_notes "
                    + columns
                    + " VALUES(?,?,?,'pending',?,?,?)",
                    (key, "KXHIGHNY", source, at, mode, "Research requested."),
                )
        self.assertFalse(self.desk.state()["activity"])
        with self.desk.store.connection() as db:
            db.execute(
                "UPDATE research_notes SET status='complete',text='Daily station report received.' WHERE status='pending'"
            )
        (row,) = self.desk.state()["activity"]
        self.assertEqual(
            (row["kind"], row["status"], row["market_id"]),
            ("research", "complete", "KXHIGHNY"),
        )
        self.assertIn("Local research completed", row["message"])
        self.assertIn("Daily station report received.", row["message"])
        self.assertEqual(self.open_desk().state()["activity"], [row])
        self.desk.action("mode", "replay")
        self.assertFalse(
            any(e["kind"] == "research" for e in self.desk.state()["activity"])
        )

    def test_migrated_scan_uses_known_receipt_and_rejects_unknown_history(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            stamp = utc()
            store.put(
                "forward_health", {"at": stamp, "status": "Unclassified older status"}
            )
            store.restore_scan_history(utc())
            self.assertEqual(store.scan_history(), [])
            store.put(
                "forward_health",
                {"at": stamp, "status": "Feed degraded. Source unavailable."},
            )
            store.restore_scan_history(utc())
            (row,) = store.scan_history()
            self.assertEqual(
                (row["status"], row["started_at"], row["finished_at"]),
                ("partial", None, stamp),
            )


if __name__ == "__main__":
    unittest.main()
