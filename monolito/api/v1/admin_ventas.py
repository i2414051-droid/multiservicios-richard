"""Admin — ventas (API v1, Fase 4).

Listado con filtros, detalle, cambio de estado, visibilidad, limpieza del
historial y exportacion CSV/PDF. Reproduce la logica de `/historial-compras`.
"""
import csv
from io import BytesIO, StringIO

from flask import Response, request
from pydantic import BaseModel

from services import admin_ventas_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_admin
from ..serializers import centimos, dinero, venta_admin_a_json
from . import bp

_FILTROS = [
    {'name': 'buscar', 'in': 'query', 'schema': {'type': 'string'},
     'description': 'Filtra por correo del cliente.'},
    {'name': 'motivo', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'estado', 'in': 'query',
     'schema': {'type': 'string',
                'enum': ['en espera', 'espera', 'entregado', 'cancelado']}},
    {'name': 'pago', 'in': 'query',
     'schema': {'type': 'string',
                'enum': ['tarjeta', 'yape', 'transferencia', 'efectivo',
                         'plin', 'sin_dato']}},
    {'name': 'entrega', 'in': 'query',
     'schema': {'type': 'string', 'enum': ['recojo', 'delivery']}},
    {'name': 'distrito', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'categoria', 'in': 'query', 'schema': {'type': 'string'}},
    {'name': 'ocultas', 'in': 'query',
     'schema': {'type': 'boolean', 'default': False}},
]


class VentaEstadoIn(BaseModel):
    estado: str


class VentaVisibilidadIn(BaseModel):
    oculta: bool


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


def _venta_admin(cur, venta_id):
    venta, items = admin_ventas_service.obtener(cur, venta_id)
    if not venta:
        raise ApiError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)
    return venta_admin_a_json(venta, items)


def _fecha_texto(valor):
    return valor.strftime('%Y-%m-%d %H:%M') if hasattr(valor, 'strftime') \
        else (str(valor) if valor else '')


@bp.get('/admin/ventas')
@doc(
    summary='Listar ventas (admin)',
    tags=['Admin'],
    auth=True,
    response_model='VentaAdminLista',
    parameters=_FILTROS + [
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                    'default': 20}},
    ],
    responses={200: 'Ventas filtradas y paginadas', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_listar_ventas():
    filtros = admin_ventas_service.normalizar(request.args)
    page, per_page = paginacion(20, 100)
    mysql, cur = abrir_cursor()
    try:
        filas, total = admin_ventas_service.listar(
            cur, filtros, page=page, per_page=per_page)
        totales = admin_ventas_service.totales(cur)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(
            [venta_admin_a_json(v, v.get('productos')) for v in filas],
            meta={
                'page': page, 'per_page': per_page,
                'total': total, 'pages': pages,
                'filtros': filtros,
                'gran_total': dinero(totales['gran_total']),
                'gran_total_centimos': centimos(totales['gran_total']),
                'monto_en_espera': dinero(totales['monto_en_espera']),
                'monto_en_espera_centimos': centimos(totales['monto_en_espera']),
                'ocultas': totales['ocultas'],
            },
        )
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar las ventas.') from exc
    finally:
        cur.close()


@bp.get('/admin/ventas/<int:venta_id>')
@doc(
    summary='Detalle de una venta (admin)',
    tags=['Admin'],
    auth=True,
    response_model='VentaAdmin',
    responses={200: 'Venta con items', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Venta no encontrada'},
)
@requiere_admin
def admin_ver_venta(venta_id):
    mysql, cur = abrir_cursor()
    try:
        return ok(_venta_admin(cur, venta_id))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la venta.') from exc
    finally:
        cur.close()


@bp.patch('/admin/ventas/<int:venta_id>/estado')
@doc(
    summary='Cambiar el estado de una venta',
    tags=['Admin'],
    auth=True,
    request='VentaEstadoIn',
    response_model='VentaAdmin',
    responses={200: 'Estado actualizado (cancelar devuelve stock)',
               401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Venta no encontrada',
               409: 'La venta ya esta cancelada',
               422: 'Estado invalido'},
)
@requiere_admin
def admin_estado_venta(venta_id):
    datos = validar_modelo(VentaEstadoIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_ventas_service.actualizar_estado(cur, venta_id, datos.estado)
        mysql.connection.commit()
        return ok(_venta_admin(cur, venta_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo cambiar el estado de la venta.') from exc
    finally:
        cur.close()


@bp.patch('/admin/ventas/<int:venta_id>/visibilidad')
@doc(
    summary='Ocultar o mostrar una venta',
    tags=['Admin'],
    auth=True,
    request='VentaVisibilidadIn',
    response_model='VentaAdmin',
    responses={200: 'Visibilidad actualizada', 401: 'Token invalido',
               403: 'Se requiere rol administrador',
               404: 'Venta no encontrada'},
)
@requiere_admin
def admin_visibilidad_venta(venta_id):
    datos = validar_modelo(VentaVisibilidadIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        admin_ventas_service.cambiar_visibilidad(cur, venta_id, datos.oculta)
        mysql.connection.commit()
        return ok(_venta_admin(cur, venta_id))
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo cambiar la visibilidad.') from exc
    finally:
        cur.close()


@bp.delete('/admin/ventas/ocultas')
@doc(
    summary='Borrar todo el historial de ventas',
    tags=['Admin'],
    auth=True,
    response_model='VentasLimpiadas',
    responses={200: 'Historial eliminado', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_limpiar_ventas():
    mysql, cur = abrir_cursor()
    try:
        borrados = admin_ventas_service.limpiar(cur)
        mysql.connection.commit()
        return ok({'eliminadas': borrados})
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo limpiar el historial.') from exc
    finally:
        cur.close()


def _texto_csv(filas):
    buffer = StringIO()
    escritor = csv.writer(buffer, delimiter=';')
    escritor.writerow(['ID', 'Fecha', 'Cliente', 'Documento', 'Total', 'Estado',
                       'Estado pago', 'Metodo pago', 'Entrega', 'Distrito'])
    for fila in filas:
        escritor.writerow([
            fila.get('id'), _fecha_texto(fila.get('fecha')),
            fila.get('correo') or '', fila.get('documento') or '',
            dinero(fila.get('total')), fila.get('estado') or '',
            fila.get('estado_pago') or '', fila.get('metodo_pago') or '',
            fila.get('tipo_entrega') or '', fila.get('distrito') or '',
        ])
    return buffer.getvalue()


def _pdf_ventas(filas):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    buffer = BytesIO()
    documento = SimpleDocTemplate(buffer, pagesize=A4)
    estilos = getSampleStyleSheet()
    elementos = [
        Paragraph('Historial de ventas', estilos['Title']),
        Paragraph('Multiservicios Richard', estilos['Normal']),
        Spacer(1, 0.4 * cm),
    ]
    datos = [['#', 'Fecha', 'Cliente', 'Documento', 'Total', 'Estado']]
    for fila in filas:
        datos.append([
            str(fila.get('id')), _fecha_texto(fila.get('fecha')),
            fila.get('correo') or '', fila.get('documento') or '',
            'S/ ' + dinero(fila.get('total')), fila.get('estado') or '',
        ])
    tabla = Table(datos, repeatRows=1)
    tabla.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#343a40')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),
    ]))
    elementos.append(tabla)
    documento.build(elementos)
    return buffer.getvalue()


@bp.get('/admin/ventas/export.csv')
@doc(
    summary='Exportar ventas a CSV',
    tags=['Admin'],
    auth=True,
    parameters=_FILTROS,
    responses={200: 'Archivo CSV (text/csv)', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_exportar_ventas_csv():
    filtros = admin_ventas_service.normalizar(request.args)
    mysql, cur = abrir_cursor()
    try:
        filas = admin_ventas_service.filas_export(cur, filtros)
    except Exception as exc:
        raise error_bd(exc, 'No se pudo exportar el historial.') from exc
    finally:
        cur.close()
    return Response(
        _texto_csv(filas).encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="ventas.csv"'},
    )


@bp.get('/admin/ventas/export.pdf')
@doc(
    summary='Exportar ventas a PDF (binario)',
    tags=['Admin'],
    auth=True,
    parameters=_FILTROS,
    responses={200: 'application/pdf', 401: 'Token invalido',
               403: 'Se requiere rol administrador'},
)
@requiere_admin
def admin_exportar_ventas_pdf():
    filtros = admin_ventas_service.normalizar(request.args)
    mysql, cur = abrir_cursor()
    try:
        filas = admin_ventas_service.filas_export(cur, filtros)
    except Exception as exc:
        raise error_bd(exc, 'No se pudo exportar el historial.') from exc
    finally:
        cur.close()
    return Response(
        _pdf_ventas(filas),
        mimetype='application/pdf',
        headers={'Content-Disposition': 'attachment; filename="ventas.pdf"'},
    )
