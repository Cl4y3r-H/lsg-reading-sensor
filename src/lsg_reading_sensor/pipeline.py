"""Orquestación de una pasada del sensor de lectura.

    KOReader (statistics.sqlite3) ──► filtrar páginas ──► bloques continuos
        ──► puntos por día (acumulativo, tope 20) ──► SQLite ──► webhook + crédito

En cada pasada se recalculan los últimos `lookback_days` días completos desde
KOReader. Como el cálculo es acumulativo (total del día − ya acreditado), es
seguro repetirlo cuantas veces sea necesario.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import __version__
from .config import SensorConfig
from .crediting import Creditor, safe_credit
from .http import HttpError, NetworkError
from .lsg_client import LsgCoreClient
from .models import CreditRecord, CreditStatus, DayEvaluation
from .rules import build_blocks, evaluate_day, filter_events, group_blocks_by_day, make_client_ref
from .store import StateStore

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    pages_read: int = 0
    pages_discarded: int = 0
    days_evaluated: int = 0
    days_closed: int = 0
    points_enqueued: int = 0
    credits_synced: int = 0
    credits_pending: int = 0
    credits_rejected: int = 0
    webhooks_sent: int = 0
    webhooks_pending: int = 0


class ReadingPipeline:
    def __init__(self, cfg: SensorConfig, store: StateStore, source, creditor: Creditor,
                 core: LsgCoreClient | None, player_id: int | None):
        self.cfg = cfg
        self.store = store
        self.source = source
        self.creditor = creditor
        self.core = core
        self.player_id = player_id

    def run_once(self, now_ts: int | None = None) -> RunSummary:
        now_ts = int(now_ts if now_ts is not None else time.time())
        summary = RunSummary()
        self._evaluate_window(now_ts, summary)
        self._drain_webhooks(summary)
        self._drain_credits(summary)
        return summary

    # --------------------------------------------------------- Evaluación
    def _since_ts(self, now_ts: int) -> int:
        tz = self.cfg.timezone
        today = datetime.fromtimestamp(now_ts, tz).date()
        first_day = today - timedelta(days=self.cfg.source.lookback_days)
        if self.cfg.start_date:
            first_day = max(first_day, date.fromisoformat(self.cfg.start_date))
        return int(datetime.combine(first_day, datetime.min.time(), tz).timestamp())

    def _evaluate_window(self, now_ts: int, summary: RunSummary) -> None:
        tz = self.cfg.timezone
        today = datetime.fromtimestamp(now_ts, tz).date()
        raw = self.source.fetch_events(self._since_ts(now_ts))
        events, discarded = filter_events(raw, self.cfg.rules, now_ts)
        summary.pages_read = len(events)
        summary.pages_discarded = sum(v for k, v in discarded.items() if k != "PAGE_CLAMPED")

        for day, blocks in sorted(group_blocks_by_day(build_blocks(events, self.cfg.rules, tz)).items()):
            credited = self.store.credited_points_on(day)
            ev = evaluate_day(day, blocks, credited, self.cfg.rules, discarded)
            summary.days_evaluated += 1

            credit = webhook = None
            if ev.credited_points > 0:
                target = credited + ev.credited_points
                ref = make_client_ref(self.player_id, day, target)
                credit = CreditRecord(ref, day, ev.credited_points, datetime.now(timezone.utc))
                webhook = (ref, self._payload(ev, ref, "progress"))
                summary.points_enqueued += ev.credited_points

            close = None
            if day < today and not self.store.is_closed(day):
                close = day
                summary.days_closed += 1
                if webhook is None:   # un día cerrado sin crédito nuevo igual deja su resumen
                    ref = f"close:{self.player_id}:{day.isoformat()}"
                    webhook = (ref, self._payload(ev, ref, "day_closed"))

            log.info("Día %s: %.1f min activos en %d bloques, %d unidades, %d pts nuevos",
                     day, ev.active_minutes, len(ev.blocks), ev.eligible_units, ev.credited_points)
            if credit or close or webhook:
                if not self.cfg.lsg.send_webhook:
                    webhook = None
                self.store.record(credit, webhook, close)

    def _payload(self, ev: DayEvaluation, ref: str, kind: str) -> dict:
        return {
            "player_id": self.player_id,
            "sensor_endpoint_id": self.cfg.lsg.sensor_endpoint_id,
            "players_sensor_endpoint_id": self.cfg.lsg.players_sensor_endpoint_id,
            "parsed_value": ev.active_minutes,
            "status": "OK" if ev.eligible_units > 0 else "IGNORED",
            "occurred_at": max(b.end for b in ev.blocks).astimezone(timezone.utc).isoformat(),
            "raw_payload": {
                "ref": ref,
                "kind": kind,                        # progress | day_closed
                "sensor": "lsg-reading",
                "sensor_version": __version__,
                "source": "koreader",
                "local_day": ev.local_day.isoformat(),
                "active_minutes": ev.active_minutes,
                "eligible_units": ev.eligible_units,
                "points_to_credit": ev.credited_points,
                "discarded_pages": ev.discarded_events,
                # Sin títulos: solo tiempos, páginas y cantidad de libros distintos.
                "blocks": [
                    {"start": b.start.isoformat(), "end": b.end.isoformat(),
                     "active_minutes": b.active_minutes, "pages": b.pages, "books": b.books}
                    for b in ev.blocks
                ],
            },
        }

    # ------------------------------------------------------------- Envíos
    def _drain_webhooks(self, summary: RunSummary) -> None:
        if self.core is None or not self.cfg.lsg.send_webhook:
            return
        for ref, payload in self.store.pending_webhooks():
            try:
                self.core.ingest_webhook(payload)
                self.store.mark_webhook(ref, sent=True)
                summary.webhooks_sent += 1
            except (NetworkError, HttpError) as exc:
                self.store.mark_webhook(ref, sent=False, error=str(exc))
                summary.webhooks_pending += 1

    def _drain_credits(self, summary: RunSummary) -> None:
        for record in self.store.pending_credits():
            outcome = safe_credit(self.creditor, record)
            if outcome.status == CreditStatus.PENDING:
                self.store.bump_credit_attempt(record.client_ref, outcome.error or "pendiente")
                summary.credits_pending += 1
                continue
            self.store.update_credit(record.client_ref, outcome.status, outcome.remote_ref, outcome.error)
            if outcome.status in (CreditStatus.SYNCED, CreditStatus.DUPLICATE):
                summary.credits_synced += 1
            else:
                summary.credits_rejected += 1
                log.error("Crédito %s RECHAZADO: %s", record.client_ref, outcome.error)
