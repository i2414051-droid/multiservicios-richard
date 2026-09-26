"""
Retencion de datos de KPIs.

alwaysdata tiene una cuota de base de datos limitada. Sin purga, la tabla de
peticiones crece un dia de forma indefinida y acaba haciendo fallar las
escrituras de la tienda entera, que es un efecto secundario inaceptable de
un panel de metricas.

La purga se dispara de forma probabilistica desde una peticion de la
aplicacion, no desde un cron, porque asi no hace falta configurar nada en
Render ni anadir dependencias.
"""
import random
import threading

from flask import current_app

from . import repository, settings

# Probabilidad de lanzar la purga en cada peticion. A un promedio de unas
# decenas de peticiones por minuto, se ejecuta aproximadamente una vez cada
# pocas horas: suficiente para no crecer, barata de ejecutar.
PURGE_PROBABILITY = 0.002

_purge_lock = threading.Lock()
_last_purge = None


def maybe_purge(app, force=False):
    """
    Ejecuta la purga si toca. Devuelve un dict con lo borrado, o None.

    force=True la ejecuta siempre (se usa desde el boton manual del panel).
    """
    global _last_purge

    if not force and random.random() > PURGE_PROBABILITY:
        return None

    # El lock evita que varias peticiones concurrentes purguen a la vez y se
    # peleen por la misma tabla.
    if not _purge_lock.acquire(blocking=False):
        return None
    try:
        with app.app_context():
            resultado = repository.purge_old_metrics(settings.RETENTION_DAYS)
        _last_purge = resultado
        try:
            current_app.logger.info('KPI: purga realizada %s', resultado)
        except Exception:
            pass
        return resultado
    except Exception:
        return None
    finally:
        _purge_lock.release()


def describe_retention():
    """Texto de la politica vigente, para mostrarlo en el panel."""
    return (
        f'Metricas crudas: {settings.RETENTION_DAYS} dias. '
        f'Chequeos de salud: {settings.HEALTH_RETENTION_DAYS} dias. '
        f'Volcado por lotes cada {settings.FLUSH_INTERVAL_SECONDS}s '
        f'o {settings.FLUSH_BATCH_SIZE} registros.'
    )
