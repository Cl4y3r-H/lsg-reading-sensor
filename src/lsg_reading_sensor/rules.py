"""Reglas del ocio lector: filtrado de páginas, bloques continuos y puntos.

Funciones puras (sin red ni base de datos).

Criterio de conteo elegido: BLOQUES CONTINUOS CON PAUSAS CORTAS TOLERADAS.
  * Las páginas (de uno o varios libros) se ordenan por hora.
  * Si entre el fin de una página y el inicio de la siguiente pasan más de
    `max_gap_minutes`, empieza un bloque nuevo.
  * Cada bloque aporta floor(minutos_activos / 30) unidades; cada unidad = 5 pts.
  * Los minutos activos son la UNIÓN de los intervalos de página: si dos
    dispositivos registran lo mismo a la vez, no se cuenta dos veces.
  * Un bloque pertenece al día local en que empezó.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import RulesConfig
from .models import DayEvaluation, PageEvent, ReadingBlock

_NAMESPACE = uuid.UUID("0b8e3d2a-5f4c-4e7a-9d61-2c7f1a9e8b33")

DISCARD_TOO_SHORT = "PAGE_TOO_SHORT"
DISCARD_FUTURE = "FUTURE_TIMESTAMP"
CLAMPED_LONG = "PAGE_CLAMPED"


def filter_events(
    events: list[PageEvent], rules: RulesConfig, now_ts: int
) -> tuple[list[PageEvent], dict[str, int]]:
    """Descarta páginas implausibles y recorta las demasiado largas."""
    kept: list[PageEvent] = []
    stats: dict[str, int] = defaultdict(int)
    for e in events:
        if e.start_ts > now_ts + rules.max_future_skew_seconds:
            stats[DISCARD_FUTURE] += 1
            continue
        if e.duration_s < rules.min_page_seconds:
            stats[DISCARD_TOO_SHORT] += 1
            continue
        if e.duration_s > rules.max_page_seconds:
            stats[CLAMPED_LONG] += 1
            e = PageEvent(e.book_key, e.page, e.start_ts, rules.max_page_seconds)
        kept.append(e)
    return kept, dict(stats)


def build_blocks(events: list[PageEvent], rules: RulesConfig, tz: ZoneInfo) -> list[ReadingBlock]:
    """Agrupa páginas en bloques de lectura continua."""
    if not events:
        return []
    max_gap = rules.max_gap_minutes * 60
    ordered = sorted(events, key=lambda e: (e.start_ts, e.end_ts))
    blocks: list[ReadingBlock] = []

    def close(start: int, end: int, active: int, pages: int, books: set[str]) -> None:
        blocks.append(ReadingBlock(
            start=datetime.fromtimestamp(start, tz), end=datetime.fromtimestamp(end, tz),
            active_seconds=active, pages=pages, books=len(books),
        ))

    b_start, b_end = ordered[0].start_ts, ordered[0].end_ts
    active, pages, books = ordered[0].duration_s, 1, {ordered[0].book_key}
    for e in ordered[1:]:
        if e.start_ts - b_end > max_gap:
            close(b_start, b_end, active, pages, books)
            b_start, b_end = e.start_ts, e.end_ts
            active, pages, books = e.duration_s, 1, {e.book_key}
            continue
        # Unión de intervalos: solo suma la parte que no se solapa con lo ya contado.
        active += max(0, e.end_ts - max(e.start_ts, b_end))
        b_end = max(b_end, e.end_ts)
        pages += 1
        books.add(e.book_key)
    close(b_start, b_end, active, pages, books)
    return blocks


def units_for_block(block: ReadingBlock, rules: RulesConfig) -> int:
    return block.active_seconds // (rules.minutes_per_block * 60)


def group_blocks_by_day(blocks: list[ReadingBlock]) -> dict[date, list[ReadingBlock]]:
    out: dict[date, list[ReadingBlock]] = defaultdict(list)
    for b in blocks:
        out[b.start.date()].append(b)   # b.start ya está en la zona horaria local
    return dict(out)


def evaluate_day(
    day: date, blocks: list[ReadingBlock], credited_already: int, rules: RulesConfig,
    discarded: dict[str, int] | None = None,
) -> DayEvaluation:
    """Puntos del día completo menos lo ya acreditado (cálculo acumulativo)."""
    units = sum(units_for_block(b, rules) for b in blocks)
    raw = units * rules.points_per_block
    to_credit = max(0, min(raw, rules.daily_cap) - credited_already)
    return DayEvaluation(
        local_day=day, blocks=blocks, eligible_units=units, raw_points=raw,
        credited_points=to_credit, discarded_events=discarded or {},
    )


def make_client_ref(player_id: int | None, day: date, total_after: int) -> str:
    """Un crédito = 'llevar el día D hasta X puntos'. Mismo objetivo → mismo UUID."""
    return str(uuid.uuid5(_NAMESPACE, f"lsg-reading:{player_id}:{day.isoformat()}:{total_after}"))
