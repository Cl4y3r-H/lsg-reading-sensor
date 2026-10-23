"""Estado local persistente (SQLite) del sensor de lectura.

A diferencia del sensor de fuerza, aquí NO se guardan las páginas leídas:
KOReader conserva el historial completo, así que en cada pasada se recalculan
los días dentro de la ventana `lookback_days`. Solo se persiste:
  * credits         → cola de créditos (idempotencia y tope diario)
  * webhook_outbox  → cola de envíos de trazabilidad
  * closed_days     → días ya cerrados (resumen final enviado)
Esto también minimiza los datos personales almacenados en el equipo.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .models import CreditRecord, CreditStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credits (
    client_ref TEXT PRIMARY KEY,
    local_day TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    remote_ref TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_outbox (
    ref TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS closed_days (local_day TEXT PRIMARY KEY, closed_at TEXT NOT NULL);
"""

_COUNTED = (CreditStatus.PENDING.value, CreditStatus.SYNCED.value, CreditStatus.DUPLICATE.value)


class StateStore:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- Lecturas
    def credited_points_on(self, day: date) -> int:
        q = f"SELECT COALESCE(SUM(amount),0) FROM credits WHERE local_day=? AND status IN ({','.join('?' * len(_COUNTED))})"
        return int(self.conn.execute(q, (day.isoformat(), *_COUNTED)).fetchone()[0])

    def is_closed(self, day: date) -> bool:
        return self.conn.execute("SELECT 1 FROM closed_days WHERE local_day=?", (day.isoformat(),)).fetchone() is not None

    # ------------------------------------------------------------ Escrituras
    def record(self, credit: CreditRecord | None, webhook: tuple[str, dict] | None, close_day: date | None = None) -> None:
        """Crédito + webhook + cierre de día en una sola transacción."""
        with self.transaction() as c:
            if credit is not None:
                c.execute(
                    "INSERT OR IGNORE INTO credits VALUES (?,?,?,?,?,?,?,?)",
                    (credit.client_ref, credit.local_day.isoformat(), credit.amount, credit.status.value,
                     credit.attempts, credit.remote_ref, credit.last_error, credit.created_at.isoformat()),
                )
            if webhook is not None:
                ref, payload = webhook
                c.execute("INSERT OR IGNORE INTO webhook_outbox(ref, payload) VALUES (?,?)",
                          (ref, json.dumps(payload, default=str)))
            if close_day is not None:
                c.execute("INSERT OR IGNORE INTO closed_days VALUES (?,?)",
                          (close_day.isoformat(), datetime.now().isoformat()))

    # ------------------------------------------------------- Cola créditos
    def pending_credits(self) -> list[CreditRecord]:
        rows = self.conn.execute("SELECT * FROM credits WHERE status=? ORDER BY created_at",
                                 (CreditStatus.PENDING.value,)).fetchall()
        return [CreditRecord(
            client_ref=r["client_ref"], local_day=date.fromisoformat(r["local_day"]), amount=r["amount"],
            created_at=datetime.fromisoformat(r["created_at"]), status=CreditStatus(r["status"]),
            attempts=r["attempts"], remote_ref=r["remote_ref"], last_error=r["last_error"],
        ) for r in rows]

    def update_credit(self, client_ref: str, status: CreditStatus, remote_ref: str | None, error: str | None) -> None:
        with self.transaction() as c:
            c.execute("UPDATE credits SET status=?, remote_ref=COALESCE(?, remote_ref), last_error=?, "
                      "attempts=attempts+1 WHERE client_ref=?", (status.value, remote_ref, error, client_ref))

    def bump_credit_attempt(self, client_ref: str, error: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE credits SET attempts=attempts+1, last_error=? WHERE client_ref=?", (error, client_ref))

    # -------------------------------------------------------- Cola webhook
    def pending_webhooks(self) -> list[tuple[str, dict]]:
        rows = self.conn.execute("SELECT ref, payload FROM webhook_outbox WHERE sent=0").fetchall()
        return [(r["ref"], json.loads(r["payload"])) for r in rows]

    def mark_webhook(self, ref: str, sent: bool, error: str | None = None) -> None:
        with self.transaction() as c:
            c.execute("UPDATE webhook_outbox SET sent=?, attempts=attempts+1, last_error=? WHERE ref=?",
                      (int(sent), error, ref))
