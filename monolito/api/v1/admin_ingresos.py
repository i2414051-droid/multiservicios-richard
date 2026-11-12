"""Admin — ingresos de productos (API v1, Fase 5).

Listado con filtros, comprobante, registro, anulacion y exportacion PDF,
con la misma semantica que `/ingresos`, `/registrar-ingreso`,
`/comprobante-ingreso/<id>`, `/ingresos/anular/<id>` y `/ingresos/pdf*`.
"""
from decimal import Decimal

from flask import Response, request
from pydantic import BaseModel, Field

from services import admin_ingresos_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import ingreso_a_json
from . import bp

_FILTROS = [
    {'name': 'buscar_proveedor', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'buscar_producto', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'fecha_desde', 'in': 'query',
     'schema': {'type': 'string', 'format': 'date'}},
    {'name': 'fecha_hasta', 'in': 'query',
     'schema': {'type': 'string', 'format': 'date'}},
    {'name': 'estado', 'in': 'query',
     'schema': {'type': 'string',
                'enum': ['todos', 'vigentes', 'anulados'],
                'default': 'todos'}},
]


class IngresoItemIn(BaseModel):
    producto_id: int = Field(gt=0)
    cantidad: int = Field(gt=0)
    precio_compra: Decimal = Field(ge=0)
    precio_venta: Decimal = Field(gt=0)


class IngresoIn(BaseModel):
    proveedor_id: int = Field(gt=0)
    notas: str | None = Field(default=None, max_length=2000)
    items: list[IngresoItemIn] = Field(min_length=1)


class IngresoAnularIn(BaseModel):
    motivo: str
    motivo_detalle: str | None = Field(default=None, max_length=250)


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


def _pdf(contenido, nombre):
    return Response(
        contenido,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{nombre}"'})


@bp.get('/admin/ingresos')
@doc(
    summary='Listar ingresos de productos (admin)',
    tags=['Admin'],
    auth=True,
    response_model='IngresoAdminLista',
    parameters=_FILTROS + [
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Ingresos filtrados y paginados', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_ingresos():
    filtros = admin_ingresos_service.normalizar_filtros(request.args)
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_ingresos_service.listar(
            cur, filtros, page=page, per_page=per_page)
        try:
            catalogo = admin_ingresos_service.catalogo_periodos(cur)
        except Exception:
            catalogo = {'anios': [], 'meses': [],
                        'motivos': admin_ingresos_service.MOTIVOS_ANULACION}
        pages = (total + per_page - 1) // per_page if total else 0
        return ok([ingreso_a_json(f) for f in filas], meta={
            'page': page, 'per_page': per_page, 'total': total, 'pages': pages,
            'filtros': filtros, **catalogo,
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los ingresos.') from exc
    finally:
        cur.close()


@bp.post('/admin/ingresos')
@doc(
    summary='Registrar un ingreso de productos',
    tags=['Admin'],
    auth=True,
    request='IngresoIn',
    response_model='IngresoAdmin',
    success_status=201,
    responses={201: 'Ingreso registrado (repone stock)',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Proveedor no encontrado',
               422: 'Datos invalidos'},
)
@requiere_admin
def admin_crear_ingreso():
    datos = validar_modelo(IngresoIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        ingreso_id = admin_ingresos_service.crear(cur, datos.model_dump())
        mysql.connection.commit()
        ingreso, items, totales = admin_ingresos_service.obtener(
            cur, ingreso_id)
        return ok(ingreso_a_json(ingreso, items, totales), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo registrar el ingreso.') from exc
    finally:
        cur.close()


@bp.get('/admin/ingresos.pdf')
@doc(
    summary='Exportar ingresos a PDF por periodo (binario)',
    tags=['Admin'],
    auth=True,
    parameters=[
        {'name': 'periodo', 'in': 'query',
         'schema': {'type': 'string', 'enum': ['mes', 'semana'],
                    'default': 'mes'}},
        {'name': 'anio', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1900}},
        {'name': 'mes', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 12}},
        {'name': 'semana', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 6}},
    ],
    responses={200: 'application/pdf', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Sin ingresos en el periodo', 422: 'Periodo invalido'},
)
@requiere_admin
def admin_ingresos_pdf():
    mysql, cur = abrir_cursor()
    try:
        contenido, nombre = admin_ingresos_service.pdf_periodo(
            cur, request.args.get('periodo'),
            request.args.get('anio'), request.args.get('mes'),
            request.args.get('semana'))
        return _pdf(contenido, nombre)
    except TiendaError as exc:
        raise _api_error(exc) from exc
    except Exception as exc:
        raise error_bd(exc, 'No se pudo generar el PDF.') from exc
    finally:
        cur.close()


@bp.get('/admin/ingresos/<int:ingreso_id>')
@doc(
    summary='Detalle de un ingreso (admin)',
    tags=['Admin'],
    auth=True,
    response_model='IngresoAdmin',
    responses={200: 'Ingreso con items', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Ingreso no encontrado'},
)
@requiere_admin
def admin_ver_ingreso(ingreso_id):
    mysql, cur = abrir_cursor()
    try:
        ingreso, items, totales = admin_ingresos_service.obtener(
            cur, ingreso_id)
        if not ingreso:
            raise ApiError('INGRESO_NO_ENCONTRADO', 'El ingreso no existe.', 404)
        return ok(ingreso_a_json(ingreso, items, totales))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el ingreso.') from exc
    finally:
        cur.close()


@bp.get('/admin/ingresos/<int:ingreso_id>.pdf')
@doc(
    summary='PDF de un ingreso (binario)',
    tags=['Admin'],
    auth=True,
    responses={200: 'application/pdf', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Ingreso no encontrado'},
)
@requiere_admin
def admin_ingreso_pdf(ingreso_id):
    mysql, cur = abrir_cursor()
    try:
        contenido, nombre = admin_ingresos_service.pdf_ingreso(cur, ingreso_id)
        return _pdf(contenido, nombre)
    except TiendaError as exc:
        raise _api_error(exc) from exc
    except Exception as exc:
        raise error_bd(exc, 'No se pudo generar el PDF.') from exc
    finally:
        cur.close()


@bp.post('/admin/ingresos/<int:ingreso_id>/anular')
@doc(
    summary='Anular un ingreso (revierte el stock)',
    tags=['Admin'],
    auth=True,
    request='IngresoAnularIn',
    response_model='IngresoAnulado',
    responses={200: 'Ingreso anulado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Ingreso no encontrado',
               409: 'El ingreso ya esta anulado',
               422: 'Motivo invalido'},
)
@requiere_admin
def admin_anular_ingreso(ingreso_id):
    from flask import g

    datos = validar_modelo(IngresoAnularIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        resultado = admin_ingresos_service.anular(
            cur, ingreso_id, datos.motivo, datos.motivo_detalle,
            g.usuario.get('correo'))
        mysql.connection.commit()
        return ok(resultado)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo anular el ingreso.') from exc
    finally:
        cur.close()
