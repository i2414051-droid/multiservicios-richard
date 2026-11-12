"""Direcciones de entrega del cliente (API v1)."""
from flask import g
from pydantic import BaseModel, Field

from services import direcciones_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_cliente
from ..serializers import direccion_a_json
from . import bp


class DireccionIn(BaseModel):
    direccion: str = Field(min_length=1, max_length=300)
    distrito: str | None = Field(default=None, max_length=150)
    referencia: str | None = Field(default=None, max_length=300)
    predeterminada: bool = False


class DireccionPatchIn(BaseModel):
    direccion: str | None = Field(default=None, max_length=300)
    distrito: str | None = Field(default=None, max_length=150)
    referencia: str | None = Field(default=None, max_length=300)
    predeterminada: bool | None = None


def _usuario_id():
    return g.usuario['id']


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


@bp.get('/direcciones')
@doc(
    summary='Listar mis direcciones',
    tags=['Direcciones'],
    auth=True,
    response_model='DireccionLista',
    responses={200: 'Direcciones del usuario (predeterminada primero)',
               401: 'Token invalido'},
)
@requiere_cliente
def listar_direcciones():
    mysql, cur = abrir_cursor()
    try:
        filas = direcciones_service.listar(cur, _usuario_id())
        return ok([direccion_a_json(fila) for fila in filas])
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar las direcciones.') from exc
    finally:
        cur.close()


@bp.post('/direcciones')
@doc(
    summary='Crear una direccion',
    tags=['Direcciones'],
    auth=True,
    request='DireccionIn',
    response_model='Direccion',
    success_status=201,
    responses={201: 'Direccion creada', 401: 'Token invalido',
               422: 'Direccion invalida'},
)
@requiere_cliente
def crear_direccion():
    datos = validar_modelo(DireccionIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        nuevo_id = direcciones_service.crear(cur, _usuario_id(), datos.model_dump())
        mysql.connection.commit()
        fila = direcciones_service.obtener(cur, _usuario_id(), nuevo_id)
        return ok(direccion_a_json(fila), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo guardar la direccion.') from exc
    finally:
        cur.close()


@bp.get('/direcciones/<int:direccion_id>')
@doc(
    summary='Ver una direccion',
    tags=['Direcciones'],
    auth=True,
    response_model='Direccion',
    responses={200: 'Direccion', 401: 'Token invalido',
               404: 'Direccion no encontrada'},
)
@requiere_cliente
def ver_direccion(direccion_id):
    mysql, cur = abrir_cursor()
    try:
        fila = direcciones_service.obtener(cur, _usuario_id(), direccion_id)
        if not fila:
            raise ApiError('DIRECCION_NO_ENCONTRADA',
                           'La direccion no existe.', 404)
        return ok(direccion_a_json(fila))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la direccion.') from exc
    finally:
        cur.close()


@bp.patch('/direcciones/<int:direccion_id>')
@doc(
    summary='Actualizar una direccion',
    tags=['Direcciones'],
    auth=True,
    request='DireccionPatchIn',
    response_model='Direccion',
    responses={200: 'Direccion actualizada', 401: 'Token invalido',
               404: 'Direccion no encontrada', 422: 'Direccion invalida'},
)
@requiere_cliente
def actualizar_direccion(direccion_id):
    datos = validar_modelo(DireccionPatchIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        actual = direcciones_service.obtener(cur, _usuario_id(), direccion_id)
        if not actual:
            raise ApiError('DIRECCION_NO_ENCONTRADA',
                           'La direccion no existe.', 404)
        combinado = {
            'direccion': datos.direccion if datos.direccion is not None
            else actual.get('direccion'),
            'distrito': datos.distrito if datos.distrito is not None
            else actual.get('distrito'),
            'referencia': datos.referencia if datos.referencia is not None
            else actual.get('referencia'),
            'predeterminada': datos.predeterminada
            if datos.predeterminada is not None
            else bool(actual.get('predeterminada')),
        }
        fila = direcciones_service.actualizar(
            cur, _usuario_id(), direccion_id, combinado)
        mysql.connection.commit()
        return ok(direccion_a_json(fila))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except ApiError:
        _rollback(mysql)
        raise
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar la direccion.') from exc
    finally:
        cur.close()


@bp.delete('/direcciones/<int:direccion_id>')
@doc(
    summary='Eliminar una direccion',
    tags=['Direcciones'],
    auth=True,
    response_model='DireccionEliminada',
    responses={200: 'Direccion eliminada (idempotente)',
               401: 'Token invalido'},
)
@requiere_cliente
def eliminar_direccion(direccion_id):
    mysql, cur = abrir_cursor()
    try:
        eliminados = direcciones_service.eliminar(
            cur, _usuario_id(), direccion_id)
        mysql.connection.commit()
        return ok({'id': int(direccion_id), 'eliminado': bool(eliminados)})
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo eliminar la direccion.') from exc
    finally:
        cur.close()


@bp.post('/direcciones/<int:direccion_id>/predeterminada')
@doc(
    summary='Marcar una direccion como predeterminada',
    tags=['Direcciones'],
    auth=True,
    response_model='Direccion',
    responses={200: 'Direccion predeterminada actualizada',
               401: 'Token invalido', 404: 'Direccion no encontrada'},
)
@requiere_cliente
def predeterminar_direccion(direccion_id):
    mysql, cur = abrir_cursor()
    try:
        fila = direcciones_service.marcar_predeterminada(
            cur, _usuario_id(), direccion_id)
        mysql.connection.commit()
        return ok(direccion_a_json(fila))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo marcar la direccion.') from exc
    finally:
        cur.close()
