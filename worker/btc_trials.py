"""Independent local paper trials for the three Bitcoin contract products."""

import logging
import time

from . import feeds
from .bitcoin_contracts import BitcoinContracts
from .btc_broker import BTCBroker
from .btc_venues import PolymarketContracts, official_outcome

LOG = logging.getLogger("pmb")
STATE_FIELDS = {
    "kalshi": "bitcoin_contracts",
    "polymarket_us": "polymarket_contracts",
    "polymarket_us_hourly": "polymarket_hourly_contracts",
}


class BitcoinTrials:
    def __init__(self, directory, kalshi_access, poly_access, lock):
        self.lock = lock
        self.sources = {
            "kalshi": BitcoinContracts(kalshi_access, lock),
            "polymarket_us": PolymarketContracts(poly_access, lock),
            "polymarket_us_hourly": PolymarketContracts(
                poly_access, lock, interval_minutes=60
            ),
        }
        self.permissions = {
            key: kalshi_access if key == "kalshi" else poly_access
            for key in self.sources
        }
        self.accounts = {key: BTCBroker(directory, venue=key) for key in self.sources}
        self.retries = {}
        self.host_backoff = {}

    def source_permission_changed(self, keys):
        for key in keys:
            self.sources[key].permission_changed()
            if self.permissions[key]()[0] != "enabled":
                self.accounts[key].set_auto(False)
                self.sync(key)

    def snapshot(self, *, comparison=False):
        accounts = {}
        result = {"bitcoin_brokers": accounts}
        for key, account in self.accounts.items():
            state = account.state()
            if comparison:
                accounts[key] = {
                    name: state.get(name)
                    for name in (
                        "portfolio",
                        "daily",
                        "daily_history",
                        "started_at",
                        "auto_enabled",
                        "performance",
                        "venue",
                    )
                }
            else:
                result[STATE_FIELDS[key]] = self.sources[key].state()
                accounts[key] = state
        return result

    def action(self, action, value):
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("venue"), str)
            or value["venue"] not in self.accounts
        ):
            raise ValueError("Choose a Bitcoin contract market.")
        key = value["venue"]
        account = self.accounts[key]
        if action == "btc-contract-order":
            receipt = account.place(value, {key: self.sources[key].state()})
            return {"ok": True, "receipt": receipt}
        if action == "btc-contract-auto":
            enabled = value.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("Choose on or off for this paper account.")
            if enabled and self.permissions[key]()[0] != "enabled":
                raise ValueError(
                    "Enable this venue’s public data on an online worker first."
                )
            account.set_auto(enabled)
        elif action == "btc-contract-limits":
            account.set_limits(value.get("limits"))
        else:
            raise ValueError("Unknown Bitcoin contract action.")
        return {"ok": True}

    def sync(self, key, settlements=()):
        with self.lock:
            return self.accounts[key].sync(
                {key: self.sources[key].state()}, settlements=settlements
            )

    def run(self, stop):
        while not stop.is_set():
            for key in self.accounts:
                try:
                    self.sync(key)
                except Exception:
                    LOG.exception("Bitcoin paper mark failed for %s", key)
            stop.wait(5)

    def reconcile(self):
        # Each open expired position is retried independently; network calls stay outside
        # the order/permission lock. An exchange outage must not delay new-window quotes.
        pending = set()
        for key, account in self.accounts.items():
            try:
                pending.update(
                    (key, row["market_id"]) for row in account.pending_settlements()
                )
            except Exception:
                LOG.exception("Could not read pending Bitcoin positions for %s", key)
        self.retries = {k: v for k, v in self.retries.items() if k in pending}
        for key, market_id in sorted(
            pending, key=lambda k: (self.retries.get(k, (0, 0))[0], k)
        ):
            host = "kalshi" if key == "kalshi" else "polymarket_us"
            request = (key, market_id)
            due, failures = self.retries.get(request, (0, 0))
            if time.monotonic() < max(due, self.host_backoff.get(host, 0)):
                continue
            mode, generation = self.permissions[key]()
            if mode != "enabled":
                continue

            def guard():
                if self.permissions[key]() != ("enabled", generation):
                    raise feeds.PermissionRevoked()

            delay = 30
            try:
                outcome = official_outcome(key, market_id, permission_guard=guard)
                if outcome:
                    with self.lock:
                        guard()
                        self.sync(key, settlements=(outcome,))
                    self.retries.pop(request, None)
                    continue
            except feeds.PermissionRevoked:
                continue
            except (feeds.Unavailable, *feeds.SAFE_ERRORS) as error:
                failures = min(6, failures + 1)
                delay = max(15 * 2**failures, getattr(error, "retry", 0) or 0)
                if getattr(error, "code", None) == 429:
                    self.host_backoff[host] = time.monotonic() + delay
            self.retries[request] = (time.monotonic() + delay, failures)

    def settle(self, stop):
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception:
                LOG.exception("Bitcoin paper settlement check failed")
            stop.wait(5)
