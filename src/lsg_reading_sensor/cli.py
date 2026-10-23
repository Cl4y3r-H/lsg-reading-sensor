"""Interfaz de línea de comandos.

    python -m lsg_reading_sensor -c config.toml inspect     # analiza KOReader sin tocar LSG
    python -m lsg_reading_sensor -c config.toml check-lsg   # login, roles, sensores, saldo
    python -m lsg_reading_sensor -c config.toml run         # una pasada
    python -m lsg_reading_sensor -c config.toml loop        # pasadas periódicas
    python -m lsg_reading_sensor -c config.toml status      # estado local
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict

from .config import ConfigError, SensorConfig, load_config
from .crediting import build_creditor
from .http import HttpError, NetworkError
from .koreader_source import KOReaderSource, SourceError
from .lsg_client import LsgAuth, LsgCoreClient
from .pipeline import ReadingPipeline
from .rules import build_blocks, evaluate_day, filter_events, group_blocks_by_day
from .store import StateStore

log = logging.getLogger("lsg_reading_sensor")


def _build(cfg: SensorConfig) -> tuple[ReadingPipeline, StateStore]:
    store = StateStore(cfg.state_db)
    core = player_id = None
    if cfg.lsg.email:
        auth = LsgAuth(cfg.lsg)
        core = LsgCoreClient(cfg.lsg, auth)
        player_id = auth.player_id
    elif cfg.lsg.credit_strategy != "dry_run" or cfg.lsg.send_webhook:
        raise ConfigError("lsg.email es obligatorio salvo en dry_run sin webhook")
    creditor = build_creditor(cfg.lsg.credit_strategy, core, cfg.lsg.point_dimension_id)
    return ReadingPipeline(cfg, store, KOReaderSource(cfg.source), creditor, core, player_id), store


def cmd_run(cfg: SensorConfig) -> int:
    pipeline, store = _build(cfg)
    try:
        print(json.dumps(asdict(pipeline.run_once()), indent=2))
    finally:
        store.close()
    return 0


def cmd_loop(cfg: SensorConfig) -> int:
    pipeline, store = _build(cfg)
    try:
        while True:
            try:
                log.info("Pasada: %s", asdict(pipeline.run_once()))
            except (NetworkError, HttpError, SourceError) as exc:
                log.warning("Pasada fallida, se reintenta en el siguiente ciclo: %s", exc)
            time.sleep(max(1, cfg.poll_interval_minutes) * 60)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


def cmd_inspect(cfg: SensorConfig) -> int:
    """Muestra bloques y puntos por día SIN escribir estado ni contactar a LSG."""
    now = int(time.time())
    since = now - cfg.source.lookback_days * 86400
    events, discarded = filter_events(KOReaderSource(cfg.source).fetch_events(since), cfg.rules, now)
    for day, blocks in sorted(group_blocks_by_day(build_blocks(events, cfg.rules, cfg.timezone)).items()):
        ev = evaluate_day(day, blocks, 0, cfg.rules)
        print(f"{day}  {ev.active_minutes:6.1f} min  {ev.eligible_units} unidades  "
              f"{min(ev.raw_points, cfg.rules.daily_cap)} pts")
        for b in blocks:
            print(f"    {b.start:%H:%M}-{b.end:%H:%M}  {b.active_minutes:5.1f} min  {b.pages} págs  {b.books} libro(s)")
    print("páginas descartadas/recortadas:", discarded)
    return 0


def cmd_status(cfg: SensorConfig) -> int:
    store = StateStore(cfg.state_db)
    try:
        c = store.conn
        print(json.dumps({
            "credits_by_status": {r[0]: {"count": r[1], "points": r[2]}
                                  for r in c.execute("SELECT status, COUNT(*), SUM(amount) FROM credits GROUP BY status")},
            "points_by_day": {r[0]: r[1] for r in c.execute(
                "SELECT local_day, SUM(amount) FROM credits GROUP BY local_day ORDER BY local_day DESC LIMIT 7")},
            "closed_days": c.execute("SELECT COUNT(*) FROM closed_days").fetchone()[0],
            "webhooks_pending": c.execute("SELECT COUNT(*) FROM webhook_outbox WHERE sent=0").fetchone()[0],
        }, indent=2))
    finally:
        store.close()
    return 0


def cmd_check_lsg(cfg: SensorConfig) -> int:
    auth = LsgAuth(cfg.lsg)
    core = LsgCoreClient(cfg.lsg, auth)
    print("health:", core.health())
    print("player_id:", auth.player_id, "roles:", auth.roles)
    print("sensores vinculados:", json.dumps(core.linked_sensors(), indent=2, ensure_ascii=False))
    print("saldo:", json.dumps(core.balance(), indent=2, ensure_ascii=False))
    return 0


COMMANDS = {"run": cmd_run, "loop": cmd_loop, "inspect": cmd_inspect,
            "status": cmd_status, "check-lsg": cmd_check_lsg}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lsg_reading_sensor")
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return COMMANDS[args.command](load_config(args.config))
    except (ConfigError, SourceError) as exc:
        log.error("%s", exc)
        return 2
