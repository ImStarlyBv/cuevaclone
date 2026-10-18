"""Proceso worker: tres bucles independientes sobre un solo proceso.

Por qué vive aquí y no en un cron externo: los bucles operativos tienen que
seguir corriendo aunque el gateway de Hermes o el cron del host estén caídos.
El contenedor sobrevive a reinicios y se reengancha solo. El cron externo solo
lee lo que estos bucles escriben en health_runs.
"""

import asyncio
import logging
import os
import signal
import sys

from app import db

from . import jobs

DELTA_MINUTES = int(os.environ.get("DELTA_MINUTES", "15"))
CATALOG_HOURS = int(os.environ.get("CATALOG_HOURS", "24"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

_stop = asyncio.Event()


async def _run(name, fn, *args, **kwargs):
    """Ejecuta un job síncrono en un hilo sin tumbar el bucle si revienta."""
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
        log.info("%s -> %s", name, result)
        return result
    except Exception:
        log.exception("%s falló", name)
        return None


async def catalog_loop():
    # primera pasada al arrancar, luego cada CATALOG_HOURS
    while not _stop.is_set():
        await _run("catalog:peliculas", jobs.crawl_catalog)
        if _stop.is_set():
            break
        await _run("catalog:series", jobs.crawl_catalog, None, "series")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=CATALOG_HOURS * 3600)
        except asyncio.TimeoutError:
            pass


async def delta_loop():
    while not _stop.is_set():
        await _run("delta", jobs.delta_episodes)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=DELTA_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


async def resolve_loop():
    """Resuelve sin parar: primero lo caducado, luego rellena huecos."""
    while not _stop.is_set():
        good, bad = await _run("resolve", jobs.resolve_batch, 40) or (0, 0)

        # si no había nada pendiente, se ataca el poblado de fuentes
        if good == 0 and bad == 0:
            seeded = await _run("seed_sources", jobs.seed_sources, 25) or 0
            if seeded == 0:
                # nada que hacer: esperar antes de volver a mirar
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
        else:
            await asyncio.sleep(1)


async def episodes_loop():
    while not _stop.is_set():
        await _run("resolve_episodes", jobs.resolve_episodes, 20)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=600)
        except asyncio.TimeoutError:
            pass


def main():
    db.open_pool()
    db.init_schema()
    log.info("worker arrancado (rps=%s, delta=%smin)", jobs.RPS, DELTA_MINUTES)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_):
        log.info("parando worker…")
        _stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    async def _all():
        await asyncio.gather(
            catalog_loop(),
            delta_loop(),
            resolve_loop(),
            episodes_loop(),
        )

    try:
        loop.run_until_complete(_all())
    finally:
        loop.close()
        log.info("worker detenido")


if __name__ == "__main__":
    main()
