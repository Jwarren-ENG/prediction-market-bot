import json
import unittest
from datetime import date
from decimal import Decimal as D
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from scripts import bitcoin_study as model

DAY = model.DAY
START = date(2024, 1, 1)


def bar(opening, close):
    opening, close = D(opening), D(close)
    return model.Bar(min(opening, close), max(opening, close), opening, close, D(2))


def row(day, opening=100, close=102, volume=2):
    return {
        "timestamp": str((day - date(1970, 1, 1)).days * 86400),
        "low": "99",
        "high": "103",
        "open": str(opening),
        "close": str(close),
        "volume": str(volume),
    }


class BitcoinStudyTest(unittest.TestCase):
    def test_sequence_uses_eight_contiguous_days_strictly_greater_and_no_future(self):
        days = {START + i * DAY: bar(100, 100 + i) for i in range(10)}
        targets = model.signals(days)
        self.assertNotIn(START + 6 * DAY, targets)
        self.assertTrue(targets[START + 7 * DAY])
        days[START + 7 * DAY] = bar(100, 100)
        self.assertFalse(model.signals(days)[START + 7 * DAY])
        del days[START + 4 * DAY]
        self.assertEqual(model.signals(days), {})

    def test_next_open_timing_transition_only_and_delay(self):
        days = {START + i * DAY: bar(100, 100 + i) for i in range(8)}
        days.update(
            {
                START + 8 * DAY: bar(200, 1000),
                START + 9 * DAY: bar(100, 100),
                START + 10 * DAY: bar(101, 100),
            }
        )
        targets = model.signals(days)
        now, _ = model.simulate(
            days, targets, START + 8 * DAY, START + 8 * DAY, fee=D(0), slip=D(0)
        )
        self.assertEqual(now["net_usd"], 2000)  # $500 / $200 at the following open.
        delayed, _ = model.simulate(
            days,
            targets,
            START + 8 * DAY,
            START + 8 * DAY,
            fee=D(0),
            slip=D(0),
            extra_delay=1,
        )
        self.assertEqual((delayed["buy_count"], delayed["net_usd"]), (0, 0))
        settled, _ = model.simulate(
            days, targets, START + 8 * DAY, START + 10 * DAY, fee=D(0), slip=D(0)
        )
        self.assertEqual((settled["buy_count"], settled["sell_count"]), (1, 1))
        self.assertEqual(settled["ending_equity_usd"], 9752.50)
        self.assertEqual(settled["max_drawdown_usd"], 2250.00)

    def test_missing_open_executes_on_next_available_open_without_imputed_mark(self):
        days = {START + 2 * DAY: bar(200, 220), START + 3 * DAY: bar(210, 210)}
        # Jan 2 has neither a traded open nor a mark. The Jan 1 close
        # signal can first enter on Jan 3, at 200, and survives the data gap.
        out, _ = model.simulate(
            days, {START: True}, START + DAY, START + 3 * DAY, fee=D(0), slip=D(0)
        )
        self.assertEqual(out["ending_equity_usd"], 10025)
        self.assertEqual(
            (out["observed_days"], out["buy_count"], out["days_held_at_close"]),
            (2, 1, 2),
        )
        self.assertTrue(out["open_terminal_position"])

    def test_fee_and_adverse_cost_paid_each_way_and_mark_matches_actual_liquidation(
        self,
    ):
        account = model.Account()
        fee, slip = model.FEE, model.SLIP
        buy = account.rebalance(True, D(100), fee, slip)
        self.assertEqual(account.cash, D(9500))
        self.assertEqual(buy.notional + buy.fee, D(500))
        self.assertIsNone(account.rebalance(True, D(300), fee, slip))
        expected = D(9500) + D(500) * (1 - slip) * (1 - fee) / ((1 + slip) * (1 + fee))
        self.assertAlmostEqual(account.equity(D(100), fee, slip), expected, places=18)
        sell = account.rebalance(False, D(100), fee, slip)
        self.assertAlmostEqual(account.cash, expected, places=18)
        self.assertEqual(account.btc, D(0))
        self.assertGreater(sell.fee, D(0))
        self.assertGreater(sell.adverse, D(0))
        low = model.Account(cash=D(8000))
        low.rebalance(True, D(72), fee, slip)
        self.assertEqual(low.cash, D(7600))

    def test_bitstamp_pair_dates_volume_and_valid_prices(self):
        def parse(rows, pair="BTC/USD"):
            return model.parse_page(
                {"data": {"pair": pair, "ohlc": rows}}, START, START + 2 * DAY
            )

        first = row(START)
        self.assertEqual(
            list(
                parse(
                    [
                        first,
                        row(START + DAY, volume=0),
                        row(START + 2 * DAY),
                        row(START - DAY),
                    ]
                )
            ),
            [START],
        )
        self.assertEqual(
            parse([first, first])[START], model.Bar(D(99), D(103), D(100), D(102), D(2))
        )
        with self.assertRaises(ValueError):
            parse([first, dict(first, close="101")])
        with self.assertRaises(ValueError):
            parse([first], "ETH/USD")
        for change in (
            {"timestamp": "12345"},
            {"low": "999"},
            {"volume": "-1"},
            {"open": "NaN"},
            {"open": "2e70000"},
        ):
            with self.assertRaises(ValueError):
                parse([dict(first, **change)])
        with self.assertRaises(TypeError):
            parse([dict(first, open=None)])

    def test_captured_public_request_uses_native_bitstamp_daily_bounds(self):
        class Result:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, n):
                return json.dumps(
                    {"data": {"pair": "BTC/USD", "ohlc": [row(START)]}}
                ).encode()

        class Opener:
            def open(self, req, timeout):
                self.url = req.full_url
                return Result()

        fake = Opener()
        self.assertIn(START, model.read_page(fake, START, START + 900 * DAY))
        url = urlsplit(fake.url)
        self.assertEqual(
            (url.scheme, url.netloc, url.path),
            ("https", "www.bitstamp.net", "/api/v2/ohlc/btcusd/"),
        )
        q = parse_qs(url.query)
        self.assertEqual(q["step"], ["86400"])
        self.assertEqual(q["exclude_current_candle"], ["true"])
        self.assertEqual(q["limit"], ["902"])
        self.assertEqual(int(q["end"][0]) - int(q["start"][0]), 900 * 86400)

    def test_long_retry_after_closes_response_and_exits_without_retrying(self):
        body = BytesIO(b"throttled")
        err = HTTPError(model.SOURCE, 429, "throttled", {"Retry-After": "90"}, body)
        opener = Mock()
        opener.open.side_effect = err
        with patch.object(model.time, "sleep") as wait, self.assertRaises(RuntimeError):
            model.read_page(opener, START, START + DAY)
        self.assertTrue(body.closed)
        opener.open.assert_called_once()
        wait.assert_not_called()

    def test_independent_period_restarts_cash_and_calendar_marks_keep_continuity(self):
        first, second = date(2021, 12, 31), date(2022, 1, 1)
        days = {first: bar(100, 150), second: bar(200, 210)}
        targets = {first - DAY: True, first: False}
        full, year = model.simulate(days, targets, first, second, fee=D(0), slip=D(0))
        holdout, _ = model.simulate(days, targets, second, second, fee=D(0), slip=D(0))
        self.assertEqual(full["net_usd"], 500)
        self.assertEqual(year[2021]["net_usd"] + year[2022]["net_usd"], full["net_usd"])
        self.assertEqual(holdout["net_usd"], 0)
