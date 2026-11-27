"""Independent local paper trials for the three Bitcoin contract products."""

import logging
import time
from pathlib import Path

from . import feeds
from .bitcoin_contracts import BitcoinContracts
from .btc_broker import BANKROLL_PRESETS, BTCBroker
from .btc_venues import PolymarketContracts, official_outcome
from .storage import Store

LOG = logging.getLogger("pmb")
STATE_FIELDS = {
    "kalshi": "bitcoin_contracts",
    "polymarket_us": "polymarket_contracts",
    "polymarket_us_hourly": "polymarket_hourly_contracts",
}


class BitcoinTrials:
    def __init__(self, directory, kalshi_access, poly_access, lock, *, store=None):
        self.directory = Path(directory)
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
        self.store = store if store is not None else Store(directory)
        profile = self.store.get("btc_bankroll_profile", {"venue": "kalshi", "starting_cents": 2500})
        self.bankroll = self._bankroll(profile)
        self.retries = {}
        self.host_backoff = {}

    def _bankroll(self, profile):
        venue, cents = profile.get("venue"), profile.get("starting_cents")
        if not isinstance(venue, str) or venue not in self.sources:
            raise ValueError("Choose an available Bitcoin contract market.")
        if type(cents) is not int or cents not in BANKROLL_PRESETS:
            raise ValueError("Choose $25, $50 or $100 in paper funds.")
        return BTCBroker(self.directory, venue, starting_cents=cents)

    @property
    def bankroll_id(self):
        return f"{self.bankroll.venue}:{self.bankroll.starting_cents}"

    def source_permission_changed(self, keys):
        for key in keys:
            self.sources[key].permission_changed()
            if self.permissions[key]()[0] != "enabled":
                self.accounts[key].set_auto(False)
                if self.bankroll.venue == key:
                    self.bankroll.set_auto(False)
                self.sync(key)

    def snapshot(self, *, comparison=False, equity_account=None, equity_minutes=None):
        accounts = {}
        result = {"bitcoin_brokers": accounts}
        for key, account in self.accounts.items():
            state = account.state(equity_minutes=equity_minutes) if key == equity_account else account.state()
            if "portfolio_history" in state:
                result["portfolio_history"] = state.pop("portfolio_history")
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
        if not comparison:
            with self.lock:
                personal = self.bankroll.state(equity_minutes=equity_minutes if equity_account == "bankroll" else None)
                result["bankroll"] = {k: personal[k] for k in (
                    "venue", "portfolio", "auto_enabled", "limits", "budget", "daily", "gate_reason", "markets")}
                result["bankroll"].update(id=self.bankroll_id, starting_options_cents=BANKROLL_PRESETS,
                                          fills=personal["fills"][:12], activity=personal["activity"][:30])
                if "portfolio_history" in personal:
                    result["portfolio_history"] = personal["portfolio_history"]
        return result

    def bankroll_action(self, action, value):
        with self.lock:
            if not isinstance(value, dict) or value.get("expected") != self.bankroll_id:
                raise ValueError("The active paper bankroll changed. Refresh and try again.")
            if action == "btc-bankroll-auto":
                enabled = value.get("enabled")
                if enabled is True and self.permissions[self.bankroll.venue]()[0] != "enabled":
                    raise ValueError("Enable this venue’s public data on an online worker first.")
                self.bankroll.set_auto(enabled)
            elif action == "btc-bankroll-profile":
                profile = {"venue": value.get("venue"), "starting_cents": value.get("starting_cents")}
                if profile != {"venue": self.bankroll.venue, "starting_cents": self.bankroll.starting_cents}:
                    if self.bankroll.has_holdings():
                        raise ValueError("This bankroll holds contracts. Switch after they sell or settle.")
                    selected = self._bankroll(profile)
                    # Publish the new selection only after its ledger is ready and paused.
                    selected.set_auto(False)
                    self.store.put("btc_bankroll_profile", profile)
                    self.bankroll = selected
            else:
                raise ValueError("Unknown paper bankroll action.")
            return {"ok": True}

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
            data = {key: self.sources[key].state()}
            self.accounts[key].sync(data, settlements=settlements)
            if self.bankroll.venue == key:
                self.bankroll.sync(data, settlements=settlements)

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
        with self.lock:
            accounts = [*self.accounts.items(), (self.bankroll.venue, self.bankroll)]
        for key, account in accounts:
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
