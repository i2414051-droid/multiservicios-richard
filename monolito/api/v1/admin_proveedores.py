"""Admin — proveedores y pedidos a proveedor (API v1, Fase 5).

CRUD de proveedores y gestion de la lista de Pedidos (`productos_para_pedir`),
con la misma semantica que `/proveedores`, `/proveedores/editar/<id>`,
`/proveedores/eliminar/<id>` y `/productos-para-pedir`.
"""
from flask import request
from pydantic import BaseModel, Field

from services import admin_proveedores_service, tienda_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import (
    grupo_compra_a_json,
    pedido_compra_a_json,
    proveedor_a_json,
)
from . import bp

_FILTROS_PROVEEDOR = [
    {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'categoria', 'in': 'query',
     'schema': {'type': 'string', 'enum': list(tienda_service.CATEGORIAS)}},
    {'name': 'page', 'in': 'query',
     'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
    {'name': 'per_page', 'in': 'query',
     'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                'default': 20}},
]


class ProveedorIn(BaseModel):
    """Alta/edicion completa. La validacion real vive en el servicio (DNI/RUC)."""
    tipo_documento: str = Field(default='dni', max_length=3)
    numero_documento: str | None = Field(default=None, max_length=20)
    nombres: str | None = Field(default=None, max_length=120)
    apellido_paterno: str | None = Field(default=None, max_length=60)
    apellido_materno: str | None = Field(default=None, max_length=60)
    razon_social: str | None = Field(default=None, max_length=120)
    celular: str | None = Field(default=None, max_length=30)
    direccion: str | None = Field(default=None, max_length=300)
    categoria: str | None = Field(default=None, max_length=100)
    notas: str | None = Field(default=None, max_length=2000)


class PedidoCompraCantidadIn(BaseModel):
    cantidad: int = Field(ge=1)


class PedidoCompraProveedorIn(BaseModel):
    proveedor_id: int | None = None


class MarcarEnviadoIn(BaseModel):
    proveedor_id: int


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


@bp.get('/admin/proveedores')
@doc(
    summary='Listar proveedores (admin)',
    tags=['Admin'],
    auth=True,
    response_model='ProveedorLista',
    parameters=_FILTROS_PROVEEDOR,
    responses={200: 'Proveedores filtrados y paginados', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_proveedores():
    filtros = admin_proveedores_service.normalizar_filtros(request.args)
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_proveedores_service.listar(
            cur, filtros, page=page, per_page=per_page)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok([proveedor_a_json(f) for f in filas], meta={
            'page': page, 'per_page': per_page, 'total': total, 'pages': pages,
            'filtros': filtros, 'categorias': list(tienda_service.CATEGORIAS),
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los proveedores.') from exc
    finally:
        cur.close()


@bp.post('/admin/proveedores')
@doc(
    summary='Crear un proveedor',
    tags=['Admin'],
    auth=True,
    request='ProveedorIn',
    response_model='Proveedor',
    success_status=201,
    responses={201: 'Proveedor creado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               409: 'Documento y categoria ya registrados',
               422: 'Datos invalidos'},
)
@requiere_admin
def admin_crear_proveedor():
    datos = validar_modelo(ProveedorIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        proveedor_id = admin_proveedores_service.crear(cur, datos.model_dump())
        mysql.connection.commit()
        return ok(proveedor_a_json(
            admin_proveedores_service.obtener(cur, proveedor_id)), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo crear el proveedor.') from exc
    finally:
        cur.close()


@bp.get('/admin/proveedores/<int:proveedor_id>')
@doc(
    summary='Detalle de un proveedor (admin)',
    tags=['Admin'],
    auth=True,
    response_model='Proveedor',
    responses={200: 'Proveedor', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Proveedor no encontrado'},
)
@requiere_admin
def admin_ver_proveedor(proveedor_id):
    mysql, cur = abrir_cursor()
    try:
        proveedor = admin_proveedores_service.obtener(cur, proveedor_id)
        if not proveedor:
            raise ApiError('PROVEEDOR_NO_ENCONTRADO', 'El proveedor no existe.',
                           404)
        return ok(proveedor_a_json(proveedor))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el proveedor.') from exc
    finally:
        cur.close()


@bp.patch('/admin/proveedores/<int:proveedor_id>')
@doc(
    summary='Actualizar un proveedor (reemplazo completo)',
    tags=['Admin'],
    auth=True,
    request='ProveedorIn',
    response_model='Proveedor',
    responses={200: 'Proveedor actualizado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Proveedor no encontrado',
               409: 'Documento y categoria ya registrados',
               422: 'Datos invalidos'},
)
@requiere_admin
def admin_actualizar_proveedor(proveedor_id):
    datos = validar_modelo(ProveedorIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_proveedores_service.actualizar(cur, proveedor_id,
                                             datos.model_dump())
        mysql.connection.commit()
        return ok(proveedor_a_json(
            admin_proveedores_service.obtener(cur, proveedor_id)))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar el proveedor.') from exc
    finally:
        cur.close()


@bp.delete('/admin/proveedores/<int:proveedor_id>')
@doc(
    summary='Eliminar un proveedor',
    tags=['Admin'],
    auth=True,
    response_model='ProveedorEliminado',
    responses={200: 'Proveedor eliminado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Proveedor no encontrado'},
)
@requiere_admin
def admin_eliminar_proveedor(proveedor_id):
    mysql, cur = abrir_cursor()
    try:
        admin_proveedores_service.eliminar(cur, proveedor_id)
        mysql.connection.commit()
        return ok({'id': int(proveedor_id), 'eliminado': True})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo eliminar el proveedor.') from exc
    finally:
        cur.close()


@bp.get('/admin/pedidos-compra')
@doc(
    summary='Listar pedidos a proveedor pendientes (admin)',
    tags=['Admin'],
    auth=True,
    response_model='PedidoCompraLista',
    parameters=[
        {'name': 'filtro', 'in': 'query',
         'schema': {'type': 'string', 'enum': ['todos', 'sin_asignar'],
                    'default': 'todos'}},
    ],
    responses={200: 'Pedidos pendientes agrupados por proveedor',
               401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_pedidos_compra():
    filtro = (request.args.get('filtro') or 'todos').strip().lower()
    mysql, cur = abrir_cursor()
    try:
        resultado = admin_proveedores_service.pedidos(cur, filtro)
        return ok([pedido_compra_a_json(p) for p in resultado['pedidos']],
                  meta={
                      'sin_asignar': resultado['sin_asignar'],
                      'filtro': resultado['filtro'],
                      'total': len(resultado['pedidos']),
                      'grupos': [grupo_compra_a_json(g)
                                 for g in resultado['grupos']],
                  })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los pedidos.') from exc
    finally:
        cur.close()


@bp.patch('/admin/pedidos-compra/<int:pedido_id>')
@doc(
    summary='Actualizar la cantidad de un pedido a proveedor',
    tags=['Admin'],
    auth=True,
    request='PedidoCompraCantidadIn',
    response_model='PedidoCompraLista',
    responses={200: 'Cantidad actualizada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Pedido no pendiente', 422: 'Cantidad invalida'},
)
@requiere_admin
def admin_actualizar_pedido_compra(pedido_id):
    datos = validar_modelo(PedidoCompraCantidadIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_proveedores_service.actualizar_cantidad(cur, pedido_id,
                                                      datos.cantidad)
        mysql.connection.commit()
        resultado = admin_proveedores_service.pedidos(cur)
        return ok([pedido_compra_a_json(p) for p in resultado['pedidos']],
                  meta={'total': len(resultado['pedidos'])})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar el pedido.') from exc
    finally:
        cur.close()


@bp.post('/admin/pedidos-compra/<int:pedido_id>/proveedor')
@doc(
    summary='Asignar (o quitar) el proveedor de un pedido',
    tags=['Admin'],
    auth=True,
    request='PedidoCompraProveedorIn',
    response_model='PedidoCompraLista',
    responses={200: 'Proveedor asignado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Pedido o proveedor no encontrado',
               409: 'El proveedor es de otra categoria'},
)
@requiere_admin
def admin_asignar_proveedor_pedido(pedido_id):
    datos = validar_modelo(PedidoCompraProveedorIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_proveedores_service.asignar_proveedor(cur, pedido_id,
                                                    datos.proveedor_id)
        mysql.connection.commit()
        resultado = admin_proveedores_service.pedidos(cur)
        return ok([pedido_compra_a_json(p) for p in resultado['pedidos']],
                  meta={'total': len(resultado['pedidos'])})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo asignar el proveedor.') from exc
    finally:
        cur.close()


@bp.delete('/admin/pedidos-compra/<int:pedido_id>/proveedor')
@doc(
    summary='Quitar el proveedor de un pedido',
    tags=['Admin'],
    auth=True,
    response_model='PedidoCompraLista',
    responses={200: 'Proveedor quitado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               409: 'El pedido ya no esta pendiente'},
)
@requiere_admin
def admin_quitar_proveedor_pedido(pedido_id):
    mysql, cur = abrir_cursor()
    try:
        admin_proveedores_service.quitar_proveedor(cur, pedido_id)
        mysql.connection.commit()
        resultado = admin_proveedores_service.pedidos(cur)
        return ok([pedido_compra_a_json(p) for p in resultado['pedidos']],
                  meta={'total': len(resultado['pedidos'])})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo quitar el proveedor.') from exc
    finally:
        cur.close()


@bp.post('/admin/pedidos-compra/marcar-enviado')
@doc(
    summary='Marcar como enviados todos los pendientes de un proveedor',
    tags=['Admin'],
    auth=True,
    request='MarcarEnviadoIn',
    response_model='PedidoCompraEnviado',
    responses={200: 'Pedido marcado como enviado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Proveedor no encontrado', 422: 'Proveedor invalido'},
)
@requiere_admin
def admin_marcar_enviado_pedido():
    datos = validar_modelo(MarcarEnviadoIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        enviados, prov = admin_proveedores_service.marcar_enviado(
            cur, datos.proveedor_id)
        mysql.connection.commit()
        return ok({'proveedor_id': datos.proveedor_id,
                   'proveedor_nombre': prov.get('nombre') or '',
                   'enviados': enviados})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo marcar el pedido como enviado.') from exc
    finally:
        cur.close()
