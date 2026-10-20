"""SQLite storage. Matches are immutable and every consumed id is a PRIMARY KEY, so the
database itself refuses to let one bank line or one invoice be reconciled twice."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from recon.models import Escalation, Match

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY, level TEXT NOT NULL, left_ids TEXT NOT NULL,
  right_ids TEXT NOT NULL, source TEXT NOT NULL, pattern TEXT, proof TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consumed (
  item_id TEXT PRIMARY KEY, match_id TEXT NOT NULL REFERENCES matches(match_id));
CREATE TABLE IF NOT EXISTS escalations (
  exception_id TEXT PRIMARY KEY, level TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traces (
  exception_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS matches_immutable BEFORE UPDATE ON matches
  BEGIN SELECT RAISE(ABORT, 'matches are immutable'); END;
"""


class AlreadyReconciled(Exception):
    pass


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self._n = self.db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    def consumed_by(self, item_id: str) -> str | None:
        row = self.db.execute("SELECT match_id FROM consumed WHERE item_id=?", (item_id,)).fetchone()
        return row[0] if row else None

    def is_consumed(self, item_id: str) -> bool:
        return self.consumed_by(item_id) is not None

    def commit(self, match: Match) -> None:
        ids = list(match.left_ids) + list(match.right_ids)
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO matches VALUES (?,?,?,?,?,?,?)",
                    (match.match_id, match.level, json.dumps(match.left_ids),
                     json.dumps(match.right_ids), match.source, match.pattern,
                     match.proof.model_dump_json()))
                self.db.executemany("INSERT INTO consumed VALUES (?,?)",
                                    [(i, match.match_id) for i in ids])
        except sqlite3.IntegrityError as exc:
            raise AlreadyReconciled(f"{match.match_id}: {exc}") from exc

    def next_match_id(self) -> str:
        self._n += 1
        return f"M{self._n:05d}"

    def escalate(self, esc: Escalation) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO escalations VALUES (?,?,?)",
                            (esc.exception_id, esc.level, esc.model_dump_json()))

    def save_trace(self, exception_id: str, trace: list[dict]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO traces VALUES (?,?)",
                            (exception_id, json.dumps(trace, default=str)))

    def matches(self) -> list[Match]:
        out = []
        for mid, level, l, r, src, pat, proof in self.db.execute(
                "SELECT * FROM matches ORDER BY match_id"):
            out.append(Match(match_id=mid, level=level, left_ids=tuple(json.loads(l)),
                             right_ids=tuple(json.loads(r)), source=src, pattern=pat,
                             proof=json.loads(proof)))
        return out

    def escalations(self) -> list[Escalation]:
        return [Escalation.model_validate_json(b) for (b,) in
                self.db.execute("SELECT body FROM escalations ORDER BY exception_id")]

    def trace(self, exception_id: str) -> list[dict]:
        row = self.db.execute("SELECT body FROM traces WHERE exception_id=?",
                              (exception_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def traces(self) -> dict[str, list[dict]]:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT * FROM traces")}
