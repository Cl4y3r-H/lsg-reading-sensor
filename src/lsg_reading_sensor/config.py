"""Configuración: archivo TOML + secretos en variables de entorno."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    pass


def _load_timezone(name: str) -> ZoneInfo:
    """En Windows, Python no trae la base de zonas horarias: se obtiene del paquete tzdata."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ConfigError(
            f"No se encontró la zona horaria '{name}'. En Windows instala la base de zonas "
            "horarias con:  python -m pip install tzdata"
        ) from None


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConfigError(f"Falta la variable de entorno {name}")
    return value


@dataclass(frozen=True)
class RulesConfig:
    """Economía y validación del ocio lector (Propuesta RF2 / GDD §2)."""

    minutes_per_block: int = 30          # P = floor(T / 30) * 5
    points_per_block: int = 5
    daily_cap: int = 20
    # Pausa máxima entre páginas para seguir en el mismo bloque continuo.
    max_gap_minutes: float = 5.0
    # Páginas más cortas se descartan (hojear rápido no es leer).
    min_page_seconds: int = 3
    # Una página no puede aportar más que esto (lector abierto y abandonado).
    max_page_seconds: int = 300
    # Tolerancia ante relojes adelantados en el teléfono.
    max_future_skew_seconds: int = 300


@dataclass(frozen=True)
class SourceConfig:
    """De dónde se obtiene statistics.sqlite3 de KOReader."""

    mode: str = "webdav"                 # webdav | file
    webdav_url: str = ""                 # URL completa al archivo statistics.sqlite3
    webdav_user: str = ""
    webdav_password_env: str = "WEBDAV_PASSWORD"
    file_path: str = ""                  # para pruebas o copia manual
    # Se reevalúan los últimos N días: cubre sincronizaciones atrasadas del teléfono.
    lookback_days: int = 7

    @property
    def webdav_password(self) -> str:
        return _env(self.webdav_password_env)


@dataclass(frozen=True)
class LsgConfig:
    auth_url: str = "https://lsg.diinf.usach.cl/lsg-auth"
    core_url: str = "https://lsg.diinf.usach.cl/lsg-core-api"
    email: str = ""
    password_env: str = "LSG_PASSWORD"
    sensor_endpoint_id: int | None = None
    players_sensor_endpoint_id: int | None = None
    # PENDIENTE: Lingüístico (5) o Mental (4). Se usa 5 por defecto.
    attribute_id: int = 5
    point_dimension_id: int = 0          # confirmar con el laboratorio
    game_id: int | None = None
    credit_strategy: str = "dry_run"     # dry_run | offline_sync | adjust
    send_webhook: bool = True
    refresh_margin_seconds: int = 600

    @property
    def password(self) -> str:
        return _env(self.password_env)


@dataclass(frozen=True)
class SensorConfig:
    timezone: ZoneInfo
    state_db: Path
    poll_interval_minutes: int
    start_date: str | None
    rules: RulesConfig
    source: SourceConfig
    lsg: LsgConfig


def load_config(path: str | Path) -> SensorConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"No existe el archivo de configuración: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    sensor = raw.get("sensor", {})
    rules = RulesConfig(**raw.get("rules", {}))
    source = SourceConfig(**raw.get("source", {}))
    lsg = LsgConfig(**raw.get("lsg", {}))

    if source.mode not in ("webdav", "file"):
        raise ConfigError(f"source.mode inválido: {source.mode}")
    if source.mode == "webdav" and not source.webdav_url:
        raise ConfigError("source.mode=webdav requiere source.webdav_url")
    if source.mode == "file" and not source.file_path:
        raise ConfigError("source.mode=file requiere source.file_path")
    if lsg.credit_strategy not in ("dry_run", "offline_sync", "adjust"):
        raise ConfigError(f"credit_strategy inválida: {lsg.credit_strategy}")
    if lsg.credit_strategy == "offline_sync" and (lsg.game_id is None or lsg.point_dimension_id <= 0):
        raise ConfigError("offline_sync requiere lsg.game_id y lsg.point_dimension_id")
    if lsg.send_webhook and lsg.sensor_endpoint_id is None:
        raise ConfigError("send_webhook requiere lsg.sensor_endpoint_id")

    return SensorConfig(
        timezone=_load_timezone(sensor.get("timezone", "America/Santiago")),
        state_db=Path(sensor.get("state_db", "reading_state.sqlite3")).expanduser(),
        poll_interval_minutes=int(sensor.get("poll_interval_minutes", 30)),
        start_date=sensor.get("start_date"),
        rules=rules,
        source=source,
        lsg=lsg,
    )
