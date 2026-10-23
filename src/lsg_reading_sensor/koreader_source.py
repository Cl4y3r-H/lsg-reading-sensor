"""Fuente de datos: base de estadísticas de KOReader (statistics.sqlite3).

KOReader registra cada página leída en la tabla `page_stat_data`
(id_book, page, start_time, duration, total_pages) y los libros en `book`.
El archivo llega al sensor de dos formas:
  * webdav: KOReader lo sube con "Reading statistics → Settings → Cloud sync".
  * file:   ruta local (pruebas o copia manual).

El esquema no está documentado oficialmente: si cambia, solo hay que tocar
`_QUERY` y `read_events`. Verificar contra un archivo real.

PRIVACIDAD: títulos y autores se usan solo para calcular un hash y se descartan
en memoria; nunca se guardan en el estado local ni se envían a LSG.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import SourceConfig
from .http import NetworkError
from .models import PageEvent

log = logging.getLogger(__name__)

_QUERY = """
SELECT b.md5, b.title, b.authors, p.page, p.start_time, p.duration
FROM page_stat_data AS p
JOIN book AS b ON b.id = p.id_book
WHERE p.start_time >= ?
ORDER BY p.start_time
"""


class SourceError(Exception):
    pass


def book_key(md5: str | None, title: str | None, authors: str | None) -> str:
    basis = md5 or f"{title or ''}|{authors or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def read_events(db_path: Path, since_ts: int) -> list[PageEvent]:
    """Lee las páginas con start_time >= since_ts desde un archivo SQLite."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SourceError(f"No se pudo abrir {db_path}: {exc}") from exc
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"book", "page_stat_data"} - tables
        if missing:
            raise SourceError(f"El archivo no parece de KOReader; faltan tablas: {sorted(missing)}")
        events = []
        for md5, title, authors, page, start, duration in conn.execute(_QUERY, (since_ts,)):
            if start is None or duration is None:
                continue
            events.append(PageEvent(book_key(md5, title, authors), int(page), int(start), int(duration)))
        return events
    finally:
        conn.close()


class KOReaderSource:
    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg

    def fetch_events(self, since_ts: int) -> list[PageEvent]:
        if self.cfg.mode == "file":
            path = Path(self.cfg.file_path).expanduser()
            if not path.exists():
                raise SourceError(f"No existe {path}")
            events = read_events(path, since_ts)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "statistics.sqlite3"
                path.write_bytes(self._download())
                events = read_events(path, since_ts)
        log.info("KOReader: %d páginas desde %s", len(events), time.strftime("%Y-%m-%d", time.gmtime(since_ts)))
        return events

    def _download(self, retries: int = 3) -> bytes:
        token = base64.b64encode(f"{self.cfg.webdav_user}:{self.cfg.webdav_password}".encode()).decode()
        req = urllib.request.Request(self.cfg.webdav_url, headers={"Authorization": f"Basic {token}"})
        for attempt in range(1, retries + 2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise SourceError("statistics.sqlite3 aún no existe en el WebDAV (¿KOReader ya sincronizó?)") from None
                if exc.code in (401, 403):
                    raise SourceError(f"WebDAV rechazó las credenciales (HTTP {exc.code})") from None
                if attempt > retries:
                    raise NetworkError(f"WebDAV HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt > retries:
                    raise NetworkError(f"WebDAV: {exc}") from exc
            time.sleep(1.5 ** attempt)
        raise NetworkError("WebDAV: reintentos agotados")
