"""Carrito autenticado de la API v1.

No hay carrito de invitado ni merge: el carrito siempre pertenece a la cuenta
del access token. La web puede seguir usando su carrito de sesion/invitado.
"""
from flask import g
from pydantic import BaseModel, Field

from services import tienda_service

from ..deps import abrir_cursor, cuerpo_json, error_bd
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_cliente
from ..serializers import carrito_a_json
from . import bp


class AgregarItemIn(BaseModel):
    producto_id: int = Field(gt=0)
    cantidad: int = Field(default=1, ge=1,
                          le=tienda_service.CANTIDAD_MAXIMA)


class ActualizarItemIn(BaseModel):
    cantidad: int = Field(ge=0, le=tienda_service.CANTIDAD_MAXIMA)


_CLAVES_RESUMEN = (
    'cantidad_items', 'lineas', 'total', 'total_centimos',
    'total_disponible', 'total_disponible_centimos', 'hay_no_disponibles',
    'envio', 'total_con_envio', 'total_con_envio_centimos', 'moneda',
)


def _usuario_id():
    return str(g.usuario['id'])


def _carrito_json(cur, usuario_id):
    filas = tienda_service.items_carrito(cur, usuario_id)
    ajustes = tienda_service.leer_ajustes(cur)
    return carrito_a_json(filas, ajustes)


def _item_de(carrito, producto_id):
    return next(
        (item for item in carrito['items']
         if item['producto_id'] == int(producto_id)),
        None,
    )


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


@bp.get('/carrito')
@doc(
    summary='Ver el carrito',
    tags=['Carrito'],
    auth=True,
    response_model='Carrito',
    responses={200: 'Carrito del usuario', 401: 'Token invalido'},
)
@requiere_cliente
def listar_carrito():
    mysql, cur = abrir_cursor()
    try:
        return ok(_carrito_json(cur, _usuario_id()))
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el carrito.') from exc
    finally:
        cur.close()


@bp.get('/carrito/resumen')
@doc(
    summary='Resumen del carrito',
    tags=['Carrito'],
    auth=True,
    response_model='ResumenCarrito',
    responses={200: 'Totales y reglas de envio', 401: 'Token invalido'},
)
@requiere_cliente
def resumen_carrito():
    mysql, cur = abrir_cursor()
    try:
        carrito = _carrito_json(cur, _usuario_id())
        return ok({clave: carrito[clave] for clave in _CLAVES_RESUMEN})
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el resumen.') from exc
    finally:
        cur.close()


@bp.post('/carrito/items')
@doc(
    summary='Agregar un producto al carrito',
    tags=['Carrito'],
    auth=True,
    request='AgregarItemIn',
    response_model='CarritoMutation',
    success_status=201,
    responses={201: 'Producto agregado',
               401: 'Token invalido',
               404: 'Producto no encontrado',
               409: 'Sin stock, stock insuficiente o tope alcanzado',
               422: 'Cantidad invalida'},
)
@requiere_cliente
def agregar_item():
    datos = validar_modelo(AgregarItemIn, cuerpo_json())
    usuario_id = _usuario_id()
    mysql, cur = abrir_cursor()
    try:
        tienda_service.agregar_item(
            cur, usuario_id, datos.producto_id, datos.cantidad)
        mysql.connection.commit()
        carrito = _carrito_json(cur, usuario_id)
        return ok({
            'item': _item_de(carrito, datos.producto_id),
            'carrito': carrito,
        }, status=201)
    except tienda_service.TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo agregar el producto al carrito.') from exc
    finally:
        cur.close()


@bp.patch('/carrito/items/<int:producto_id>')
@doc(
    summary='Actualizar la cantidad de una linea',
    tags=['Carrito'],
    auth=True,
    request='ActualizarItemIn',
    response_model='CarritoMutation',
    responses={200: 'Cantidad actualizada (0 elimina la linea)',
               401: 'Token invalido',
               404: 'La linea no existe',
               409: 'Stock insuficiente o tope alcanzado',
               422: 'Cantidad invalida'},
)
@requiere_cliente
def actualizar_item(producto_id):
    datos = validar_modelo(ActualizarItemIn, cuerpo_json())
    usuario_id = _usuario_id()
    mysql, cur = abrir_cursor()
    try:
        resultado = tienda_service.actualizar_item(
            cur, usuario_id, producto_id, datos.cantidad)
        mysql.connection.commit()
        carrito = _carrito_json(cur, usuario_id)
        return ok({
            'eliminado': resultado['eliminado'],
            'item': _item_de(carrito, producto_id),
            'carrito': carrito,
        })
    except tienda_service.TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar el carrito.') from exc
    finally:
        cur.close()


@bp.delete('/carrito/items/<int:producto_id>')
@doc(
    summary='Quitar una linea del carrito',
    tags=['Carrito'],
    auth=True,
    response_model='CarritoMutation',
    responses={200: 'Linea eliminada (idempotente)',
               401: 'Token invalido'},
)
@requiere_cliente
def eliminar_item(producto_id):
    usuario_id = _usuario_id()
    mysql, cur = abrir_cursor()
    try:
        eliminados = tienda_service.eliminar_item(cur, usuario_id, producto_id)
        mysql.connection.commit()
        carrito = _carrito_json(cur, usuario_id)
        return ok({
            'eliminado': bool(eliminados),
            'item': None,
            'carrito': carrito,
        })
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo quitar el producto del carrito.') from exc
    finally:
        cur.close()


@bp.delete('/carrito')
@doc(
    summary='Vaciar el carrito',
    tags=['Carrito'],
    auth=True,
    response_model='CarritoVaciado',
    responses={200: 'Carrito vaciado', 401: 'Token invalido'},
)
@requiere_cliente
def vaciar_carrito():
    usuario_id = _usuario_id()
    mysql, cur = abrir_cursor()
    try:
        eliminados = tienda_service.vaciar_carrito(cur, usuario_id)
        mysql.connection.commit()
        carrito = _carrito_json(cur, usuario_id)
        return ok({'eliminados': eliminados, 'carrito': carrito})
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo vaciar el carrito.') from exc
    finally:
        cur.close()


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass
