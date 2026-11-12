"""Endpoints de sistema: salud y version de la API."""
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..deps import get_mysql
from ..errors import ok
from ..openapi import doc
from . import bp

_LIMA = timezone(timedelta(hours=-5))


def _ahora_lima():
    return datetime.now(timezone.utc).astimezone(_LIMA).isoformat(timespec='seconds')


@bp.get('/health')
@doc(
    summary='Estado del servicio',
    tags=['Sistema'],
    responses={200: 'Servicio y base de datos operativos',
               503: 'Servicio degradado: la base de datos no responde'},
)
def health():
    db_ok = False
    detalle = None
    try:
        cur = get_mysql().connection.cursor()
        try:
            cur.execute('SELECT 1 AS ok')
            cur.fetchone()
            db_ok = True
        finally:
            cur.close()
    except Exception as exc:
        detalle = str(exc)[:200]

    datos = {
        'servicio': 'multiservicios-richard-api',
        'version': '1.0.0',
        'base_datos': 'ok' if db_ok else 'error',
        'hora': _ahora_lima(),
    }
    if detalle:
        datos['detalle'] = detalle
    if db_ok:
        return ok(datos)
    return ok(datos, status=503)


@bp.get('/version')
@doc(
    summary='Version de la API',
    tags=['Sistema'],
    responses={200: 'Version actual'},
)
def version():
    return ok({
        'api': 'v1',
        'version': '1.0.0',
        'app': current_app.name,
    })
