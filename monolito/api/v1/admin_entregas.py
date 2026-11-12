"""Admin — entregas / seguimiento (API v1, Fase 5).

Listado con filtros, detalle, alta, cambio de estado, eliminacion, productos de
la entrega y asignacion de entrega a una venta. Reproduce `/entregas`,
`/entregas/crear/<venta_id>`, `/entregas/actualizar/<id>`,
`/entregas/eliminar/<id>`, `/entregas/detalle/<id>`,
`/entregas/agregar-productos/<id>` y `/entregas/asignar/<venta_id>`.
"""
from flask import request
from pydantic import BaseModel, Field

from services import admin_entregas_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import entrega_a_json
from . import bp

_FILTROS = [
    {'name': 'filtro', 'in': 'query',
     'schema': {'type': 'string',
                'enum': ['todas', 'pendientes', 'en_camino', 'entregado',
                         'cancelado'],
                'default': 'todas'}},
    {'name': 'motivo', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'page', 'in': 'query',
     'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
    {'name': 'per_page', 'in': 'query',
     'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                'default': 20}},
]


class ProductoCantidadIn(BaseModel):
    producto_id: int = Field(gt=0)
    cantidad: int = Field(default=1, ge=1)


class EntregaIn(BaseModel):
    venta_id: int = Field(gt=0)
    direccion_envio: str | None = Field(default=None, max_length=400)
    fecha_estimada: str | None = None
    notas: str | None = Field(default=None, max_length=2000)


class EntregaPatchIn(BaseModel):
    estado: str
    notas: str | None = Field(default=None, max_length=2000)


class EntregaProductosIn(BaseModel):
    items: list[ProductoCantidadIn] = Field(default_factory=list)


class AsignarEntregaIn(BaseModel):
    direccion_envio: str | None = Field(default=None, max_length=400)
    fecha_estimada: str | None = None
    notas: str | None = Field(default=None, max_length=2000)
    items: list[ProductoCantidadIn] = Field(default_factory=list)


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


def _detalle(cur, entrega_id):
    entrega, productos_venta, productos_entrega = \
        admin_entregas_service.obtener(cur, entrega_id)
    if not entrega:
        raise ApiError('ENTREGA_NO_ENCONTRADA', 'La entrega no existe.', 404)
    return entrega_a_json(entrega, productos_venta, productos_entrega)


@bp.get('/admin/entregas')
@doc(
    summary='Listar entregas (admin)',
    tags=['Admin'],
    auth=True,
    response_model='EntregaAdminLista',
    parameters=_FILTROS,
    responses={200: 'Entregas filtradas y paginadas', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_entregas():
    filtros = admin_entregas_service.normalizar_filtros(request.args)
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_entregas_service.listar(
            cur, filtros, page=page, per_page=per_page)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok([entrega_a_json(f) for f in filas], meta={
            'page': page, 'per_page': per_page, 'total': total, 'pages': pages,
            'filtros': filtros,
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar las entregas.') from exc
    finally:
        cur.close()


@bp.post('/admin/entregas')
@doc(
    summary='Crear el seguimiento de una venta',
    tags=['Admin'],
    auth=True,
    request='EntregaIn',
    response_model='EntregaAdmin',
    success_status=201,
    responses={201: 'Entrega creada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Venta no encontrada',
               409: 'La venta ya tiene seguimiento',
               422: 'Datos invalidos'},
)
@requiere_admin
def admin_crear_entrega():
    datos = validar_modelo(EntregaIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        entrega_id = admin_entregas_service.crear(cur, datos.model_dump())
        mysql.connection.commit()
        return ok(_detalle(cur, entrega_id), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo crear la entrega.') from exc
    finally:
        cur.close()


@bp.get('/admin/entregas/<int:entrega_id>')
@doc(
    summary='Detalle de una entrega (admin)',
    tags=['Admin'],
    auth=True,
    response_model='EntregaAdmin',
    responses={200: 'Entrega con productos', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Entrega no encontrada'},
)
@requiere_admin
def admin_ver_entrega(entrega_id):
    mysql, cur = abrir_cursor()
    try:
        return ok(_detalle(cur, entrega_id))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la entrega.') from exc
    finally:
        cur.close()


@bp.patch('/admin/entregas/<int:entrega_id>')
@doc(
    summary='Actualizar el estado de una entrega',
    tags=['Admin'],
    auth=True,
    request='EntregaPatchIn',
    response_model='EntregaAdmin',
    responses={200: 'Estado actualizado (cancelar devuelve stock)',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Entrega no encontrada',
               409: 'La venta ya esta cancelada',
               422: 'Estado invalido'},
)
@requiere_admin
def admin_actualizar_entrega(entrega_id):
    datos = validar_modelo(EntregaPatchIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_entregas_service.actualizar(
            cur, entrega_id, datos.model_dump(exclude_unset=True))
        mysql.connection.commit()
        return ok(_detalle(cur, entrega_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar la entrega.') from exc
    finally:
        cur.close()


@bp.delete('/admin/entregas/<int:entrega_id>')
@doc(
    summary='Eliminar una entrega',
    tags=['Admin'],
    auth=True,
    response_model='EntregaEliminada',
    responses={200: 'Entrega eliminada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Entrega no encontrada'},
)
@requiere_admin
def admin_eliminar_entrega(entrega_id):
    mysql, cur = abrir_cursor()
    try:
        admin_entregas_service.eliminar(cur, entrega_id)
        mysql.connection.commit()
        return ok({'id': int(entrega_id), 'eliminada': True})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo eliminar la entrega.') from exc
    finally:
        cur.close()


@bp.post('/admin/entregas/<int:entrega_id>/productos')
@doc(
    summary='Reemplazar los productos de una entrega',
    tags=['Admin'],
    auth=True,
    request='EntregaProductosIn',
    response_model='EntregaAdmin',
    responses={200: 'Productos actualizados', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Entrega no encontrada'},
)
@requiere_admin
def admin_productos_entrega(entrega_id):
    datos = validar_modelo(EntregaProductosIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_entregas_service.set_productos(
            cur, entrega_id, [i.model_dump() for i in datos.items])
        mysql.connection.commit()
        return ok(_detalle(cur, entrega_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudieron actualizar los productos.') from exc
    finally:
        cur.close()


@bp.post('/admin/ventas/<int:venta_id>/entrega')
@doc(
    summary='Crear o actualizar la entrega de una venta',
    tags=['Admin'],
    auth=True,
    request='AsignarEntregaIn',
    response_model='EntregaAsignada',
    responses={200: 'Entrega creada o actualizada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Venta no encontrada'},
)
@requiere_admin
def admin_asignar_entrega(venta_id):
    datos = validar_modelo(AsignarEntregaIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        entrega_id, accion = admin_entregas_service.asignar(
            cur, venta_id, datos.model_dump(),
            [i.model_dump() for i in datos.items])
        mysql.connection.commit()
        return ok({'venta_id': int(venta_id), 'entrega_id': entrega_id,
                   'accion': accion})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo asignar la entrega.') from exc
    finally:
        cur.close()
