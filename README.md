# Sensor de Ocio Lector — LifeSync-Games × Project Zomboid

Lee las estadísticas de lectura de **KOReader** (Android, Linux, Kindle, Kobo),
detecta bloques de lectura continua y los convierte en puntos (GDD §2):
`P = floor(T / 30) · 5`, con tope de 20 puntos diarios.

No requiere app propia: KOReader registra cada página leída (hora y duración) y
sincroniza `statistics.sqlite3` a un servidor WebDAV. El sensor descarga ese
archivo, lo analiza y reporta a LSG-Core-API. Solo usa la biblioteca estándar
de Python ≥ 3.11.

```
[Teléfono] KOReader ──Cloud sync──► WebDAV ◄──descarga── Sensor ──► LSG (webhook + crédito)
```

## Criterio de conteo: bloques continuos con pausas toleradas

* Las páginas se ordenan por hora. Si entre dos páginas pasan más de
  `max_gap_minutes` (5), comienza un bloque nuevo.
* Cada bloque aporta `floor(minutos_activos / 30)` unidades → 5 pts por unidad.
  Dos bloques de 20 min separados NO suman una unidad.
* Minutos activos = suma del tiempo en páginas (unión de intervalos: lecturas
  simultáneas en dos dispositivos no se cuentan doble).
* Páginas < 3 s se descartan (hojeo); páginas > 300 s se recortan a 300 s
  (lector abierto y abandonado).
* El bloque pertenece al día local en que empezó.

## Privacidad

Títulos y autores solo se usan para calcular un hash en memoria. No se guardan
en el estado local ni se envían a LSG: el webhook incluye solo horarios, minutos,
cantidad de páginas y cantidad de libros distintos.

## Uso

```bash
export WEBDAV_PASSWORD=...   LSG_PASSWORD=...
cp config.example.toml config.toml

PYTHONPATH=src python -m lsg_reading_sensor -c config.toml inspect    # analiza KOReader sin tocar LSG
PYTHONPATH=src python -m lsg_reading_sensor -c config.toml check-lsg
PYTHONPATH=src python -m lsg_reading_sensor -c config.toml run
PYTHONPATH=src python -m lsg_reading_sensor -c config.toml loop
PYTHONPATH=src python -m lsg_reading_sensor -c config.toml status
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Módulos

| Archivo | Rol |
|---|---|
| `koreader_source.py` | Descarga (WebDAV) o abre (archivo) `statistics.sqlite3` y extrae páginas anonimizadas |
| `rules.py` | Filtrado de páginas, bloques continuos, puntos, tope (funciones puras) |
| `store.py` | Cola de créditos, cola de webhooks y días cerrados |
| `pipeline.py` | Una pasada: recalcula los últimos `lookback_days` días |
| `http.py`, `lsg_client.py`, `crediting.py` | Copiados del sensor de fuerza (mismos pendientes con LSG) |

## Pendientes

1. Verificar el esquema de `statistics.sqlite3` con un archivo real (tablas
   `book` y `page_stat_data`) y la ruta exacta que usa la sincronización WebDAV.
2. Soporte de CBR en KOReader (CBZ confirmado).
3. Dimensión de puntos: Lingüístico (5) o Mental (4).
4. Quién aloja el WebDAV (idealmente el laboratorio).
5. Los mismos pendientes con LSG que el sensor de fuerza (mecanismo de abono, alta del sensor).

## Contexto

Parte del trabajo de título *"Aplicación del framework LifeSync-Games al videojuego Project Zomboid
a través de un mod y sensores virtuales especializados"* (Claudio Hernández Hernández, Departamento
de Ingeniería Informática, Universidad de Santiago de Chile, 2026).

| Repositorio | Contenido |
|---|---|
| [lsg-pz-mod](https://github.com/Cl4y3r-H/lsg-pz-mod) | Mod de Project Zomboid (Build 42) y prueba de concepto Java ↔ Lua |
| [lsg-strength-sensor](https://github.com/Cl4y3r-H/lsg-strength-sensor) | Sensor de entrenamiento de fuerza (wger) |
| [lsg-reading-sensor](https://github.com/Cl4y3r-H/lsg-reading-sensor) | Sensor de ocio lector (KOReader) |
