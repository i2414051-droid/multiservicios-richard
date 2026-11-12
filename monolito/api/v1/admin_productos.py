"""Admin — productos (API v1, Fase 4).

CRUD de productos con la misma semantica que las pantallas `/admin`,
`/agregar_producto`, `/editar_producto`, `/activar_producto` y
`/eliminar_producto`. Las imagenes se suben por separado con
`multipart/form-data`.
"""
from decimal import Decimal

from flask import request
from pydantic import BaseModel, Field

from services import admin_productos_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import producto_admin_a_json
from . import bp

_PRODUCTO_NO_ENCONTRADO = 'El producto no existe.'


class ProductoAdminIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str | None = Field(default=None, max_length=2000)
    precio: Decimal = Field(ge=0)
    stock: int = Field(ge=0)
    categoria: str | None = Field(default=None, max_length=100)
    estado: str | None = None


class ProductoAdminPatchIn(BaseModel):
    nombre: str | None = Field(default=None, max_length=200)
    descripcion: str | None = Field(default=None, max_length=2000)
    precio: Decimal | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)
    categoria: str | None = Field(default=None, max_length=100)
    estado: str | None = None


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


def _producto_admin(cur, producto_id):
    producto = admin_productos_service.obtener(cur, producto_id)
    if not producto:
        raise ApiError('PRODUCTO_NO_ENCONTRADO', _PRODUCTO_NO_ENCONTRADO, 404)
    filas = admin_productos_service.imagenes(cur, producto_id)
    extras = [f.get('imagen') for f in filas if f.get('imagen')]
    return producto_admin_a_json(producto, extras, filas)


@bp.get('/admin/productos')
@doc(
    summary='Listar productos (admin)',
    tags=['Admin'],
    auth=True,
    response_model='ProductoAdminLista',
    parameters=[
        {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Productos activos e inactivos', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_productos():
    buscar = (request.args.get('buscar') or '').strip()[:100]
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_productos_service.listar(
            cur, buscar=buscar, page=page, per_page=per_page)
        data = []
        for fila in filas:
            galeria = admin_productos_service.imagenes(cur, fila['id'])
            extras = [g.get('imagen') for g in galeria if g.get('imagen')]
            data.append(producto_admin_a_json(fila, extras, galeria))
        alertas = admin_productos_service.alertas(cur)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(data, meta={
            'page': page, 'per_page': per_page,
            'total': total, 'pages': pages, 'buscar': buscar,
            **alertas,
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los productos.') from exc
    finally:
        cur.close()


@bp.post('/admin/productos')
@doc(
    summary='Crear un producto (sin imagen)',
    tags=['Admin'],
    auth=True,
    request='ProductoAdminIn',
    response_model='ProductoAdmin',
    success_status=201,
    responses={201: 'Producto creado',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               422: 'Datos invalidos'},
)
@requiere_admin
def admin_crear_producto():
    datos = validar_modelo(ProductoAdminIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        producto_id = admin_productos_service.crear(cur, datos.model_dump())
        mysql.connection.commit()
        return ok(_producto_admin(cur, producto_id), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo crear el producto.') from exc
    finally:
        cur.close()


@bp.get('/admin/productos/<int:producto_id>')
@doc(
    summary='Detalle de un producto (admin)',
    tags=['Admin'],
    auth=True,
    response_model='ProductoAdmin',
    responses={200: 'Producto', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado'},
)
@requiere_admin
def admin_ver_producto(producto_id):
    mysql, cur = abrir_cursor()
    try:
        return ok(_producto_admin(cur, producto_id))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el producto.') from exc
    finally:
        cur.close()


@bp.patch('/admin/productos/<int:producto_id>')
@doc(
    summary='Actualizar un producto',
    tags=['Admin'],
    auth=True,
    request='ProductoAdminPatchIn',
    response_model='ProductoAdmin',
    responses={200: 'Producto actualizado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado', 422: 'Datos invalidos'},
)
@requiere_admin
def admin_actualizar_producto(producto_id):
    datos = validar_modelo(ProductoAdminPatchIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_productos_service.actualizar(
            cur, producto_id, datos.model_dump(exclude_unset=True))
        mysql.connection.commit()
        return ok(_producto_admin(cur, producto_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar el producto.') from exc
    finally:
        cur.close()


@bp.post('/admin/productos/<int:producto_id>/imagen')
@doc(
    summary='Subir imagenes de un producto',
    tags=['Admin'],
    auth=True,
    request_multipart=[
        {'name': 'imagenes', 'type': 'file', 'array': True, 'required': True,
         'description': 'Una o mas imagenes (JPG, PNG, GIF, WEBP o AVIF).'},
        {'name': 'principal', 'type': 'boolean',
         'description': 'Si es true, la primera imagen reemplaza la principal.'},
    ],
    response_model='ProductoImagenSubida',
    responses={200: 'Imagenes guardadas', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado',
               409: 'Se supera el maximo de imagenes',
               422: 'Archivo o imagen invalida'},
)
@requiere_admin
def admin_subir_imagen(producto_id):
    archivos = [f for f in request.files.getlist('imagenes') if f and f.filename]
    if not archivos:
        unico = request.files.get('imagen')
        if unico and unico.filename:
            archivos = [unico]
    principal = (request.form.get('principal') or '').strip().lower() in (
        '1', 'true', 'si', 'sí', 'yes')

    mysql, cur = abrir_cursor()
    try:
        admin_productos_service.agregar_imagenes(
            cur, producto_id, archivos, principal=principal)
        mysql.connection.commit()
        return ok(_producto_admin(cur, producto_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudieron guardar las imagenes.') from exc
    finally:
        cur.close()


@bp.delete('/admin/productos/<int:producto_id>/imagenes/<int:imagen_id>')
@doc(
    summary='Eliminar una imagen de la galeria',
    tags=['Admin'],
    auth=True,
    response_model='ProductoImagenSubida',
    responses={200: 'Galeria actualizada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado'},
)
@requiere_admin
def admin_eliminar_imagen(producto_id, imagen_id):
    mysql, cur = abrir_cursor()
    try:
        if not admin_productos_service.obtener(cur, producto_id):
            raise ApiError('PRODUCTO_NO_ENCONTRADO', _PRODUCTO_NO_ENCONTRADO, 404)
        admin_productos_service.eliminar_imagen(cur, producto_id, imagen_id)
        mysql.connection.commit()
        return ok(_producto_admin(cur, producto_id))
    except ApiError:
        _rollback(mysql)
        raise
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo eliminar la imagen.') from exc
    finally:
        cur.close()


@bp.delete('/admin/productos/<int:producto_id>/imagen')
@doc(
    summary='Quitar la imagen principal',
    tags=['Admin'],
    auth=True,
    response_model='ProductoImagenSubida',
    responses={200: 'Imagen principal quitada (promueve la primera de la galeria)',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado'},
)
@requiere_admin
def admin_quitar_principal(producto_id):
    mysql, cur = abrir_cursor()
    try:
        admin_productos_service.quitar_principal(cur, producto_id)
        mysql.connection.commit()
        return ok(_producto_admin(cur, producto_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo quitar la imagen principal.') from exc
    finally:
        cur.close()


@bp.post('/admin/productos/<int:producto_id>/activar')
@doc(
    summary='Activar un producto',
    tags=['Admin'],
    auth=True,
    response_model='ProductoAdmin',
    responses={200: 'Producto activado', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado'},
)
@requiere_admin
def admin_activar_producto(producto_id):
    mysql, cur = abrir_cursor()
    try:
        admin_productos_service.cambiar_estado(cur, producto_id, 'activo')
        mysql.connection.commit()
        return ok(_producto_admin(cur, producto_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo activar el producto.') from exc
    finally:
        cur.close()


@bp.delete('/admin/productos/<int:producto_id>')
@doc(
    summary='Desactivar (o eliminar) un producto',
    tags=['Admin'],
    auth=True,
    response_model='ProductoEliminado',
    parameters=[
        {'name': 'definitivo', 'in': 'query', 'required': False,
         'schema': {'type': 'boolean', 'default': False},
         'description': 'Si es true borra el producto y su galeria.'},
    ],
    responses={200: 'Producto desactivado o eliminado',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Producto no encontrado'},
)
@requiere_admin
def admin_eliminar_producto(producto_id):
    definitivo = (request.args.get('definitivo') or '').strip().lower() in (
        '1', 'true', 'si', 'sí', 'yes')
    mysql, cur = abrir_cursor()
    try:
        if definitivo:
            admin_productos_service.eliminar_definitivo(cur, producto_id)
            mysql.connection.commit()
            return ok({'id': int(producto_id), 'eliminado': True,
                       'definitivo': True, 'estado': None})
        admin_productos_service.cambiar_estado(cur, producto_id, 'inactivo')
        mysql.connection.commit()
        return ok({'id': int(producto_id), 'eliminado': False,
                   'definitivo': False, 'estado': 'inactivo'})
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo eliminar el producto.') from exc
    finally:
        cur.close()
