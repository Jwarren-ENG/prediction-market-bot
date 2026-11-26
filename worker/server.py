"""Single-owner, loopback paper worker and authenticated desktop API."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import secrets
import mimetypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit, unquote, parse_qs
from urllib.request import Request, build_opener, ProxyHandler

from . import monitoring
from .bitcoin import BitcoinPaper, quote_fresh
from .btc_trials import BitcoinTrials
from .feeds import collect
from .ledger import Ledger
from .notifications import Notifications
from .quotes import QuoteTape
from .replay import replay_demo
from .research import NoRedirect, Research
from .storage import Store, access_key, atomic_text, data_home, exclusive_worker, utc
from .weather import instant

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 47823
VERSION = "0.3.0"
LOG = logging.getLogger("pmb")


def private_dir(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class Desk:
    def __init__(self, directory, interval=300, online=True):
        self.directory = directory
        self.store = Store(directory)
        self.ledgers = {
            name: Ledger(directory / (name + ".sqlite"))
            for name in ("replay", "forward")
        }
        self.research = Research(self.store)
        self.notifications = Notifications(self.store)
        self.scan_revision = 0
        self.transition = threading.RLock()
        self.scanning = threading.Lock()
        self.analyzing = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.online = online
        self.quotes = QuoteTape(self._quote_access, self.transition)
        self.bitcoin = BitcoinPaper(directory, online=online)
        self.bitcoin_trials = BitcoinTrials(directory, self._quote_access, self._poly_access, self.transition)
        self.bitcoin_contracts = self.bitcoin_trials.sources["kalshi"]
        self.next_pair = 0.0
        self.interval = interval
        self.started = utc()
        self.heartbeat = self.started
        self.scan_started = None
        self.active_scan_id = None
        self.next_scan = 0.0
        self.store.restore_scan_history(self.started)
        with self.store.connection() as db:
            db.execute(
                "UPDATE research_notes SET status='interrupted',text='Worker restarted during this request.' WHERE status='pending'"
            )
        paused = self.store.get("paused", False)
        self.ledgers["replay"].set_paused(paused, acknowledge_risk=False)
        self.ledgers["forward"].set_paused(
            self.store.get("forward_paused", paused), acknowledge_risk=False
        )
        if not self.store.get("demo_initialized", False):
            self.store.put("replay_result", replay_demo(self.ledgers["replay"]))
            self.store.put("demo_initialized", True)

    def mode(self):
        return self.store.get("mode", "replay")

    def _quote_access(self):
        return self._access("allow_market", "permission_generation")

    def _poly_access(self):
        return self._access("allow_polymarket_us", "polymarket_generation")

    def _access(self, setting, generation):
        if not self.online or self.stop.is_set():
            return "offline", None
        if self.store.get(setting, False) is not True:
            return "disabled", None
        return "enabled", self.store.get(generation, 0)

    def paper_automation(self):
        control = self.ledgers["forward"].entry_control()
        access, _ = self._quote_access()
        available = access == "enabled"
        if control["risk_pause_reason"]:
            reason = control["risk_pause_reason"]
        elif access == "offline":
            reason = "Forward auto-trading requires an online worker."
        elif not available:
            reason = "Forward auto-trading requires market data permission."
        elif control["manual_pause"]:
            reason = "New forward paper entries are paused."
        else:
            reason = "Eligible forward contracts can open paper positions within the account limits."
        return {
            **control,
            "enabled": bool(
                available
                and not control["manual_pause"]
                and not control["risk_pause_reason"]
            ),
            "available": available,
            "reason": reason,
        }

    def desk_snapshot(self, desk):
        """Independent desktop reads share only the worker heartbeat."""
        snapshot = {"version": VERSION, "health": {"online": self.online, "heartbeat": self.heartbeat}}
        if desk == "bitcoin":
            snapshot["bitcoin"] = self.bitcoin.state()
            snapshot.update(self.bitcoin_trials.snapshot())
        elif desk == "comparison":
            with self.store.connection() as db:
                snapshot["comparison"] = monitoring.comparison(db)
            snapshot["comparison"]["bitcoin_managed"] = self.bitcoin.managed()
            snapshot.update(self.bitcoin_trials.snapshot(comparison=True))
        return snapshot

    def state(self, *, activity_hours=None, bitcoin=False):
        mode = self.mode()
        output = self.ledgers[mode].state()
        saved = self.store.get("forward_health", {})
        scans = self.store.scan_history()
        scan = (
            scans[0]
            if scans
            else {
                "id": None,
                "status": "idle",
                "started_at": None,
                "finished_at": None,
                "detail": "Waiting for the first forward scan.",
            }
        )
        if mode == "forward":
            scan_events = [
                {
                    "id": s["id"],
                    "kind": "scan",
                    "at": s["finished_at"] or s["started_at"],
                    "status": s["status"],
                    "market_id": None,
                    "message": "Forward scan " + s["status"] + ". " + s["detail"],
                }
                for s in scans
            ]
            activity = (
                output["activity"] + scan_events + self.store.local_research_activity()
            )
            output["activity"] = sorted(
                activity, key=lambda e: instant(e["at"]), reverse=True
            )[:500]
        output.update(
            {
                "version": VERSION,
                "mode": mode,
                "paper_automation": self.paper_automation(),
                "research": self.research.state(mode),
                "notifications": self.notifications.state(),
                "settings": {
                    "allow_market": self.store.get("allow_market", False),
                    "scan_interval_seconds": self.interval,
                },
                "health": {
                    "last_scan": next(
                        (s["finished_at"] for s in scans if s["finished_at"]),
                        saved.get("at"),
                    ),
                    "heartbeat": self.heartbeat,
                    "started_at": self.started,
                    "status": "Running" if self.online else "Offline replay",
                    "source_status": scan["detail"]
                    if scan["id"]
                    else saved.get("status", scan["detail"]),
                    "source_evidence": saved.get("evidence", []),
                    "scanning": scan["status"] == "running",
                    "scan": scan,
                    "recent_scans": scans,
                    "research_running": self.analyzing.locked(),
                    "scan_started": scan["started_at"],
                    "replay_result": (
                        self.store.get("replay_result", {}) if mode == "replay" else {}
                    ),
                    "online": self.online,
                },
            }
        )
        output["quotes"] = self.quotes.state()
        if activity_hours in monitoring.WINDOWS:
            clock = output.get("clock") if mode == "replay" else utc()
            end = int(instant(clock or utc()).timestamp()) + 1
            start = end - activity_hours * 3600
            stride = monitoring.WINDOWS[activity_hours]
            with self.store.connection() as db:
                scans = monitoring.counts(db, "scans", start, end, stride) if mode == "forward" else None
            output["activity_history"] = {
                "hours": activity_hours, "start_at": monitoring.stamp(start),
                "end_at": monitoring.stamp(end), "bin_seconds": stride, "scans": scans,
                "decisions": self.ledgers[mode].decision_counts(start, end, stride),
            }
        if bitcoin:
            output["bitcoin"] = self.bitcoin.state()
            with self.store.connection() as db:
                output["comparison"] = monitoring.comparison(db)
        return output

    def record_comparison(self):
        """Take one common-time sample, using marks the two workers could observe."""
        with self.transition:
            if self._quote_access()[0] != "enabled":
                return
            btc = self.bitcoin.state()
            if not btc.get("enabled") or not btc.get("started_at"):
                return
            at = utc()
            now = instant(at)
            active, hold = btc["portfolio"], btc["benchmark"]
            quote = btc.get("quote") or {}
            if not (hold["filled_orders"] and quote.get("fresh") and quote_fresh(quote, now)
                    and active.get("mark_fresh") and hold.get("mark_fresh")):
                return
            weather = self.ledgers["forward"].portfolio_mark(now)
            if not monitoring.comparable_weather(weather):
                return
            with self.store.connection() as db:
                if not db.execute("SELECT 1 FROM paired_equity LIMIT 1").fetchone():
                    start = instant(btc["started_at"])
                    if not btc.get("manual_trades") and 0 <= (now-start).total_seconds() <= 60:
                        opening = self.ledgers["forward"].portfolio_mark(start, require_known_at=True)
                        if monitoring.comparable_weather(opening):
                            monitoring.record_pair(db, btc["started_at"], opening["equity_cents"],
                                                   active["initial_cash_cents"], hold["initial_cash_cents"], opening=True)
                            db.commit()
                monitoring.record_pair(db, at, weather["equity_cents"], active["equity_cents"], hold["equity_cents"])

    def scan(self, *, scheduled=False):
        with self.transition:
            if not self.online or self.stop.is_set():
                raise ValueError("An online worker is required for a forward scan.")
            if self.scanning.locked():
                return self.active_scan_id
            if scheduled and time.monotonic() < self.next_scan:
                return None
            self.scan_started = utc()
            scan_id = self.store.begin_scan(self.scan_started)
            self.active_scan_id = scan_id
            self.scanning.acquire()
            revision = self.scan_revision
            allowed = self.store.get("allow_market", False)
            generation = self.store.get("permission_generation", 0)

        def run():
            evidence = []
            outcome = "failed"
            try:

                def permitted():
                    return (
                        self.online
                        and not self.stop.is_set()
                        and self.store.get("allow_market", False)
                        and self.store.get("permission_generation", 0) == generation
                    )

                data = collect(
                    self.directory / "feeds",
                    utc(),
                    allowed,
                    permission_check=permitted,
                    permission_lock=self.transition,
                )
                now = utc()
                settlement_errors = []
                with self.transition:
                    revoked = allowed and not permitted()
                    if allowed and not revoked:
                        ledger = self.ledgers["forward"]
                        ledger.ingest(data.get("packets", []), now)
                        for settlement in data.get("settlements", []):
                            if not ledger.has_market(settlement["market_id"]):
                                continue  # Historical outcomes are useful for calibration on the feed side.
                            try:
                                ledger.settle(
                                    settlement["market_id"],
                                    settlement["result"],
                                    settlement["observed_at"],
                                    settlement["source"],
                                )
                            except ValueError as exc:
                                settlement_errors.append(str(exc))
                evidence = [
                    {
                        k: s.get(k)
                        for k in (
                            "title",
                            "url",
                            "published_at",
                            "available_at",
                            "detail",
                        )
                    }
                    for s in data.get("evidence", [])[:12]
                    if isinstance(s, dict)
                ]
                status = str(data.get("status", "Updated"))
                if settlement_errors:
                    status += " Settlement review required. " + " ".join(
                        dict.fromkeys(settlement_errors)
                    )
                if revoked:
                    status = "Market permission changed during collection. The worker discarded this venue scan."
                    evidence = []
                outcome = (
                    "partial"
                    if data.get("degraded") or settlement_errors or revoked
                    else "complete"
                )
            except Exception as exc:
                LOG.exception("Forward collection failed")
                status = f"Source scan needs attention ({type(exc).__name__}). The worker will retry."
            with self.transition:
                finished = utc()
                try:
                    recorded = self.store.finish_scan(
                        scan_id, outcome, finished, status[:1500], evidence
                    )
                    if recorded and not self.stop.is_set():
                        try:
                            self.notify(status, outcome != "complete", finished)
                            if (
                                outcome != "failed"
                                and allowed
                                and permitted()
                                and self.store.get("research_auto", False)
                            ):
                                self.review_next_market(finished)
                        except Exception:
                            LOG.exception(
                                "Post-scan notification or research scheduling failed"
                            )
                finally:
                    self.next_scan = (
                        time.monotonic() + self.interval
                        if self.scan_revision == revision
                        else 0.0
                    )
                    self.active_scan_id = None
                    self.scanning.release()
                    self.wake.set()

        try:
            threading.Thread(target=run, daemon=True, name="source-collection").start()
        except RuntimeError:
            with self.transition:
                self.store.finish_scan(
                    scan_id,
                    "failed",
                    utc(),
                    "The source collection thread could not start.",
                    [],
                )
                self.next_scan = time.monotonic() + self.interval
                self.active_scan_id = None
                self.scanning.release()
                self.wake.set()
        return scan_id

    def notify(self, status, degraded, now):
        if not self.online:
            return
        mail = self.notifications.state()
        if not (mail.get("enabled") and mail.get("ready")):
            return

        def send():
            try:
                forward = self.ledgers["forward"].state()
                reason = forward.get("risk_pause_reason")
                unknown = any(
                    order.get("status") == "unknown"
                    for order in forward["portfolio"]["orders"]
                )
                report = status
                if reason:
                    report += " A paper risk stop requires review."
                if unknown:
                    report += " An unresolved paper order requires review."
                self.notifications.tick(
                    report, bool(degraded or reason or unknown), now
                )
            except Exception:
                LOG.exception("Notification delivery failed")

        threading.Thread(target=send, daemon=True, name="forward-notifications").start()

    def review_next_market(self, now):
        as_of = instant(now)
        candidates = []
        for market in self.ledgers["forward"].state()["markets"]:
            if market.get("decision") in ("Settled", "Held") or market.get(
                "settlement"
            ):
                continue
            try:
                if instant(market.get("close_at")) > as_of:
                    candidates.append(market)
            except ValueError:
                continue
        if not candidates:
            return
        candidates.sort(key=lambda market: market["id"])
        previous = self.store.get("last_auto_market", "")
        pick = next((m for m in candidates if m["id"] > previous), candidates[0])
        if self.start_research(pick, "forward", manual=False):
            self.store.put("last_auto_market", pick["id"])

    def start_research(self, market, mode, *, manual=True):
        if not self.analyzing.acquire(blocking=False):
            if manual:
                raise ValueError("A research review is already running.")
            return False

        def run():
            try:
                self.research.run(
                    market, mode, local_only=not self.online, manual=manual
                )
            except Exception:
                LOG.exception("Analyst job failed")
            finally:
                self.analyzing.release()

        threading.Thread(target=run, daemon=True, name="analyst-review").start()
        return True

    def schedule(self):
        collectors = []
        if self.online:
            jobs = {"public-quote-sampler": self.quotes.run, "bitcoin-paper": self.bitcoin.run,
                    "bitcoin-trials": self.bitcoin_trials.run, "bitcoin-settlements": self.bitcoin_trials.settle}
            jobs.update({key + "-contract-quotes": source.run for key, source in self.bitcoin_trials.sources.items()})
            for name, fn in jobs.items():
                thread = threading.Thread(target=fn, args=(self.stop,), name=name, daemon=True)
                thread.start()
                collectors.append(thread)
        try:
            while not self.stop.is_set():
                self.heartbeat = utc()
                if self.online and time.monotonic() >= self.next_pair:
                    self.next_pair = time.monotonic() + 20
                    try:
                        self.record_comparison()
                    except Exception:
                        LOG.exception("Could not record a common portfolio mark")
                if (
                    self.online
                    and time.monotonic() >= self.next_scan
                    and not self.scanning.locked()
                ):
                    self.scan(scheduled=True)
                self.wake.wait(2)
                self.wake.clear()
        finally:
            self.quotes.wake.set()
            for source in self.bitcoin_trials.sources.values():
                source.wake.set()
            for thread in collectors:
                thread.join(timeout=1)

    def action(self, action, value):
        with self.transition:
            mode = self.mode()
            if action in ("btc-contract-order", "btc-contract-auto", "btc-contract-limits"):
                return self.bitcoin_trials.action(action, value)
            if action == "btc-spot-order":
                receipt = self.bitcoin.manual_order(value)
                self.next_pair = 0.0
                self.wake.set()
                return {"ok": True, "receipt": receipt}
            if action == "btc-poly-feed" and type(value) is bool:
                if value and not self.online:
                    raise ValueError("An online worker is required for Polymarket US data.")
                if self.store.get("allow_polymarket_us", False) != value:
                    self.store.put_many({"allow_polymarket_us": value,
                        "polymarket_generation": self.store.get("polymarket_generation", 0) + 1})
                    self.bitcoin_trials.source_permission_changed(("polymarket_us", "polymarket_us_hourly"))
            elif action in ("btc-feed", "btc-auto") and type(value) is bool:
                if value and not self.online:
                    raise ValueError("An online worker is required for the Bitcoin paper trial.")
                if action == "btc-feed":
                    self.bitcoin.configure(value)
                else:
                    self.bitcoin.set_auto(value)
                self.next_pair = 0.0
                self.wake.set()
            elif action == "mode" and value in ("replay", "forward"):
                self.store.put("mode", value)
            elif action in ("pause", "resume"):
                paused = action == "pause"
                if isinstance(value, dict) and value.get("source") == "markets":
                    if not paused:
                        gate = self.paper_automation()
                        if not gate["available"] or gate["risk_pause_reason"]:
                            raise ValueError(gate["reason"])
                    self.store.put("forward_paused", paused)
                    self.ledgers["forward"].set_paused(paused, acknowledge_risk=False)
                    return {"ok": True, "paper_automation": self.paper_automation()}
                if isinstance(value, dict) and value.get("source") not in (
                    None,
                    "worker",
                ):
                    raise ValueError("Unknown paper-entry control source.")
                self.store.put_many({"paused": paused, "forward_paused": paused})
                for ledger in self.ledgers.values():
                    ledger.set_paused(paused)
            elif action == "permission" and type(value) is bool:
                if self.store.get("allow_market", False) != value:
                    self.store.put_many(
                        {
                            "permission_generation": self.store.get(
                                "permission_generation", 0
                            )
                            + 1,
                            "allow_market": value,
                        }
                    )
                    self.quotes.permission_changed()
                    self.bitcoin_trials.source_permission_changed(("kalshi",))
                    self.scan_revision += 1
                    self.next_scan = 0.0
                    self.wake.set()
            elif action == "scan":
                return {"ok": True, "scan_id": self.scan()}
            elif action == "replay":
                self.store.put("replay_result", replay_demo(self.ledgers["replay"]))
            elif action == "configure_research":
                self.research.configure(value)
            elif action == "research_auto" and type(value) is bool:
                self.store.put("research_auto", value)
            elif action == "research":
                markets = self.ledgers[mode].state().get("markets", [])
                market = next((m for m in markets if m.get("id") == value), None)
                if not market:
                    raise ValueError("Select an available market.")
                self.start_research(market, mode)
            else:
                raise ValueError("Unsupported action or invalid value.")
        return {"ok": True}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        conn, addr = super().get_request()
        conn.settimeout(12)
        return conn, addr


class Handler(BaseHTTPRequestHandler):
    server_version = "PMB/0.1"
    sys_version = ""

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        if ctype == "application/json":
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def permitted(self, private):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("//" + host)
            if (
                parsed.hostname not in self.server.hosts
                or parsed.username
                or parsed.password
                or parsed.path
                or not host
            ):
                raise ValueError()
            origin = self.headers.get("Origin")
            if origin:
                loc = urlsplit(origin)
                if (
                    loc.scheme not in ("http", "https")
                    or loc.netloc.lower() != host.lower()
                ):
                    raise ValueError()
        except ValueError:
            self.reply(403, {"error": "Unrecognized worker origin."})
            return False
        if private:
            auth = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(auth, b"Bearer " + self.server.key.encode()):
                self.reply(
                    401, {"error": "Open the worker connection link to authenticate."}
                )
                return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.permitted(path.startswith("/api/")):
            return
        if path == "/healthz":
            payload = {"service": "pmb-paper", "version": VERSION}
            nonce = parse_qs(urlsplit(self.path).query).get("challenge", [""])[0]
            if len(nonce) == 48 and all(c in "0123456789abcdef" for c in nonce):
                payload.update(
                    {
                        "pid": os.getpid(),
                        "online": self.server.desk.online,
                        "proof": hmac.new(
                            self.server.key.encode(), nonce.encode(), "sha256"
                        ).hexdigest(),
                    }
                )
            return self.reply(200, payload)
        if path == "/api/ping":
            return self.reply(200, {"service": "pmb-paper", "pid": os.getpid()})
        if path == "/api/notifications/preview":
            return self.reply(200, self.server.desk.notifications.preview())
        if path in ("/api/bitcoin", "/api/comparison", "/api/status", "/api/state"):
            params = parse_qs(urlsplit(self.path).query)
            hours = params.get("activity_hours", [None])[0]
            if hours is not None and hours not in {"24", "48", "168"}:
                return self.reply(400, {"error": "Choose 24, 48 or 168 activity hours."})
            try:
                if path != "/api/state":
                    return self.reply(200, self.server.desk.desk_snapshot(path.removeprefix("/api/")))
                return self.reply(200, self.server.desk.state(
                    activity_hours=int(hours) if hours else None,
                    bitcoin=params.get("workspace") == ["bitcoin"],
                ))
            except Exception:
                LOG.exception("Snapshot failed")
                return self.reply(500, {"error": "Worker snapshot could not be read."})
        if path.startswith("/api/"):
            return self.reply(404, {"error": "Unknown endpoint."})
        ui = (ROOT / "ui").resolve()
        relative = "index.html" if path == "/" else unquote(path).lstrip("/")
        candidate = (ui / relative).resolve()
        if (
            not candidate.is_relative_to(ui)
            or not candidate.is_file()
            or candidate.suffix.lower()
            not in {
                ".html",
                ".css",
                ".js",
                ".mjs",
                ".woff2",
                ".woff",
                ".png",
                ".svg",
                ".ico",
                ".webp",
                ".ttf",
            }
        ):
            return self.reply(404, {"error": "File unavailable."})
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if candidate.suffix in (".mjs", ".js"):
            mime = "application/javascript"
        return self.reply(200, candidate.read_bytes(), mime)

    def do_POST(self):
        if not self.permitted(True):
            return
        if self.path == "/api/worker/stop":
            self.reply(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != "/api/action":
            return self.reply(404, {"error": "Unknown endpoint."})
        if self.headers.get_content_type() != "application/json":
            return self.reply(415, {"error": "Expected JSON."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                return self.reply(413, {"error": "Invalid request size."})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Invalid action.")
            result = self.server.desk.action(data.get("action"), data.get("value"))
            self.reply(200, result)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)[:260]})
        except Exception:
            LOG.exception("Worker action failed")
            self.reply(500, {"error": "Action failed. Review worker health."})


def serve(directory, port, offline=False, interval=300):
    with exclusive_worker(directory):
        key = access_key(directory)
        with Server(("127.0.0.1", port), Handler) as http:
            desk = Desk(directory, interval, not offline)
            http.desk, http.key = desk, key
            hosts = os.environ.get("PMB_ALLOWED_HOSTS", "").lower().split(",")
            http.hosts = {
                "localhost",
                "127.0.0.1",
                *(h.strip() for h in hosts if h.strip()),
            }
            record = {
                "port": http.server_address[1],
                "pid": os.getpid(),
                "started": utc(),
                "root": str(ROOT),
            }
            path = directory / "connection.json"
            atomic_text(path, json.dumps(record))
            thread = threading.Thread(
                target=desk.schedule, name="paper-scheduler", daemon=True
            )
            thread.start()

            def finish(*unused):
                desk.stop.set()
                desk.wake.set()
                threading.Thread(target=http.shutdown, daemon=True).start()

            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, finish)
                signal.signal(signal.SIGINT, finish)
            try:
                http.serve_forever(poll_interval=0.3)
            finally:
                desk.stop.set()
                desk.wake.set()
                thread.join(timeout=2)
                path.unlink(missing_ok=True)


def connection(directory):
    try:
        record = json.loads((directory / "connection.json").read_text())
        port = record["port"]
        if type(port) is not int or not 0 < port < 65536:
            return None
        key = (directory / "access.key").read_text().strip()
        if not key:
            return None
        base = f"http://127.0.0.1:{port}/"
        nonce = secrets.token_hex(24)
        req = Request(base + "healthz?challenge=" + nonce)
        local = build_opener(ProxyHandler({}), NoRedirect())
        with local.open(req, timeout=0.8) as response:
            answer = json.loads(response.read(1024))
        proof = hmac.new(key.encode(), nonce.encode(), "sha256").hexdigest()
        if answer.get("pid") == record["pid"] and hmac.compare_digest(
            str(answer.get("proof", "")), proof
        ):
            return {"url": base + "#token=" + key, "online": answer.get("online", True)}
    except (OSError, ValueError, KeyError, TypeError, URLError):
        pass
    return None


def desktop(directory, port, offline):
    existing = connection(directory)
    if existing:
        if offline and existing.get("online"):
            raise ValueError(
                "This data directory already has an online worker. Select a separate data directory for an offline session."
            )
        return existing
    args = [
        sys.executable,
        "-I",
        "-B",
        str(ROOT / "pmb.py"),
        "--data-dir",
        str(directory),
        "serve",
        "--port",
        str(port),
    ]
    if offline:
        args.append("--offline")
    opts = (
        {"start_new_session": True}
        if os.name != "nt"
        else {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    )
    with (directory / "startup.log").open("ab") as stream:
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            close_fds=True,
            **opts,
        )
    for i in range(120):
        time.sleep(0.12)
        answer = connection(directory)
        if answer:
            return answer
        if i > 20 and child.poll() is not None:
            break
    raise RuntimeError(f"Worker did not become ready. See {directory / 'startup.log'}.")


def main():
    ap = argparse.ArgumentParser(
        description="Prediction Market Bot personal paper worker"
    )
    ap.add_argument("--data-dir", type=Path, default=data_home())
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("serve", "desktop"):
        p = sub.add_parser(name)
        p.add_argument(
            "--port", type=int, default=int(os.environ.get("PMB_PORT", DEFAULT_PORT))
        )
        p.add_argument(
            "--offline",
            action="store_true",
            default=os.environ.get("PMB_OFFLINE") == "1",
        )
        if name == "serve":
            p.add_argument(
                "--interval",
                type=int,
                default=300,
                help="forward scan period in seconds, minimum 60",
            )
    sub.add_parser(
        "replay-check",
        help="verify two identical source-to-settlement replays in an isolated SQLite account",
    )
    sub.add_parser(
        "connect", help="print a private connection URL for the running worker"
    )
    sub.add_parser(
        "mail-preview", help="show the local forward digest without sending email"
    )
    end = sub.add_parser("stop", help="gracefully stop this local worker")
    end.add_argument(
        "--matching-root",
        action="store_true",
        help="only stop a worker launched from this installation",
    )
    options = ap.parse_args()
    directory = private_dir(options.data_dir)
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        if options.command == "replay-check":
            import tempfile

            with tempfile.TemporaryDirectory() as work:
                db = Path(work) / "paper.sqlite"
                first = Ledger(db)
                try:
                    a = replay_demo(first)
                finally:
                    first.close()
                second = Ledger(db)
                try:
                    b = replay_demo(second)
                finally:
                    second.close()
                if a != b or not a.get("synthetic") or not a.get("comparison"):
                    raise RuntimeError("Paper replay was not reproducible.")
                print(json.dumps(a))
        elif options.command == "mail-preview":
            preview = Notifications(Store(directory)).preview()
            print(preview["subject"] + "\n\n" + preview["body"])
        elif options.command == "serve":
            if not 60 <= options.interval <= 86400:
                raise ValueError("Scan interval must be 60 to 86400 seconds.")
            serve(directory, options.port, options.offline, options.interval)
        elif options.command == "desktop":
            print(json.dumps(desktop(directory, options.port, options.offline)))
        elif options.command == "stop":
            record_path = directory / "connection.json"
            if not record_path.exists():
                return 0
            record = json.loads(record_path.read_text())
            if options.matching_root and Path(record.get("root", "")).resolve() != ROOT:
                return 0
            link = connection(directory)
            if not link:
                return 0
            base, token = link["url"].split("#token=", 1)
            req = Request(
                base + "api/worker/stop",
                data=b"",
                headers={"Authorization": "Bearer " + token},
                method="POST",
            )
            with build_opener(ProxyHandler({}), NoRedirect()).open(
                req, timeout=3
            ) as result:
                result.read(512)
            for _ in range(80):
                if not record_path.exists():
                    break
                time.sleep(0.05)
        else:
            link = connection(directory)
            if not link:
                raise RuntimeError("The worker is offline.")
            print(link["url"])
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
