"""Modelos de dominio del sensor de lectura."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


@dataclass(frozen=True)
class PageEvent:
    """Una página leída según KOReader (tabla page_stat_data).

    `book_key` es un hash: el sensor nunca guarda ni envía títulos ni autores.
    """

    book_key: str
    page: int
    start_ts: int          # epoch en segundos (UTC)
    duration_s: int

    @property
    def end_ts(self) -> int:
        return self.start_ts + self.duration_s

    @property
    def event_key(self) -> str:
        return f"{self.book_key}:{self.page}:{self.start_ts}"


@dataclass(frozen=True)
class ReadingBlock:
    """Tramo de lectura continua (pausas menores a max_gap toleradas)."""

    start: datetime
    end: datetime
    active_seconds: int    # suma de duraciones de página (no incluye pausas)
    pages: int
    books: int

    @property
    def active_minutes(self) -> float:
        return round(self.active_seconds / 60, 1)


@dataclass
class DayEvaluation:
    local_day: date
    blocks: list[ReadingBlock]
    eligible_units: int        # bloques de 30 min completos en el día
    raw_points: int
    credited_points: int       # a acreditar en esta pasada (tras tope y lo ya acreditado)
    discarded_events: dict[str, int] = field(default_factory=dict)

    @property
    def active_minutes(self) -> float:
        return round(sum(b.active_seconds for b in self.blocks) / 60, 1)


class CreditStatus(str, Enum):
    PENDING = "PENDING"
    SYNCED = "SYNCED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


@dataclass
class CreditRecord:
    client_ref: str
    local_day: date
    amount: int
    created_at: datetime
    status: CreditStatus = CreditStatus.PENDING
    attempts: int = 0
    remote_ref: str | None = None
    last_error: str | None = None
