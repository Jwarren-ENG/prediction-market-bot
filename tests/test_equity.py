import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta

from worker import equity
from worker.weather import instant, iso


class EquityHistoryTests(unittest.TestCase):
    def test_new_account_keeps_intrahour_moves_in_the_week_view(self):
        start = instant("2026-11-26T12:00:00Z")
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute(equity.SCHEMA)
            for n in range(40):
                equity.record(db, iso(start + timedelta(minutes=n)), 1000000 + n%7)
            end = start + timedelta(minutes=40)
            hour = equity.history(db, "spot", 60, end, 1000000)
            week = equity.history(db, "spot", 10080, end, 1000000)
            self.assertEqual(week["points"], hour["points"])
            self.assertEqual(len(week["points"]), 41)

    def test_long_window_preserves_extrema_order_and_breaks_after_downsampling(self):
        start = instant("2026-11-20T00:00:00Z")
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute(equity.SCHEMA)
            for n in range(1500):
                # Multiple points in each bucket. Both extremes occur in the middle.
                amount = {204: 999900, 207: 1000110, 210: None}.get(n, 1000000)
                equity.record(db, iso(start + timedelta(minutes=n)), amount)
            end = start + timedelta(minutes=1500)
            s = equity.history(db, "forward", 10080, end, 1000001)
            self.assertLessEqual(len(s["points"]), 4*145)
            self.assertEqual([p[1] for p in s["points"] if p[1] != 1000000], [999900, 1000110, 1000001])
            self.assertEqual(s["points"][0][2], 0)
            self.assertEqual(s["points"][-1][2], 1)
            self.assertTrue(s["has_gaps"])
            self.assertEqual(s["observations"], 1500)
            short = equity.history(db, "forward", 15, end, None)
            self.assertIsNone(short["current_cents"])
            self.assertTrue(all(p[0] >= short["start_ms"] for p in short["points"]))
            self.assertTrue(all(p[1] == 1000000 for p in short["points"]))

    def test_gaps_and_missing_prices_never_become_losses(self):
        start = instant("2026-11-26T15:45:00Z")
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute(equity.SCHEMA)
            self.assertEqual(equity.history(db, "spot", 60, start, None)["points"], [])
            equity.record(db, iso(start), 1000000)
            equity.record(db, iso(start + timedelta(seconds=10)), 1000000)
            equity.record(db, iso(start + timedelta(seconds=15)), None)
            equity.record(db, iso(start + timedelta(seconds=25)), 999987)
            equity.record(db, iso(start - timedelta(seconds=10)), 1)
            data = equity.history(db, "spot", 240, start + timedelta(hours=1), 1000000)
            self.assertEqual([(c, seg) for _,c,seg in data["points"]], [(1000000,0),(999987,1),(1000000,2)])
            self.assertEqual(data["first_at"], iso(start))
