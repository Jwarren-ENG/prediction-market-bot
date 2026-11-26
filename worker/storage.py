"""Private worker state, instance ownership, and crash-safe user settings."""

import contextlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from . import monitoring


def utc():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def data_home():
    override = os.environ.get("PMB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Prediction Market Bot"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Prediction Market Bot"
        )
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "prediction-market-bot"
    )


def atomic_text(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        temp = Path(output.name)
        try:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def exclusive_worker(directory):
    file = (directory / "worker.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if file.seek(0, os.SEEK_END) == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        file.close()
        raise RuntimeError("A worker already owns this data directory.") from exc
    try:
        yield
    finally:
        file.close()


def access_key(directory):
    # Only the instance lock holder creates the key.
    path = directory / "access.key"
    if not path.exists():
        atomic_text(path, secrets.token_urlsafe(40))
    key = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{54}", key):
        raise RuntimeError(
            "Worker access.key is damaged. Stop the worker and move that file aside to regenerate the private connection link."
        )
    return key


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "worker.sqlite"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_notes(
                    fingerprint TEXT NOT NULL, market_id TEXT NOT NULL, provider TEXT NOT NULL,
                    status TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                    sources TEXT NOT NULL DEFAULT '[]', reserve_cents INTEGER NOT NULL DEFAULT 0,
                    workspace TEXT NOT NULL, PRIMARY KEY(fingerprint,market_id,provider,workspace));
                CREATE TABLE IF NOT EXISTS forward_scans(
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS scan_receipts_time ON forward_scans(finished_at);
            """)
            db.executescript(monitoring.PAIR_SCHEMA)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(research_notes)")
            }
            if "completed_at" not in columns:
                db.execute("ALTER TABLE research_notes ADD COLUMN completed_at TEXT")
            db.execute("""CREATE INDEX IF NOT EXISTS research_completion_time
                       ON research_notes(workspace,provider,status,coalesce(completed_at,at) DESC)""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS research_record_completion
                       AFTER UPDATE OF status ON research_notes
                       WHEN NEW.status='complete' AND OLD.status!='complete'
                       BEGIN
                         UPDATE research_notes SET completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                         WHERE rowid=NEW.rowid;
                       END""")

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, name, default=None):
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE name=?", (name,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, name, value):
        self.put_many({name: value})

    def put_many(self, values):
        with self.connection() as db:
            db.executemany(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                [(name, json.dumps(value)) for name, value in values.items()],
            )

    def restore_scan_history(self, at):
        """A new exclusive worker cannot resume an interrupted network capture."""
        with self.connection() as db:
            db.execute(
                "UPDATE forward_scans SET status='failed',finished_at=?,detail=? WHERE status='running'",
                (at, "The worker restarted before this scan recorded a completion."),
            )
            if db.execute("SELECT 1 FROM forward_scans LIMIT 1").fetchone():
                return
            row = db.execute(
                "SELECT value FROM settings WHERE name='forward_health'"
            ).fetchone()
            if not row:
                return
            old = json.loads(row["value"])
            stamp, detail = old.get("at"), old.get("status", "")
            if not isinstance(stamp, str) or not isinstance(detail, str):
                return
            try:
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None:
                    return
            except ValueError:
                return
            if detail.startswith("Source scan needs attention"):
                status = "failed"
            elif (
                detail.startswith("Feed degraded.")
                or "Settlement review required." in detail
                or "Exceptional venue payouts remain pending" in detail
            ):
                status = "partial"
            elif (
                detail.startswith("Collected ")
                or detail == "NOAA collection active. Kalshi market access is off."
            ):
                status = "complete"
            else:
                return
            db.execute(
                "INSERT INTO forward_scans VALUES(?,?,NULL,?,?)",
                ("scan-legacy", status, stamp, detail[:1500]),
            )

    def begin_scan(self, at):
        scan_id = "scan-" + secrets.token_hex(12)
        with self.connection() as db:
            db.execute(
                "INSERT INTO forward_scans VALUES(?,'running',?,NULL,?)",
                (scan_id, at, "Collecting forward sources."),
            )
            cutoff = datetime.fromisoformat(at.replace("Z", "+00:00")) - timedelta(days=8)
            db.execute("DELETE FROM forward_scans WHERE finished_at < ?",
                       (cutoff.isoformat(timespec="microseconds").replace("+00:00", "Z"),))
        return scan_id

    def finish_scan(self, scan_id, status, at, detail, evidence):
        with self.connection() as db:
            write = db.execute(
                """UPDATE forward_scans SET status=?,finished_at=?,detail=?
                               WHERE id=? AND status='running'""",
                (status, at, detail, scan_id),
            )
            if write.rowcount:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES('forward_health',?)",
                    (json.dumps({"at": at, "status": detail, "evidence": evidence}),),
                )
            return bool(write.rowcount)

    def scan_history(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM forward_scans ORDER BY rowid DESC LIMIT 100"
                )
            ]

    def local_research_activity(self):
        with self.connection() as db:
            notes = db.execute("""SELECT fingerprint,market_id,at,completed_at,text FROM research_notes
                               WHERE workspace='forward' AND provider='Local research' AND status='complete'
                               ORDER BY coalesce(completed_at,at) DESC,rowid DESC LIMIT 100""")
            result = []
            for note in notes:
                mid = note["market_id"]
                message = f"Local research completed for {mid}."
                if not note["completed_at"]:
                    message += " The recorded time is the request time."
                excerpt = " ".join(note["text"].split())
                if excerpt:
                    message += " " + (
                        excerpt[:297] + "…" if len(excerpt) > 300 else excerpt
                    )
                result.append(
                    {
                        "id": f"research-{mid}-{note['fingerprint']}",
                        "kind": "research",
                        "at": note["completed_at"] or note["at"],
                        "market_id": mid,
                        "status": "complete",
                        "message": message,
                    }
                )
            return result
