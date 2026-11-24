"""Transactional, non-replaying call ledger adapted from MCP Gateway."""

from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .contracts import fingerprint


class JournalConflict(ValueError):
    pass


class Journal:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self._read_only = read_only
        if self._read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS edgelab_events(
                    seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, event_key TEXT NOT NULL,
                    body TEXT NOT NULL, body_hash TEXT NOT NULL, UNIQUE(run_id, event_key)
                )
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        database = self.path.resolve().as_uri() + "?mode=ro" if self._read_only else self.path
        db = sqlite3.connect(database, uri=self._read_only, timeout=2, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def get(self, run_id: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT body, body_hash FROM edgelab_events WHERE run_id=? AND event_key=?",
                             (run_id, key)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row: tuple[str, str]) -> dict:
        value = json.loads(row[0])
        if not isinstance(value, dict) or fingerprint(value) != row[1]:
            raise JournalConflict("Stored record fingerprint mismatch")
        return value

    def append(self, run_id: str, key: str, body: dict) -> bool:
        """Claim an event once; a false return means the identical record already exists."""
        if self._read_only:
            raise JournalConflict("Cannot append through a read-only journal")
        digest = fingerprint(body)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                inserted = db.execute(
                    "INSERT INTO edgelab_events(run_id,event_key,body,body_hash) VALUES(?,?,?,?) "
                    "ON CONFLICT(run_id,event_key) DO NOTHING",
                    (run_id, key, json.dumps(body, sort_keys=True, allow_nan=False), digest),
                ).rowcount == 1
                if not inserted:
                    old = db.execute("SELECT body, body_hash FROM edgelab_events WHERE run_id=? AND event_key=?",
                                     (run_id, key)).fetchone()
                    if fingerprint(self._decode(old)) != digest:
                        raise JournalConflict("The event is already bound to different inputs")
                db.commit()
                return inserted
            except BaseException:
                db.rollback()
                raise

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT event_key, body, body_hash FROM edgelab_events WHERE run_id=? ORDER BY seq",
                              (run_id,)).fetchall()
        return [{"key": key, "value": self._decode((body, digest)), "sha256": digest}
                for key, body, digest in rows]
