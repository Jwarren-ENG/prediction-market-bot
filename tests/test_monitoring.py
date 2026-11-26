import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from worker import monitoring
from worker.server import Desk
from worker.storage import utc
from worker.weather import instant


class MonitoringTest(unittest.TestCase):
    def test_window_counts_all_receipts_and_preserves_subsecond_boundaries(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE activity(at TEXT,kind TEXT)")
            start = 1780000000
            first = monitoring.stamp(start)
            last = monitoring.stamp(start + 3599).replace(".000000", ".999999")
            db.executemany(
                "INSERT INTO activity VALUES(?,?)",
                [(first, "decision")] * 550
                + [
                    (last, "decision"),
                    (first, "fill"),
                    (monitoring.stamp(start + 3600), "decision"),
                    (monitoring.stamp(start + 7200), "decision"),
                ],
            )
            result = monitoring.counts(db, "decisions", start, start + 7200, 3600)
            self.assertEqual(result, {"first_at": first, "counts": [551, 1]})

    def test_common_start_same_bucket_entry_costs_restart_and_lifetime_drawdown(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.executescript(monitoring.PAIR_SCHEMA)
            for at, values, opening, written in (
                (300, (10000, 10000, 10000), True, True),
                (310, (10000, 10000, 9930), False, True),
                (315, (10000, 10000, 9900), False, False),
                (600, (10050, 10100, 10150), False, True),
                (900, (10020, 10001, 10010), False, True),
                (620, (1, 1, 1), True, False),
            ):
                with db:
                    self.assertEqual(
                        monitoring.record_pair(
                            db, monitoring.stamp(at), *values, opening=opening
                        ),
                        written,
                    )
            summary = monitoring.comparison(db)
            self.assertEqual(summary["start_at"], monitoring.stamp(300))
            self.assertEqual(summary["as_of"], monitoring.stamp(900))
            self.assertEqual(summary["accounts"]["hold"]["drawdown_cents"], 140)
            self.assertEqual(summary["accounts"]["bitcoin"]["net_cents"], 1)
            self.assertEqual(summary["points"][-1]["weather_cents"], 10020)

    def test_data_only_has_no_comparison_and_first_eligible_trade_includes_costs(self):
        with TemporaryDirectory() as path:
            desk = Desk(Path(path))
            try:
                desk.store.put("allow_market", True)
                desk.action("btc-feed", True)
                desk.record_comparison()
                with desk.store.connection() as db:
                    self.assertIsNone(monitoring.comparison(db)["start_at"])
                at = utc()
                btc = desk.bitcoin.state()
                btc["started_at"] = at
                btc["quote"] = {"fresh": True, "source_at": at, "observed_at": at}
                btc["benchmark"].update(
                    filled_orders=1, equity_cents=999345, mark_fresh=True
                )
                with patch.object(desk.bitcoin, "state", return_value=btc):
                    desk.record_comparison()
                    desk.record_comparison()
                    btc["quote"]["source_at"] = (
                        instant(at) - timedelta(seconds=31)
                    ).isoformat()
                    desk.record_comparison()
                with desk.store.connection() as db:
                    result = monitoring.comparison(db)
                    self.assertEqual(result["accounts"]["hold"]["net_cents"], -655)
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM paired_equity").fetchone()[0],
                        2,
                    )
                self.assertFalse(
                    monitoring.comparable_weather(
                        {"positions": [], "orders": [{"status": "unknown"}]}
                    )
                )
                self.assertFalse(
                    monitoring.comparable_weather(
                        {"positions": [{"mark_status": "thin"}], "orders": []}
                    )
                )
            finally:
                for ledger in desk.ledgers.values():
                    ledger.close()
