"""Single-owner operational email. No message contains account or venue data."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path

from . import weather

MAX_ATTEMPTS = 3
RETRY_SECONDS = (60, 300)
CLAIM_SECONDS = 180
MAX_PER_TICK = 2
LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
LOCAL_PART = re.compile(r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}")


def _host(value):
    if not value or len(value) > 253:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(LABEL.fullmatch(p) for p in value.split("."))


def _mailbox(value):
    parts = value.split("@")
    return (
        len(parts) == 2
        and len(value) <= 254
        and LOCAL_PART.fullmatch(parts[0])
        and not parts[0].startswith(".")
        and not parts[0].endswith(".")
        and ".." not in parts[0]
        and _host(parts[1])
        and ":" not in parts[1]
    )


@dataclass(frozen=True, repr=False)
class Config:
    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str
    recipient: str

    def route(self):
        # Persist a fingerprint only. A changed recipient cannot inherit a pending
        # envelope from an earlier owner configuration.
        routing = (
            self.host.lower(),
            str(self.port),
            self.security,
            self.user,
            self.sender,
            self.recipient,
        )
        return hashlib.sha256("\x00".join(routing).encode()).hexdigest()


def _configuration():
    if os.environ.get("PMB_MAIL_ENABLED") != "1":
        return False, None, "Email is off."
    host = os.environ.get("PMB_SMTP_HOST", "")
    security = os.environ.get("PMB_SMTP_SECURITY", "ssl").lower()
    port = os.environ.get("PMB_SMTP_PORT", "465" if security == "ssl" else "587")
    sender = os.environ.get("PMB_MAIL_FROM", "")
    owner = os.environ.get("PMB_MAIL_TO", "")
    user = os.environ.get("PMB_SMTP_USER", "")
    password = os.environ.get("PMB_SMTP_PASSWORD", "")
    issues = []
    if not _host(host):
        issues.append("PMB_SMTP_HOST")
    if security not in ("ssl", "starttls"):
        issues.append("PMB_SMTP_SECURITY")
    if (
        not port.isascii()
        or not port.isdigit()
        or len(port) > 5
        or not 1 <= int(port) <= 65535
    ):
        issues.append("PMB_SMTP_PORT")
    if not user or any(ord(c) < 32 for c in user):
        issues.append("PMB_SMTP_USER")
    if not password:
        issues.append("PMB_SMTP_PASSWORD")
    if not _mailbox(sender):
        issues.append("PMB_MAIL_FROM")
    if not _mailbox(owner):
        issues.append("PMB_MAIL_TO")
    if issues:
        return True, None, "Email setup needs " + ", ".join(issues) + "."
    return (
        True,
        Config(host, int(port), security, user, password, sender, owner),
        "Email is ready.",
    )


def _local_time(stamp):
    local = weather.eastern(weather.instant(stamp))
    offset = local.strftime("%z")
    return (
        local.strftime("%Y-%m-%d at %H:%M")
        + f" New York time (UTC{offset[:3]}:{offset[3:]})."
    )


def _contents(row):
    if row["kind"] == "degraded":
        subject = "PMB worker needs attention"
        body = "A forward worker check reported a problem on " + _local_time(
            row["created"]
        )
    elif row["kind"] == "recovered":
        subject = "PMB worker checks recovered"
        body = "A forward worker check succeeded on " + _local_time(row["created"])
    else:
        subject = "PMB paper research review · " + row["local_day"]
        health = (
            "The latest worker check needs attention."
            if row["degraded"]
            else "The latest worker check succeeded."
        )
        body = (
            "Your daily paper research review is ready. "
            + health
            + "\n\nThe worker completed the check on "
            + _local_time(row["created"])
        )
    if row["last_healthy"] and row["degraded"]:
        body += "\n\nThe last successful worker check ran on " + _local_time(
            row["last_healthy"]
        )
    return {
        "subject": subject,
        "body": body
        + "\n\nOpen Prediction Market Bot to review the private worker and research inbox.",
    }


def _message_id(route, event):
    digest = hashlib.sha256((route + event).encode()).hexdigest()
    return f"<{digest}@pmb.invalid>"


def _message(row, config):
    content = _contents(row)
    message = EmailMessage(policy=SMTP)
    message["From"] = config.sender
    message["To"] = config.recipient
    message["Date"] = format_datetime(weather.instant(row["created"]))
    message["Message-ID"] = _message_id(row["route"], row["event"])
    message["Subject"] = content["subject"]
    message.set_content(content["body"])
    return message.as_bytes()


def _context():
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if (
        sys.platform == "darwin"
        and not ctx.get_ca_certs()
        and Path("/etc/ssl/cert.pem").is_file()
    ):
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


class Notifications:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS mail_progress(
                    route TEXT PRIMARY KEY, checked TEXT NOT NULL, degraded INTEGER NOT NULL,
                    incident INTEGER NOT NULL, last_healthy TEXT);
                CREATE TABLE IF NOT EXISTS mail_outbox(
                    route TEXT NOT NULL, event TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL,
                    local_day TEXT NOT NULL, degraded INTEGER NOT NULL, last_healthy TEXT,
                    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    due TEXT NOT NULL, updated TEXT NOT NULL, token TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(route,event));
                CREATE INDEX IF NOT EXISTS mail_due ON mail_outbox(route,state,due);
            """
            )

    def preview(self):
        """Render a generic example locally without connecting to a mail provider."""
        return {
            "subject": "PMB paper research review · [New York date]",
            "body": "Your daily paper research review is ready. The email includes the worker's operational status "
            "and scan time. Open Prediction Market Bot to review the private worker and research inbox.",
        }

    def state(self):
        enabled, config, status = _configuration()
        state = {
            "enabled": enabled,
            "ready": bool(config),
            "status": status,
            "last_delivery": None,
            "last_attempt": None,
            "pending": 0,
            "review_required": 0,
            "review_items": [],
        }
        with self.store.connection() as db:
            # Surface uncertain delivery across route changes without disclosing destinations.
            state["review_required"] = db.execute(
                "SELECT count(*) FROM mail_outbox WHERE state='review'"
            ).fetchone()[0]
            for r in db.execute(
                """SELECT route,event,kind,created,updated,attempts FROM mail_outbox
                WHERE state='review' ORDER BY updated DESC LIMIT 10"""
            ):
                state["review_items"].append(
                    {
                        "kind": r["kind"],
                        "created_at": r["created"],
                        "at": r["updated"],
                        "attempts": r["attempts"],
                        "message_id": _message_id(r["route"], r["event"]),
                    }
                )
            if not config:
                return state
            route = config.route()
            state["pending"] = db.execute(
                """SELECT count(*) FROM mail_outbox WHERE route=?
                AND state IN ('queued','claimed','transmitting')""",
                (route,),
            ).fetchone()[0]
            fields = "kind,state,attempts,updated AS at,detail"
            last = db.execute(
                f"""SELECT {fields} FROM mail_outbox WHERE route=? AND attempts>0
                ORDER BY updated DESC,rowid DESC LIMIT 1""",
                (route,),
            ).fetchone()
            delivered = db.execute(
                f"""SELECT {fields} FROM mail_outbox WHERE route=? AND state='accepted'
                ORDER BY updated DESC,rowid DESC LIMIT 1""",
                (route,),
            ).fetchone()
            state["last_attempt"] = dict(last) if last else None
            state["last_delivery"] = dict(delivered) if delivered else None
        return state

    def _enqueue(self, route, degraded, now):
        stamp = weather.iso(now)
        day = str(weather.eastern(now).date())
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM mail_progress WHERE route=?", (route,)
            ).fetchone()
            if prior and stamp <= prior["checked"]:
                return
            incident = prior["incident"] if prior else 0
            last_healthy = (
                (prior["last_healthy"] if prior else None) if degraded else stamp
            )
            events = []
            if degraded and (not prior or not prior["degraded"]):
                incident += 1
                events.append((f"incident/{incident}/down", "degraded"))
            elif not degraded and prior and prior["degraded"]:
                events.append((f"incident/{incident}/up", "recovered"))
            events.append((f"daily/{day}", "digest"))
            db.execute(
                """UPDATE mail_outbox SET state='superseded',updated=?,detail='A newer daily review is available.'
                WHERE route=? AND kind='digest' AND local_day<? AND state IN ('queued','claimed')""",
                (stamp, route, day),
            )
            db.execute(
                """INSERT INTO mail_progress VALUES(?,?,?,?,?) ON CONFLICT(route) DO UPDATE SET
                checked=excluded.checked,degraded=excluded.degraded,incident=excluded.incident,last_healthy=excluded.last_healthy""",
                (route, stamp, int(degraded), incident, last_healthy),
            )
            for key, kind in events:
                db.execute(
                    """INSERT OR IGNORE INTO mail_outbox
                    (route,event,kind,created,local_day,degraded,last_healthy,due,updated) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        route,
                        key,
                        kind,
                        stamp,
                        day,
                        int(degraded),
                        last_healthy,
                        stamp,
                        stamp,
                    ),
                )

    def _claim(self, route, now):
        stamp = weather.iso(now)
        cutoff = weather.iso(now - timedelta(seconds=CLAIM_SECONDS))
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # An interrupted DATA command can have reached the provider. Pre-DATA
            # work is retryable; the token prevents a reclaimed attempt from sending.
            db.execute(
                """UPDATE mail_outbox SET state='review',updated=?,detail='SMTP acceptance is unknown after interruption.'
                WHERE state='transmitting' AND updated<=?""",
                (stamp, cutoff),
            )
            db.execute(
                """UPDATE mail_outbox SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                updated=?,token='',detail='Interrupted before submitting mail.' WHERE state='claimed' AND updated<=?""",
                (MAX_ATTEMPTS, stamp, cutoff),
            )
            row = db.execute(
                """SELECT * FROM mail_outbox WHERE route=? AND state='queued' AND due<=?
                AND attempts<? ORDER BY created,CASE kind WHEN 'digest' THEN 1 ELSE 0 END,rowid LIMIT 1""",
                (route, stamp, MAX_ATTEMPTS),
            ).fetchone()
            if not row:
                return None
            claim = dict(row)
            claim["token"] = secrets.token_hex(16)
            claim["attempts"] += 1
            db.execute(
                """UPDATE mail_outbox SET state='claimed',token=?,attempts=?,updated=?,detail=''
                WHERE route=? AND event=?""",
                (claim["token"], claim["attempts"], stamp, route, claim["event"]),
            )
            return claim

    def _set(self, claim, state, stamp, detail="", due=None, expected=None):
        with self.store.connection() as db:
            sql = """UPDATE mail_outbox SET state=?,updated=?,due=?,detail=?
                WHERE route=? AND event=? AND token=?"""
            params = [
                state,
                stamp,
                due or stamp,
                detail,
                claim["route"],
                claim["event"],
                claim["token"],
            ]
            if expected:
                sql += " AND state=?"
                params.append(expected)
            return db.execute(sql, params).rowcount == 1

    def _preflight_failure(self, claim, exc, now):
        code = getattr(exc, "smtp_code", None)
        retryable = (
            400 <= code < 500
            if isinstance(code, int)
            else isinstance(exc, (OSError, smtplib.SMTPServerDisconnected))
            and not isinstance(exc, (ssl.SSLError, smtplib.SMTPNotSupportedError))
        )
        # Only expose the SMTP response class/code. Server messages may echo a
        # credential or an address and are never copied to state, logs or email.
        note = "SMTP failed before message submission" + (
            f" (code {code})." if isinstance(code, int) else "."
        )
        stamp = weather.iso(now)
        if retryable and claim["attempts"] < MAX_ATTEMPTS:
            due = weather.iso(
                now + timedelta(seconds=RETRY_SECONDS[claim["attempts"] - 1])
            )
            self._set(claim, "queued", stamp, note, due=due, expected="claimed")
        else:
            self._set(claim, "failed", stamp, note, expected="claimed")

    def _deliver(self, claim, config, now):
        started = time.monotonic()

        def current():
            return now + timedelta(seconds=time.monotonic() - started)

        def enabled_for_route():
            _, latest, _ = _configuration()
            return bool(latest) and latest.route() == claim["route"]

        if not enabled_for_route():
            self._set(
                claim,
                "cancelled",
                weather.iso(current()),
                "Owner email configuration changed.",
                expected="claimed",
            )
            return
        client = None
        sending = False
        try:
            wire = _message(claim, config)
            tls = _context()
            options = dict(timeout=8, local_hostname="pmb.local")
            if config.security == "ssl":
                client = smtplib.SMTP_SSL(
                    config.host, config.port, context=tls, **options
                )
            else:
                client = smtplib.SMTP(config.host, config.port, **options)
                _expect(client.ehlo(), {250})
                client.starttls(context=tls)
            _expect(client.ehlo(), {250})
            client.login(config.user, config.password)
            _expect(client.mail(config.sender), {250})
            _expect(client.rcpt(config.recipient), {250, 251})
            if not enabled_for_route():
                self._set(
                    claim,
                    "cancelled",
                    weather.iso(current()),
                    "Owner email configuration changed.",
                    expected="claimed",
                )
                return
            if not self._set(
                claim, "transmitting", weather.iso(current()), expected="claimed"
            ):
                return
            sending = True
            try:
                code, _ = client.data(wire)
            except smtplib.SMTPDataError as exc:
                code = exc.smtp_code
            stamp = weather.iso(current())
            if code == 250:
                self._set(
                    claim,
                    "accepted",
                    stamp,
                    "SMTP server accepted the message.",
                    expected="transmitting",
                )
            elif isinstance(code, int) and 400 <= code < 600:
                self._set(
                    claim,
                    "failed",
                    stamp,
                    f"SMTP explicitly declined the message (code {code}).",
                    expected="transmitting",
                )
            else:
                self._set(
                    claim,
                    "review",
                    stamp,
                    "SMTP returned an unrecognized delivery response.",
                    expected="transmitting",
                )
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            if sending:
                self._set(
                    claim,
                    "review",
                    weather.iso(current()),
                    "SMTP acceptance is unknown. Check the mail provider's delivery records.",
                    expected="transmitting",
                )
            else:
                self._preflight_failure(claim, exc, current())
        finally:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass

    def tick(self, health_status: str, degraded: bool, now: str):
        """Record one scan and process at most two due mail items outside trading transactions.

        Free-form health_status stays inside the desk. Only the explicit degraded
        flag and trusted worker timestamps can enter the operational message.
        """
        _, config, _ = _configuration()
        if config is None:
            return self.state()
        if type(degraded) is not bool:
            raise ValueError("A boolean worker-health flag is required.")
        instant = weather.instant(now)
        route = config.route()
        self._enqueue(route, degraded, instant)
        started = time.monotonic()
        for _ in range(MAX_PER_TICK):
            attempt_at = instant + timedelta(seconds=time.monotonic() - started)
            claim = self._claim(route, attempt_at)
            if not claim:
                break
            self._deliver(claim, config, attempt_at)
        return self.state()


def _expect(response, acceptable):
    code, _ = response
    if code not in acceptable:
        raise smtplib.SMTPResponseException(code, b"SMTP preflight rejected")
