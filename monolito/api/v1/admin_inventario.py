"""Admin — verificacion de inventario (API v1, Fase 5).

Lista los productos por tramo de stock (sin stock / critico / bajo / normal /
inactivos) con los mismos contadores que `/verificar-inventario`.
"""
from flask import request

from services import admin_inventario_service

from ..deps import abrir_cursor, error_bd, paginacion
from ..errors import ok
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import inventario_producto_a_json
from . import bp


@bp.get('/admin/inventario/verificacion')
@doc(
    summary='Verificar inventario por cantidad (admin)',
    tags=['Admin'],
    auth=True,
    response_model='InventarioProductoLista',
    parameters=[
        {'name': 'filtro', 'in': 'query',
         'schema': {'type': 'string',
                    'enum': ['todos', 'sin_stock', 'critico', 'bajo', 'normal',
                             'inactivos'],
                    'default': 'todos'}},
        {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Productos clasificados por nivel de stock',
               401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_verificar_inventario():
    filtros = admin_inventario_service.normalizar_filtros(request.args)
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        items, total, contadores = admin_inventario_service.listar(
            cur, filtros, page=page, per_page=per_page)
        critico = contadores.get('umbral_critico', 15)
        normal = contadores.get('umbral_normal', 50)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(
            [inventario_producto_a_json(f, critico, normal) for f in items],
            meta={'page': page, 'per_page': per_page, 'total': total,
                  'pages': pages, 'filtros': filtros, **contadores})
    except Exception as exc:
        raise error_bd(exc, 'No se pudo verificar el inventario.') from exc
    finally:
        cur.close()
