import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lsg_reading_sensor.config import LsgConfig, RulesConfig, SensorConfig, SourceConfig
from lsg_reading_sensor.crediting import CreditOutcome
from lsg_reading_sensor.http import NetworkError
from lsg_reading_sensor.koreader_source import KOReaderSource, SourceError, read_events
from lsg_reading_sensor.models import CreditStatus, PageEvent
from lsg_reading_sensor.pipeline import ReadingPipeline
from lsg_reading_sensor.rules import (build_blocks, evaluate_day, filter_events, group_blocks_by_day,
                                      make_client_ref)
from lsg_reading_sensor.store import StateStore

TZ = ZoneInfo("America/Santiago")
R = RulesConfig()


def ts(day, hour, minute=0, second=0):
    return int(datetime(2026, 10, day, hour, minute, second, tzinfo=TZ).timestamp())


def reading(day, hour, minutes, start_minute=0, page_s=60, book="b1", first_page=1):
    """Lectura continua: una página cada `page_s` segundos durante `minutes` minutos."""
    n = int(minutes * 60 // page_s)
    t0 = ts(day, hour, start_minute)
    return [PageEvent(book, first_page + i, t0 + i * page_s, page_s) for i in range(n)]


# ----------------------------------------------------------------- Reglas
class FilterTest(unittest.TestCase):
    def test_discards_and_clamps(self):
        now = ts(13, 20)
        evs = [PageEvent("b", 1, ts(13, 10), 1),          # demasiado corta
               PageEvent("b", 2, ts(13, 23), 60),         # en el futuro
               PageEvent("b", 3, ts(13, 11), 900),        # muy larga → se recorta a 300
               PageEvent("b", 4, ts(13, 12), 60)]
        kept, stats = filter_events(evs, R, now)
        self.assertEqual([e.page for e in kept], [3, 4])
        self.assertEqual(kept[0].duration_s, 300)
        self.assertEqual(stats, {"PAGE_TOO_SHORT": 1, "FUTURE_TIMESTAMP": 1, "PAGE_CLAMPED": 1})


class BlocksTest(unittest.TestCase):
    def test_short_pause_keeps_block(self):
        evs = reading(13, 18, 20) + reading(13, 18, 15, start_minute=24)   # pausa de 4 min
        blocks = build_blocks(evs, R, TZ)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].active_seconds, 35 * 60)

    def test_long_pause_splits_block(self):
        evs = reading(13, 18, 20) + reading(13, 18, 20, start_minute=30)   # pausa de 10 min
        blocks = build_blocks(evs, R, TZ)
        self.assertEqual([b.active_minutes for b in blocks], [20.0, 20.0])
        # Dos bloques de 20 min NO suman una unidad: la lectura debe ser continua.
        self.assertEqual(evaluate_day(date(2026, 10, 13), blocks, 0, R).eligible_units, 0)

    def test_overlap_counted_once(self):
        a = reading(13, 18, 30, book="tablet")
        b = reading(13, 18, 30, book="phone")   # mismo horario en otro dispositivo
        blocks = build_blocks(a + b, R, TZ)
        self.assertEqual(blocks[0].active_seconds, 30 * 60)
        self.assertEqual(blocks[0].books, 2)

    def test_block_belongs_to_start_day(self):
        evs = reading(13, 23, 45, start_minute=40)             # 23:40 → 00:25
        by_day = group_blocks_by_day(build_blocks(evs, R, TZ))
        self.assertEqual(list(by_day), [date(2026, 10, 13)])


class PointsTest(unittest.TestCase):
    def _pts(self, minutes, credited=0):
        blocks = build_blocks(reading(13, 18, minutes), R, TZ)
        return evaluate_day(date(2026, 10, 13), blocks, credited, R)

    def test_thresholds(self):
        self.assertEqual([self._pts(m).credited_points for m in (29, 30, 59, 65, 90)], [0, 5, 5, 10, 15])

    def test_daily_cap(self):
        ev = self._pts(180)           # 6 unidades = 30 pts brutos
        self.assertEqual((ev.raw_points, ev.credited_points), (30, 20))

    def test_cumulative(self):
        self.assertEqual(self._pts(65, credited=5).credited_points, 5)
        self.assertEqual(self._pts(180, credited=20).credited_points, 0)

    def test_client_ref(self):
        d = date(2026, 10, 13)
        self.assertEqual(make_client_ref(50, d, 10), make_client_ref(50, d, 10))
        self.assertNotEqual(make_client_ref(50, d, 10), make_client_ref(50, d, 15))


# ------------------------------------------------------------- Fuente KOReader
def make_koreader_db(path: Path, events: list[tuple]) -> None:
    """Crea un statistics.sqlite3 con el esquema de KOReader. events: (titulo, page, start, duration)."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT, authors TEXT, notes INTEGER, last_open INTEGER,
            highlights INTEGER, pages INTEGER, series TEXT, language TEXT, md5 TEXT,
            total_read_time INTEGER, total_read_pages INTEGER);
        CREATE TABLE page_stat_data (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER,
            total_pages INTEGER, UNIQUE (id_book, page, start_time));
    """)
    ids = {}
    for title, page, start, dur in events:
        if title not in ids:
            ids[title] = len(ids) + 1
            c.execute("INSERT INTO book(id, title, authors, md5) VALUES (?,?,?,?)",
                      (ids[title], title, "Autor", f"md5-{title}"))
        c.execute("INSERT INTO page_stat_data VALUES (?,?,?,?,?)", (ids[title], page, start, dur, 200))
    c.commit()
    c.close()


class SourceTest(unittest.TestCase):
    def test_reads_and_hides_titles(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "statistics.sqlite3"
            make_koreader_db(db, [("Berserk Vol 1", 1, ts(13, 18), 40), ("Berserk Vol 1", 2, ts(13, 18, 1), 45),
                                  ("Otro", 1, ts(10, 18), 30)])
            evs = read_events(db, since_ts=ts(12, 0))
            self.assertEqual([(e.page, e.duration_s) for e in evs], [(1, 40), (2, 45)])
            self.assertNotIn("Berserk", repr(evs))
            self.assertEqual(len(evs[0].book_key), 16)

    def test_rejects_non_koreader_file(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.sqlite3"
            sqlite3.connect(db).execute("CREATE TABLE other (x)").connection.commit()
            with self.assertRaises(SourceError):
                read_events(db, 0)


# ----------------------------------------------------------------- Pipeline
class FakeSource:
    def __init__(self):
        self.events: list[PageEvent] = []

    def fetch_events(self, since_ts):
        return [e for e in self.events if e.start_ts >= since_ts]


class FakeCreditor:
    name = "fake"

    def __init__(self):
        self.fail = False
        self.received = []

    def credit(self, record):
        if self.fail:
            raise NetworkError("sin conexión")
        self.received.append(record)
        return CreditOutcome(CreditStatus.SYNCED, remote_ref="L1")


class FakeCore:
    def __init__(self):
        self.webhooks = []

    def ingest_webhook(self, payload):
        self.webhooks.append(payload)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = SensorConfig(timezone=TZ, state_db=Path(self.tmp.name) / "s.db", poll_interval_minutes=30,
                           start_date=None, rules=R, source=SourceConfig(mode="file", file_path="x"),
                           lsg=LsgConfig(sensor_endpoint_id=8, players_sensor_endpoint_id=13))
        self.store = StateStore(cfg.state_db)
        self.src, self.cred, self.core = FakeSource(), FakeCreditor(), FakeCore()
        self.p = ReadingPipeline(cfg, self.store, self.src, self.cred, self.core, player_id=50)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def total(self):
        return sum(r.amount for r in self.cred.received)

    def test_rerun_does_not_double_credit(self):
        self.src.events = reading(13, 18, 65)
        self.p.run_once(now_ts=ts(13, 20))
        self.p.run_once(now_ts=ts(13, 21))
        self.assertEqual(self.total(), 10)

    def test_reading_in_progress_is_credited_incrementally(self):
        self.src.events = reading(13, 18, 20)                       # sincroniza a mitad de lectura
        self.p.run_once(now_ts=ts(13, 18, 25))
        self.assertEqual(self.total(), 0)
        self.src.events = reading(13, 18, 65)                       # el mismo bloque continúa
        self.p.run_once(now_ts=ts(13, 19, 30))
        self.assertEqual(self.total(), 10)

    def test_cap_across_passes(self):
        self.src.events = reading(13, 15, 100)
        self.p.run_once(now_ts=ts(13, 17))
        self.src.events += reading(13, 19, 120, book="b2")
        self.p.run_once(now_ts=ts(13, 22))
        self.assertEqual(self.total(), 20)

    def test_network_failure_then_recovery(self):
        self.src.events = reading(13, 18, 30)
        self.cred.fail = True
        s1 = self.p.run_once(now_ts=ts(13, 20))
        self.assertEqual(s1.credits_pending, 1)
        self.cred.fail = False
        s2 = self.p.run_once(now_ts=ts(13, 21))
        self.assertEqual((s2.credits_synced, s2.points_enqueued), (1, 0))
        self.assertEqual(self.total(), 5)

    def test_day_closed_summary_once(self):
        self.src.events = reading(12, 18, 10)                       # 10 min: no da puntos
        self.p.run_once(now_ts=ts(13, 9))
        self.p.run_once(now_ts=ts(13, 10))
        kinds = [w["raw_payload"]["kind"] for w in self.core.webhooks]
        self.assertEqual(kinds, ["day_closed"])
        self.assertEqual(self.core.webhooks[0]["status"], "IGNORED")

    def test_webhook_has_no_titles(self):
        self.src.events = reading(13, 18, 30, book="hash123")
        self.p.run_once(now_ts=ts(13, 20))
        body = json.dumps(self.core.webhooks)
        self.assertNotIn("hash123", body)          # ni siquiera el hash del libro sale del equipo
        self.assertIn('"books": 1', body)


if __name__ == "__main__":
    unittest.main()
