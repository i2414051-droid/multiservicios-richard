"""Endpoints publicos de catalogo y configuracion de la tienda."""
from flask import request

from services import tienda_service

from ..deps import abrir_cursor, error_bd, paginacion
from ..errors import ApiError, ok
from ..openapi import doc
from ..serializers import config_tienda_a_json, producto_a_json
from . import bp

_PARAMS_PRODUCTOS = [
    {'name': 'buscar', 'in': 'query', 'required': False,
     'schema': {'type': 'string'}, 'description': 'Texto a buscar en el nombre.'},
    {'name': 'categoria', 'in': 'query', 'required': False,
     'schema': {'type': 'string'}, 'description': 'Categoria exacta o Todos.'},
    {'name': 'page', 'in': 'query', 'required': False,
     'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
    {'name': 'per_page', 'in': 'query', 'required': False,
     'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                'default': 20}},
    {'name': 'orden', 'in': 'query', 'required': False,
     'schema': {'type': 'string', 'default': 'nuevo',
                'enum': list(tienda_service.ORDENES)}},
    {'name': 'solo_disponibles', 'in': 'query', 'required': False,
     'schema': {'type': 'boolean', 'default': False},
     'description': 'Si es true, oculta productos con stock 0.'},
]


def _verdadero(valor):
    return (valor or '').strip().lower() in ('1', 'true', 'si', 'sí', 'yes')


@bp.get('/categorias')
@doc(
    summary='Listar categorias',
    tags=['Catalogo'],
    response_model='CategoriaLista',
    responses={200: 'Categorias con conteo de productos'},
)
def categorias():
    mysql, cur = abrir_cursor()
    try:
        filas = tienda_service.categorias_con_conteo(cur)
        return ok(filas, meta={'total': len(filas)})
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar las categorias.') from exc
    finally:
        cur.close()


@bp.get('/productos')
@doc(
    summary='Listar productos',
    tags=['Catalogo'],
    parameters=_PARAMS_PRODUCTOS,
    response_model='ProductoLista',
    responses={200: 'Productos activos paginados', 422: 'Orden invalido'},
)
def productos():
    buscar = (request.args.get('buscar') or '').strip()[:100]
    categoria = (request.args.get('categoria') or 'Todos').strip()[:100]
    orden = (request.args.get('orden') or 'nuevo').strip().lower()
    if orden not in tienda_service.ORDENES:
        raise ApiError(
            'ORDEN_INVALIDO',
            'El orden debe ser uno de: %s.' % ', '.join(tienda_service.ORDENES),
            422,
        )
    solo_disponibles = _verdadero(request.args.get('solo_disponibles'))
    page, per_page = paginacion(20, 100)

    mysql, cur = abrir_cursor()
    try:
        filas, total = tienda_service.listar_productos(
            cur,
            buscar=buscar,
            categoria=categoria,
            solo_disponibles=solo_disponibles,
            orden=orden,
            page=page,
            per_page=per_page,
        )
        extras = tienda_service.imagenes_extra(
            cur, [fila['id'] for fila in filas])
        data = [
            producto_a_json(fila, extras.get(fila['id'], []))
            for fila in filas
        ]
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(data, meta={
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': pages,
            'orden': orden,
            'buscar': buscar,
            'categoria': categoria,
            'solo_disponibles': solo_disponibles,
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el catalogo.') from exc
    finally:
        cur.close()


@bp.get('/productos/<int:producto_id>')
@doc(
    summary='Ficha de un producto',
    tags=['Catalogo'],
    response_model='Producto',
    responses={200: 'Producto activo', 404: 'Producto no encontrado'},
)
def producto_detalle(producto_id):
    mysql, cur = abrir_cursor()
    try:
        producto = tienda_service.obtener_producto(
            cur, producto_id, solo_activo=True)
        if not producto:
            raise ApiError('PRODUCTO_NO_ENCONTRADO',
                           'El producto no existe o no esta disponible.', 404)
        extras = tienda_service.imagenes_extra(
            cur, [producto_id]).get(producto_id, [])
        return ok(producto_a_json(producto, extras))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el producto.') from exc
    finally:
        cur.close()


@bp.get('/productos/<int:producto_id>/imagenes')
@doc(
    summary='Imagenes de un producto',
    tags=['Catalogo'],
    response_model='ImagenesProducto',
    responses={200: 'Galeria del producto', 404: 'Producto no encontrado'},
)
def producto_imagenes(producto_id):
    mysql, cur = abrir_cursor()
    try:
        producto = tienda_service.obtener_producto(
            cur, producto_id, solo_activo=True)
        if not producto:
            raise ApiError('PRODUCTO_NO_ENCONTRADO',
                           'El producto no existe o no esta disponible.', 404)
        extras = tienda_service.imagenes_extra(
            cur, [producto_id]).get(producto_id, [])
        datos = producto_a_json(producto, extras)
        return ok({
            'producto_id': datos['id'],
            'imagen': datos['imagen'],
            'imagen_original': datos['imagen_original'],
            'imagenes': datos['imagenes'],
            'imagenes_thumb': datos['imagenes_thumb'],
        })
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar las imagenes.') from exc
    finally:
        cur.close()


@bp.get('/config/tienda')
@doc(
    summary='Configuracion publica de la tienda',
    tags=['Catalogo'],
    response_model='ConfigTienda',
    responses={200: 'Datos de contacto y reglas de envio'},
)
def config_tienda():
    mysql, cur = abrir_cursor()
    try:
        ajustes = tienda_service.leer_ajustes(cur)
        return ok(config_tienda_a_json(ajustes))
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la configuracion.') from exc
    finally:
        cur.close()
