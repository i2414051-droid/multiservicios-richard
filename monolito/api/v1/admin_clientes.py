"""Admin — clientes y permisos (API v1, Fase 4).

Reproduce las reglas de `/clientes`, `/clientes/<id>/estado`,
`/clientes/<id>/historial` y `/permisos`: estados validos, prohibicion de
auto-bloqueo, historial de cambios y la regla del administrador principal
(`SUPERADMIN_CORREO`, por defecto `admin@mail.com`).
"""
import os

from flask import g, request
from pydantic import BaseModel, Field

from services import admin_clientes_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import (
    cliente_admin_a_json,
    historial_estado_a_json,
    usuario_permiso_a_json,
)
from . import bp

SUPERADMIN_CORREO = os.environ.get('SUPERADMIN_CORREO', 'admin@mail.com').lower()


class ClienteEstadoIn(BaseModel):
    estado: str
    motivo: str | None = Field(default=None, max_length=300)


class RolIn(BaseModel):
    rol: str


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


def _es_superadmin():
    return (g.usuario.get('correo') or '').lower() == SUPERADMIN_CORREO


@bp.get('/admin/clientes')
@doc(
    summary='Listar clientes (admin)',
    tags=['Admin'],
    auth=True,
    response_model='ClienteAdminLista',
    parameters=[
        {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'filtro', 'in': 'query',
         'schema': {'type': 'string', 'default': 'todos',
                    'enum': ['todos', 'activo', 'inactivo', 'bloqueado',
                             'pendiente_verificacion', 'moroso']}},
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Clientes paginados', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_clientes():
    buscar = (request.args.get('buscar') or '').strip()[:100]
    filtro = (request.args.get('filtro') or 'todos').strip()
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total, conteos = admin_clientes_service.listar(
            cur, buscar=buscar, filtro=filtro, page=page, per_page=per_page)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(
            [cliente_admin_a_json(fila) for fila in filas],
            meta={
                'page': page, 'per_page': per_page,
                'total': total, 'pages': pages,
                'buscar': buscar, 'filtro': filtro,
                'conteos': conteos,
            },
        )
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los clientes.') from exc
    finally:
        cur.close()


@bp.get('/admin/clientes/<int:user_id>')
@doc(
    summary='Detalle de un cliente (admin)',
    tags=['Admin'],
    auth=True,
    response_model='ClienteAdmin',
    responses={200: 'Cliente con historial de compras',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Cliente no encontrado'},
)
@requiere_admin
def admin_ver_cliente(user_id):
    mysql, cur = abrir_cursor()
    try:
        fila = admin_clientes_service.obtener(cur, user_id)
        if not fila:
            raise ApiError('CLIENTE_NO_ENCONTRADO', 'Cliente no encontrado.', 404)
        return ok(cliente_admin_a_json(fila))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el cliente.') from exc
    finally:
        cur.close()


@bp.patch('/admin/clientes/<int:user_id>/estado')
@doc(
    summary='Cambiar el estado de un cliente',
    tags=['Admin'],
    auth=True,
    request='ClienteEstadoIn',
    response_model='ClienteEstadoCambiado',
    responses={200: 'Estado actualizado', 401: 'Token invalido',
               403: 'Solo el administrador principal',
               404: 'Cliente no encontrado',
               409: 'No puedes bloquearte a ti mismo',
               422: 'Estado invalido'},
)
@requiere_admin
def admin_estado_cliente(user_id):
    datos = validar_modelo(ClienteEstadoIn, cuerpo_json())
    es_mismo = int(user_id) == int(g.usuario['id'])
    mysql, cur = abrir_cursor()
    try:
        resultado = admin_clientes_service.cambiar_estado(
            cur, user_id, datos.estado, datos.motivo, g.usuario['id'],
            es_superadmin=_es_superadmin(), es_mismo_usuario=es_mismo)
        mysql.connection.commit()
        return ok({'id': int(user_id), **resultado})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo cambiar el estado del cliente.') from exc
    finally:
        cur.close()


@bp.get('/admin/clientes/<int:user_id>/historial')
@doc(
    summary='Historial de estados de un cliente',
    tags=['Admin'],
    auth=True,
    response_model='HistorialEstadoLista',
    responses={200: 'Ultimos 50 cambios de estado', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_historial_cliente(user_id):
    mysql, cur = abrir_cursor()
    try:
        filas = admin_clientes_service.historial(cur, user_id)
        return ok([historial_estado_a_json(fila) for fila in filas])
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el historial.') from exc
    finally:
        cur.close()


@bp.get('/admin/usuarios')
@doc(
    summary='Listar usuarios y roles',
    tags=['Admin'],
    auth=True,
    response_model='UsuarioPermisoLista',
    parameters=[
        {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Usuarios con su rol', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_usuarios():
    buscar = (request.args.get('buscar') or '').strip()[:100]
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_clientes_service.listar_usuarios(
            cur, buscar=buscar, page=page, per_page=per_page)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(
            [usuario_permiso_a_json(fila) for fila in filas],
            meta={
                'page': page, 'per_page': per_page,
                'total': total, 'pages': pages, 'buscar': buscar,
                'es_superadmin': _es_superadmin(),
            },
        )
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los usuarios.') from exc
    finally:
        cur.close()


@bp.patch('/admin/usuarios/<int:user_id>/rol')
@doc(
    summary='Cambiar el rol de un usuario',
    tags=['Admin'],
    auth=True,
    request='RolIn',
    response_model='RolActualizado',
    responses={200: 'Rol actualizado', 401: 'Token invalido',
               403: 'Solo el administrador principal puede cambiar roles',
               404: 'Usuario no encontrado', 422: 'Rol invalido'},
)
@requiere_admin
def admin_rol_usuario(user_id):
    datos = validar_modelo(RolIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        rol = admin_clientes_service.cambiar_rol(
            cur, user_id, datos.rol, es_superadmin=_es_superadmin())
        mysql.connection.commit()
        return ok({'id': int(user_id), 'rol': rol})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo cambiar el rol.') from exc
    finally:
        cur.close()
