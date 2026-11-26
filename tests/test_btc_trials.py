from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest import TestCase
from unittest.mock import Mock, patch

from worker import feeds
from worker.btc_trials import BitcoinTrials


class TrialReconciliationTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.generation = 1
        self.trials = BitcoinTrials(
            Path(temp.name),
            lambda: ("enabled", 1),
            lambda: ("enabled", self.generation),
            RLock(),
        )
        for key in self.trials.accounts:
            account = Mock()
            account.pending_settlements.return_value = [{"market_id": key + "-pending"}]
            self.trials.accounts[key] = account
            self.trials.sources[key] = Mock()

    def test_host_throttle_and_network_calls_do_not_hold_order_lock(self):
        def outcome(venue, market, *, permission_guard):
            permission_guard()
            self.assertFalse(self.trials.lock._is_owned())
            if venue == "polymarket_us":
                raise feeds.Unavailable("rate limit", code=429, retry=60)
            return {"venue": venue, "market_id": market, "result": "YES"}

        with patch("worker.btc_trials.official_outcome", side_effect=outcome) as read:
            self.trials.reconcile()
            self.assertEqual(
                [c.args[0] for c in read.call_args_list], ["kalshi", "polymarket_us"]
            )
            self.trials.accounts["kalshi"].sync.assert_called_once()
            self.trials.accounts["polymarket_us_hourly"].sync.assert_not_called()
            self.trials.accounts["kalshi"].pending_settlements.return_value = []
            self.trials.reconcile()
            self.assertEqual(read.call_count, 2)

    def test_permission_change_during_outcome_read_discards_receipt(self):
        for key in ("kalshi", "polymarket_us_hourly"):
            self.trials.accounts[key].pending_settlements.return_value = []

        def stale_result(key, market, *, permission_guard):
            permission_guard()
            self.generation += 1
            return {"venue": key, "market_id": market, "result": "NO"}

        with patch("worker.btc_trials.official_outcome", side_effect=stale_result):
            self.trials.reconcile()
        self.trials.accounts["polymarket_us"].sync.assert_not_called()
