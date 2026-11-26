import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.server import Desk, Handler, Server
from worker.storage import exclusive_worker


class DesktopServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.desk = Desk(Path(cls.tmp.name), online=False)
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.server.desk = cls.desk
        cls.server.key = "test-local-token"
        cls.server.hosts = {"127.0.0.1", "localhost"}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        for ledger in cls.desk.ledgers.values():
            ledger.close()
        cls.tmp.cleanup()

    def api(self, path, *, headers=None, body=None, private=True):
        host, port = self.server.server_address
        conn = http.client.HTTPConnection(host, port)
        request_headers = {"Content-Type": "application/json"}
        if private:
            request_headers["Authorization"] = "Bearer test-local-token"
        request_headers.update(headers or {})
        conn.request(
            "POST" if body is not None else "GET",
            path,
            json.dumps(body).encode() if body is not None else None,
            request_headers,
        )
        response = conn.getresponse()
        status = response.status
        data = response.read()
        conn.close()
        return status, data

    def test_private_state_is_token_scoped_and_never_contains_the_access_key(self):
        self.assertEqual(self.api("/api/state", private=False)[0], 401)
        status, raw = self.api("/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(b"test-local-token", raw)
        state = json.loads(raw)
        self.assertGreater(len(state["markets"]), 0)
        self.assertEqual(state["research"]["configured"], [])

    def test_workspace_reads_preserve_the_mode_and_have_separate_paper_balances(self):
        mode = self.desk.mode()
        status, raw = self.api("/api/state?workspace=bitcoin")
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertEqual(state["bitcoin"]["portfolio"]["initial_cash_cents"], 1000000)
        self.assertFalse(state["bitcoin"]["enabled"])
        self.assertIsNone(state["comparison"]["start_at"])
        self.assertEqual(self.desk.mode(), mode)
        for hours, length in ((24,24), (48,24), (168,28)):
            status, raw = self.api(f"/api/state?activity_hours={hours}")
            self.assertEqual(status, 200)
            history = json.loads(raw)["activity_history"]
            self.assertEqual(len(history["decisions"]["counts"]), length)
            self.assertEqual(history["hours"], hours)
        self.assertEqual(self.api("/api/state?activity_hours=22")[0], 400)
        self.assertEqual(self.api("/api/state?workspace=bitcoin", private=False)[0], 401)

    def test_origin_content_type_and_host_validation(self):
        host, port = self.server.server_address
        for origin, expected in ((f"http://{host}:{port}", 200), ("null", 403)):
            with self.subTest(origin=origin):
                status, _ = self.api(
                    "/api/action", headers={"Origin": origin}, body={"action": "replay"}
                )
                self.assertEqual(status, expected)
        self.assertEqual(
            self.api("/healthz", headers={"Host": "unrelated.test"})[0], 403
        )
        self.assertEqual(
            self.api(
                "/api/action",
                headers={"Origin": "https://unrelated.test"},
                body={"action": "pause"},
            )[0],
            403,
        )
        self.assertEqual(
            self.api(
                "/api/action",
                headers={"Content-Type": "text/plain"},
                body={"action": "pause"},
            )[0],
            415,
        )
        self.assertEqual(self.api("/../worker/server.py")[0], 404)

    def test_two_clients_read_same_replay_and_can_switch_to_empty_forward_account(self):
        a = json.loads(self.api("/api/state")[1])
        self.assertEqual(self.api("/api/action", body={"action": "replay"})[0], 200)
        b = json.loads(self.api("/api/state")[1])
        self.assertEqual(a["portfolio"], b["portfolio"])
        self.api("/api/action", body={"action": "mode", "value": "forward"})
        try:
            live = json.loads(self.api("/api/state")[1])
            self.assertEqual(live["mode"], "forward")
            self.assertEqual(live["markets"], [])
            self.assertEqual(live["portfolio"]["realized_cents"], 0)
        finally:
            self.api("/api/action", body={"action": "mode", "value": "replay"})

    def test_worker_ownership_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            with exclusive_worker(p), self.assertRaises(RuntimeError):
                with exclusive_worker(p):
                    pass

    def test_automatic_reviews_rotate_open_markets_and_retry_if_busy(self):
        now = "2026-11-26T16:00:00Z"
        open_contract = {"decision": "Watch", "close_at": "2026-11-27T21:00:00Z"}
        rows = [
            {**open_contract, "id": "C"},
            {**open_contract, "id": "A"},
            {**open_contract, "id": "B", "decision": "Held"},
            {**open_contract, "id": "D", "close_at": now},
            {**open_contract, "id": "E", "close_at": None},
        ]
        with (
            patch.object(
                self.desk.ledgers["forward"], "state", return_value={"markets": rows}
            ),
            patch.object(
                self.desk, "start_research", side_effect=[True, False, True, True]
            ) as start,
        ):
            for _ in range(4):
                self.desk.review_next_market(now)
            self.assertEqual(
                [call.args[0]["id"] for call in start.call_args_list],
                ["A", "C", "C", "A"],
            )
            self.assertTrue(
                all(
                    call.args[1] == "forward" and not call.kwargs["manual"]
                    for call in start.call_args_list
                )
            )

    def test_backfill_settlement_for_unseen_market_does_not_fail_scan(self):
        self.desk.online = True
        self.desk.store.put("allow_market", True)
        result = {
            "packets": [],
            "settlements": [
                {
                    "market_id": "unseen",
                    "result": "yes",
                    "observed_at": "2026-11-20T00:00:00Z",
                    "source": "kalshi",
                }
            ],
            "evidence": [],
            "status": "Forward sources current",
        }
        try:
            with patch("worker.server.collect", return_value=result):
                status, body = self.api("/api/action", body={"action": "scan"})
                self.assertEqual(status, 200)
                requested_id = json.loads(body)["scan_id"]
                deadline = time.monotonic() + 3
                while self.desk.scanning.locked() and time.monotonic() < deadline:
                    time.sleep(0.02)
            status, body = self.api("/api/state")
            self.assertEqual(status, 200)
            receipt = json.loads(body)["health"]["scan"]
            self.assertEqual(
                (receipt["id"], receipt["status"]), (requested_id, "complete")
            )
            self.assertTrue(receipt["finished_at"])
            self.assertEqual(
                self.desk.store.get("forward_health")["status"],
                "Forward sources current",
            )
        finally:
            self.desk.online = False
            self.desk.store.put("allow_market", False)

    def test_nonce_proof_authenticates_listener_without_sending_private_token(self):
        import hashlib
        import hmac
        import os

        nonce = "62417a" * 8
        status, raw = self.api("/healthz?challenge=" + nonce, private=False)
        answer = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(answer["pid"], os.getpid())
        self.assertEqual(
            answer["proof"],
            hmac.new(
                self.server.key.encode(), nonce.encode(), hashlib.sha256
            ).hexdigest(),
        )
        self.assertNotIn(
            "proof", json.loads(self.api("/healthz?challenge=short", private=False)[1])
        )

    def test_damaged_existing_access_key_does_not_regenerate_anonymous_access(self):
        from worker.storage import access_key

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            key = access_key(path)
            self.assertEqual(access_key(path), key)
            (path / "access.key").write_text(" ")
            with self.assertRaises(RuntimeError):
                access_key(path)

    def test_mail_preview_is_private_and_does_not_deliver(self):
        with patch("smtplib.SMTP_SSL") as delivery:
            self.assertEqual(
                self.api("/api/notifications/preview", private=False)[0], 401
            )
            status, response = self.api("/api/notifications/preview")
            self.assertEqual(status, 200)
            self.assertIn("subject", json.loads(response))
            delivery.assert_not_called()
