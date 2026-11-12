"""Admin — salidas de productos (API v1, Fase 5).

Listado, comprobante, registro, eliminacion (revierte stock) y PDF, con la
misma semantica que `/salidas`, `/registrar-salida`,
`/comprobante-salida/<id>` y `/eliminar-salida/<id>`.
"""
from flask import Response, request
from pydantic import BaseModel, Field

from services import admin_salidas_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import salida_a_json
from . import bp


class SalidaItemIn(BaseModel):
    producto_id: int = Field(gt=0)
    cantidad: int = Field(gt=0)


class SalidaIn(BaseModel):
    venta_id: int | None = None
    notas: str | None = Field(default=None, max_length=2000)
    items: list[SalidaItemIn] = Field(min_length=1)


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


@bp.get('/admin/salidas')
@doc(
    summary='Listar salidas de productos (admin)',
    tags=['Admin'],
    auth=True,
    response_model='SalidaAdminLista',
    parameters=[
        {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'},
         'description': 'Filtra por nombre del cliente de la venta.'},
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Salidas filtradas y paginadas', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_salidas():
    filtros = admin_salidas_service.normalizar_filtros(request.args)
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_salidas_service.listar(
            cur, filtros, page=page, per_page=per_page)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok([salida_a_json(f) for f in filas], meta={
            'page': page, 'per_page': per_page, 'total': total, 'pages': pages,
            'filtros': filtros,
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar las salidas.') from exc
    finally:
        cur.close()


@bp.post('/admin/salidas')
@doc(
    summary='Registrar una salida de productos',
    tags=['Admin'],
    auth=True,
    request='SalidaIn',
    response_model='SalidaAdmin',
    success_status=201,
    responses={201: 'Salida registrada (descuenta stock)',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Venta no encontrada',
               409: 'Stock insuficiente',
               422: 'Datos invalidos'},
)
@requiere_admin
def admin_crear_salida():
    datos = validar_modelo(SalidaIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        salida_id = admin_salidas_service.crear(cur, datos.model_dump())
        mysql.connection.commit()
        salida, items = admin_salidas_service.obtener(cur, salida_id)
        return ok(salida_a_json(salida, items), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo registrar la salida.') from exc
    finally:
        cur.close()


@bp.get('/admin/salidas/<int:salida_id>')
@doc(
    summary='Detalle de una salida (admin)',
    tags=['Admin'],
    auth=True,
    response_model='SalidaAdmin',
    responses={200: 'Salida con items', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Salida no encontrada'},
)
@requiere_admin
def admin_ver_salida(salida_id):
    mysql, cur = abrir_cursor()
    try:
        salida, items = admin_salidas_service.obtener(cur, salida_id)
        if not salida:
            raise ApiError('SALIDA_NO_ENCONTRADA', 'La salida no existe.', 404)
        return ok(salida_a_json(salida, items))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la salida.') from exc
    finally:
        cur.close()


@bp.get('/admin/salidas/<int:salida_id>.pdf')
@doc(
    summary='PDF de una salida (binario)',
    tags=['Admin'],
    auth=True,
    responses={200: 'application/pdf', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Salida no encontrada'},
)
@requiere_admin
def admin_salida_pdf(salida_id):
    mysql, cur = abrir_cursor()
    try:
        contenido, nombre = admin_salidas_service.pdf_salida(cur, salida_id)
        return Response(
            contenido, mimetype='application/pdf',
            headers={'Content-Disposition':
                     f'attachment; filename="{nombre}"'})
    except TiendaError as exc:
        raise _api_error(exc) from exc
    except Exception as exc:
        raise error_bd(exc, 'No se pudo generar el PDF.') from exc
    finally:
        cur.close()


@bp.delete('/admin/salidas/<int:salida_id>')
@doc(
    summary='Eliminar una salida (revierte el stock)',
    tags=['Admin'],
    auth=True,
    response_model='SalidaEliminada',
    responses={200: 'Salida eliminada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Salida no encontrada'},
)
@requiere_admin
def admin_eliminar_salida(salida_id):
    mysql, cur = abrir_cursor()
    try:
        admin_salidas_service.eliminar(cur, salida_id)
        mysql.connection.commit()
        return ok({'id': int(salida_id), 'eliminada': True})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo eliminar la salida.') from exc
    finally:
        cur.close()
