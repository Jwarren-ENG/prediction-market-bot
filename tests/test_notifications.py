import concurrent.futures
import json
import os
import smtplib
import ssl
import tempfile
import unittest
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from worker import notifications as mail, weather
from worker.storage import Store

CONFIG = {
    "PMB_MAIL_ENABLED": "1",
    "PMB_MAIL_TO": "owner@example.invalid",
    "PMB_MAIL_FROM": "paper@example.invalid",
    "PMB_SMTP_HOST": "smtp.example.invalid",
    "PMB_SMTP_USER": "fixture-user",
    "PMB_SMTP_PASSWORD": "fixture-secret",
}
START = weather.instant("2026-11-27T13:00:00Z")
PRIVATE_STATUS = "Venue quote 0.75. KXHIGHNY-test. Position 300. Bearer private-token. owner@example.invalid"


class FakeSMTP:
    def __init__(self, harness):
        self.h = harness

    def ehlo(self):
        return self.h.action("ehlo", (250, b"mock"))

    def starttls(self, *, context):
        self.h.contexts.append(context)
        return self.h.action("starttls", (220, b"ready"))

    def login(self, user, password):
        return self.h.action("login", (235, b"ok"))

    def mail(self, sender):
        return self.h.action("mail", (250, b"ok"))

    def rcpt(self, recipient):
        self.h.recipients.append(recipient)
        return self.h.action("rcpt", (250, b"ok"))

    def data(self, message):
        self.h.messages.append(BytesParser(policy=policy.default).parsebytes(message))
        return self.h.action("data", (250, b"accepted"))

    def close(self):
        self.h.action("close", None)


class Harness:
    def __init__(self):
        self.events = []
        self.scripts = {}
        self.contexts = []
        self.messages = []
        self.recipients = []

    def action(self, name, default):
        self.events.append(name)
        op = self.scripts.get(name)
        if isinstance(op, BaseException):
            raise op
        return op() if callable(op) else (default if op is None else op)

    def connect(self, name, host, port, **options):
        if "context" in options:
            self.contexts.append(options["context"])
        self.action(name, None)
        return FakeSMTP(self)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.mail = mail.Notifications(self.store)
        self.smtp = Harness()
        patches = (
            patch.dict(os.environ, CONFIG),
            patch.object(
                mail.smtplib,
                "SMTP_SSL",
                side_effect=lambda *a, **k: self.smtp.connect("ssl", *a, **k),
            ),
            patch.object(
                mail.smtplib,
                "SMTP",
                side_effect=lambda *a, **k: self.smtp.connect("plain", *a, **k),
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("Network is forbidden in mail tests."),
            ),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Keep the Windows cryptographic runtime environment intact, while isolating SMTP overrides.
        for name in ("PMB_SMTP_SECURITY", "PMB_SMTP_PORT"):
            if name not in CONFIG:
                os.environ.pop(name, None)

    def tick(self, seconds=0, degraded=False, instance=None):
        return (instance or self.mail).tick(
            PRIVATE_STATUS, degraded, weather.iso(START + timedelta(seconds=seconds))
        )

    def rows(self):
        with self.store.connection() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM mail_outbox ORDER BY created,rowid")
            ]

    def test_disabled_unconfigured_and_preview_never_connect(self):
        os.environ["PMB_MAIL_ENABLED"] = "0"
        preview = self.mail.preview()
        self.assertIn("Open Prediction Market Bot", preview["body"])
        self.assertFalse(self.tick()["enabled"])
        os.environ["PMB_MAIL_ENABLED"] = "yes"
        self.assertFalse(self.tick()["enabled"])
        os.environ["PMB_MAIL_ENABLED"] = "1"
        os.environ.pop("PMB_SMTP_PASSWORD")
        pending = self.tick()
        self.assertTrue(pending["enabled"])
        self.assertFalse(pending["ready"])
        self.assertIn("PMB_SMTP_PASSWORD", pending["status"])
        self.assertEqual(self.smtp.events, [])
        self.assertFalse(self.rows())

    def test_only_one_explicit_recipient_and_encrypted_settings(self):
        for key, value in (
            ("PMB_MAIL_TO", "a@example.invalid,b@example.invalid"),
            ("PMB_MAIL_TO", "a@example.invalid\r\nBcc: other@example.invalid"),
            ("PMB_MAIL_FROM", "Someone <a@example.invalid>"),
            ("PMB_SMTP_HOST", "smtps://smtp.example.invalid/relay"),
            ("PMB_SMTP_PORT", "65536"),
            ("PMB_SMTP_SECURITY", "plain"),
        ):
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, {key: value}),
            ):
                self.assertFalse(self.tick()["ready"])
        self.assertFalse(self.smtp.events)
        self.assertFalse(self.rows())

    def test_digest_is_generic_private_and_deduplicated_on_restart(self):
        first = self.tick()
        self.assertTrue(first["ready"])
        self.assertEqual(first["last_delivery"]["state"], "accepted")
        self.assertEqual(
            first["last_delivery"]["detail"], "SMTP server accepted the message."
        )
        self.assertEqual(
            self.smtp.events, ["ssl", "ehlo", "login", "mail", "rcpt", "data", "close"]
        )
        self.assertEqual(self.smtp.recipients, ["owner@example.invalid"])
        original = self.smtp.messages[0]
        self.assertEqual(original["Subject"], "PMB paper research review · 2026-11-27")
        self.assertIn("2026-11-27 at 08:00 New York time", original.get_content())
        self.assertIn("pmb.invalid", original["Message-ID"])
        self.tick(600, instance=mail.Notifications(self.store))
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertTrue(self.smtp.contexts[0].check_hostname)
        self.assertEqual(self.smtp.contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(
            self.smtp.contexts[0].minimum_version, ssl.TLSVersion.TLSv1_2
        )
        saved = (
            json.dumps(self.rows())
            + json.dumps(self.mail.state())
            + original.get_content()
        )
        for sensitive in (
            "private-token",
            "fixture-secret",
            "fixture-user",
            "owner@example.invalid",
            "KXHIGHNY-test",
            "0.75",
            "Position 300",
        ):
            self.assertNotIn(sensitive, saved)

    def test_midnight_is_new_york_midnight_and_clock_rewind_does_not_repeat(self):
        def visit(value):
            return self.mail.tick(PRIVATE_STATUS, False, value)

        visit("2026-11-28T04:59:55Z")
        visit("2026-11-28T05:00:02Z")
        visit("2026-11-28T04:59:59Z")
        self.assertEqual(
            [m["Subject"] for m in self.smtp.messages],
            [
                "PMB paper research review · 2026-11-27",
                "PMB paper research review · 2026-11-28",
            ],
        )

    def test_one_alert_per_failure_and_recovery_episode(self):
        for minute, down in enumerate((False, True, True, False, False, True, False)):
            self.tick(minute * 60, down)
        self.assertEqual(
            [r["kind"] for r in self.rows()],
            ["digest", "degraded", "recovered", "degraded", "recovered"],
        )
        self.assertEqual(len({m["Message-ID"] for m in self.smtp.messages}), 5)
        self.assertIn(
            "last successful worker check", self.smtp.messages[1].get_content()
        )
        self.assertNotIn("private-token", self.smtp.messages[1].get_content())
        self.assertIn("succeeded", self.smtp.messages[2].get_content())

    def test_starttls_never_authenticates_or_sends_in_plaintext(self):
        os.environ["PMB_SMTP_SECURITY"] = "starttls"
        self.smtp.scripts["starttls"] = smtplib.SMTPNotSupportedError("private-token")
        result = self.tick()
        self.assertEqual(self.smtp.events, ["plain", "ehlo", "starttls", "close"])
        self.assertEqual(result["last_attempt"]["state"], "failed")
        self.assertNotIn("private-token", str(result))
        self.smtp.events.clear()
        self.smtp.scripts.clear()
        self.tick(86400)
        self.assertEqual(
            self.smtp.events,
            [
                "plain",
                "ehlo",
                "starttls",
                "ehlo",
                "login",
                "mail",
                "rcpt",
                "data",
                "close",
            ],
        )
        self.assertTrue(self.smtp.contexts[-1].check_hostname)

    def test_bounded_pre_data_retry_backoff_and_no_certificate_downgrade(self):
        self.smtp.scripts["rcpt"] = (450, b"temporary private-token")
        self.assertEqual(self.tick()["pending"], 1)
        self.tick(45)
        self.assertEqual(self.smtp.events.count("rcpt"), 1)
        self.tick(65)
        self.tick(150)
        self.assertEqual(self.smtp.events.count("rcpt"), 2)
        state = self.tick(370)
        self.assertEqual(state["last_attempt"]["state"], "failed")
        self.tick(7200)
        self.assertEqual(self.smtp.events.count("rcpt"), 3)
        self.assertNotIn("data", self.smtp.events)
        self.smtp.scripts.clear()
        self.smtp.scripts["ssl"] = ssl.SSLCertVerificationError(
            "invalid certificate private-token"
        )
        state = self.tick(86400)
        self.assertEqual(state["last_attempt"]["state"], "failed")
        self.assertNotIn("plain", self.smtp.events)
        self.assertNotIn("private-token", str(state))

    def test_unknown_after_data_is_held_for_manual_review_and_never_retried(self):
        self.smtp.scripts["data"] = smtplib.SMTPServerDisconnected("private-token")
        state = self.tick()
        self.assertEqual(state["last_attempt"]["state"], "review")
        self.assertEqual(state["review_required"], 1)
        self.assertEqual(
            state["review_items"][0]["message_id"], self.smtp.messages[0]["Message-ID"]
        )
        self.assertNotIn("private-token", str(state))
        self.tick(4000, instance=mail.Notifications(self.store))
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(self.rows()[0]["attempts"], 1)

    def test_explicit_data_rejection_is_distinct_from_unknown_acceptance(self):
        self.smtp.scripts["data"] = smtplib.SMTPDataError(554, b"private-token")
        state = self.tick()
        self.assertEqual(state["last_attempt"]["state"], "failed")
        self.assertEqual(state["review_required"], 0)
        self.assertIn("554", state["last_attempt"]["detail"])
        self.tick(500)
        self.assertEqual(len(self.smtp.messages), 1)

    def test_restart_recovers_preflight_but_quarantines_interrupted_data(self):
        config = mail._configuration()[1]
        self.mail._enqueue(config.route(), True, START)
        first = self.mail._claim(config.route(), START)
        self.mail._set(first, "transmitting", weather.iso(START), expected="claimed")
        second = self.mail._claim(config.route(), START)
        self.assertEqual(second["kind"], "digest")
        state = self.tick(
            mail.CLAIM_SECONDS + 1, True, instance=mail.Notifications(self.store)
        )
        self.assertEqual(state["review_required"], 1)
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual([r["state"] for r in self.rows()], ["review", "accepted"])
        self.assertEqual(self.rows()[1]["attempts"], 2)

    def test_concurrent_ticks_claim_digest_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            states = list(pool.map(lambda _: self.tick(), range(8)))
        self.assertTrue(all(s["ready"] for s in states))
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_recipient_change_during_preflight_discards_the_envelope(self):
        def switch_route():
            os.environ["PMB_MAIL_TO"] = "next-owner@example.invalid"
            return (250, b"ok")

        self.smtp.scripts["rcpt"] = switch_route
        self.tick()
        self.assertFalse(self.smtp.messages)
        self.assertEqual(self.rows()[0]["state"], "cancelled")
        self.smtp.scripts.clear()
        self.tick(600)
        self.assertEqual(self.smtp.messages[0]["To"], "next-owner@example.invalid")
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
