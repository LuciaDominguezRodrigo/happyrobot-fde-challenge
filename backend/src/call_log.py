"""Local, ops-facing mirror of call activity for the dashboard.

This is NOT the system of record -- HappyRobot Twin is (see api.py). This
module exists only because the ops dashboard needs something this backend
can read without a Twin REST API, and because it can capture per-step detail
(each negotiation round, etc.) that the post-call Twin extraction summarizes
away. Backed by SQLite so it survives a backend restart during a demo.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "call_log.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    mc_number TEXT,
    carrier_legal_name TEXT,
    fmcsa_status TEXT,
    otp_destination TEXT,
    otp_verified INTEGER,
    otp_attempts INTEGER DEFAULT 0,
    load_id TEXT,
    loadboard_rate INTEGER,
    negotiation_rounds INTEGER DEFAULT 0,
    last_offer INTEGER,
    last_action TEXT,
    agreed_rate INTEGER,
    outcome TEXT,
    booking_ref TEXT,
    flagged INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
"""


class CallLogStore:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(calls)")}
        if "flagged" not in cols:
            self._conn.execute("ALTER TABLE calls ADD COLUMN flagged INTEGER DEFAULT 0")

    def _ensure_row(self, call_id: str) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT INTO calls (call_id, created_at, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(call_id) DO NOTHING",
            (call_id, now, now),
        )

    def upsert(self, call_id: Optional[str], **fields: Any) -> None:
        if not call_id:
            return
        self._ensure_row(call_id)
        if fields:
            set_clause = ", ".join(f"{key} = ?" for key in fields)
            self._conn.execute(
                f"UPDATE calls SET {set_clause}, updated_at = ? WHERE call_id = ?",
                (*fields.values(), time.time(), call_id),
            )
        self._conn.commit()

    def increment(self, call_id: Optional[str], field: str) -> None:
        if not call_id:
            return
        self._ensure_row(call_id)
        self._conn.execute(
            f"UPDATE calls SET {field} = COALESCE({field}, 0) + 1, updated_at = ? WHERE call_id = ?",
            (time.time(), call_id),
        )
        self._conn.commit()

    def list_calls(self, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM calls ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def summary(self) -> dict:
        calls = self.list_calls(limit=10000)
        total = len(calls)
        booked = sum(1 for c in calls if c["outcome"] == "booked")
        otp_attempted = sum(1 for c in calls if c["otp_destination"] is not None)
        otp_verified = sum(1 for c in calls if c["otp_verified"])
        mc_active = sum(1 for c in calls if c["fmcsa_status"] == "active")
        negotiation_failed = sum(1 for c in calls if c["outcome"] == "negotiation_failed")
        flagged = sum(1 for c in calls if c["flagged"])
        rounds = [c["negotiation_rounds"] or 0 for c in calls if c["negotiation_rounds"]]
        return {
            "total_calls": total,
            "booked": booked,
            "booking_rate": round(booked / total, 3) if total else None,
            "negotiation_failed": negotiation_failed,
            "mc_verification_active_rate": round(mc_active / total, 3) if total else None,
            "otp_verification_rate": round(otp_verified / otp_attempted, 3) if otp_attempted else None,
            "avg_negotiation_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
            "flagged_for_review": flagged,
        }
