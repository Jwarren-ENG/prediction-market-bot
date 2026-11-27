from pathlib import Path
from datetime import timedelta
from tempfile import TemporaryDirectory
from threading import RLock
from unittest import TestCase
from unittest.mock import Mock, patch

from worker import feeds
from worker.btc_trials import BitcoinTrials
from tests.test_btc_broker import T, START, feed, order, settled


class BankrollProfileTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.mode = "enabled"
        self.access = lambda: (self.mode, 1)
        self.trials = BitcoinTrials(self.directory, self.access, self.access, RLock())

    def select(self, venue, cents):
        self.trials.bankroll_action("btc-bankroll-profile", {
            "expected": self.trials.bankroll_id, "venue": venue, "starting_cents": cents})

    def test_one_active_trial_resumes_its_history_and_never_adds_it_to_comparison(self):
        trials = self.trials
        personal = trials.bankroll
        personal.place(order(qty=1), feed(), T)
        with self.assertRaisesRegex(ValueError, "holds contracts"):
            self.select("polymarket_us", 10000)
        end = START + timedelta(minutes=15)
        personal.sync({}, [settled("kalshi", end)], now=end)
        cash = personal.state(end)["portfolio"]["cash_cents"]
        self.assertEqual(cash, 2547)
        personal.set_auto(True)
        self.select("polymarket_us", 10000)
        self.assertFalse(trials.bankroll.state(T)["auto_enabled"])
        self.assertEqual(trials.bankroll.state(T)["portfolio"]["equity_cents"], 10000)
        with self.assertRaisesRegex(ValueError, "changed"):
            trials.bankroll_action("btc-bankroll-auto", {"expected":"kalshi:2500", "enabled":True})
        restarted = BitcoinTrials(self.directory, self.access, self.access, RLock())
        self.assertEqual(restarted.bankroll_id, "polymarket_us:10000")
        self.select("kalshi", 2500)
        snapshot = trials.snapshot(equity_account="bankroll", equity_minutes=60)
        self.assertEqual(snapshot["portfolio_history"]["current_cents"], cash)
        self.assertEqual(snapshot["bankroll"]["portfolio"]["filled_orders"], 1)
        self.assertFalse(snapshot["bankroll"]["auto_enabled"])
        self.assertEqual(sum(a["portfolio"]["cash_cents"] for a in snapshot["bitcoin_brokers"].values()), 3000000)
        self.assertNotIn("bankroll", trials.snapshot(comparison=True))

    def test_offline_validation_revocation_and_settlement_shared_with_benchmark(self):
        trials = self.trials
        for value in (True, 2500.5, 2501, 1000000, "2500"):
            with self.assertRaises(ValueError):
                self.select("kalshi", value)
        self.select("polymarket_us_hourly", 5000)
        key = trials.bankroll.venue
        for broker in (trials.accounts[key], trials.bankroll):
            self.assertEqual(broker.place(order(key, qty=1), feed(key), T)["status"], "filled")
        trials.bankroll_action("btc-bankroll-auto", {"expected":trials.bankroll_id,"enabled":True})
        self.mode = "offline"
        trials.source_permission_changed([key])
        with self.assertRaisesRegex(ValueError, "public data"):
            trials.bankroll_action("btc-bankroll-auto", {"expected":trials.bankroll_id,"enabled":True})
        self.assertFalse(trials.bankroll.state()["auto_enabled"])
        self.mode = "enabled"
        outcome = settled(key, START + timedelta(hours=1))
        with patch("worker.btc_trials.official_outcome", return_value=outcome) as read:
            trials.reconcile()
        read.assert_called_once()
        for broker in (trials.accounts[key], trials.bankroll):
            self.assertFalse(broker.has_holdings())


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
