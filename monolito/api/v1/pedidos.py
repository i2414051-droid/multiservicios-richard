"""Pedidos de la API v1: checkout, alta, historial, seguimiento y boletas.

Fase 2 sin cobro real: se aceptan `transferencia` y `contra_entrega`, que
quedan con `estado_pago='pendiente'`. Tarjeta/Yape responden 409
`PAGO_NO_DISPONIBLE` hasta que se habilite el cargo Culqi.
"""
from decimal import Decimal

from flask import Response, g, request
from pydantic import BaseModel, Field

from services import pedidos_service, tienda_service

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import requiere_cliente
from ..serializers import (
    carrito_a_json,
    centimos,
    dinero,
    direccion_a_json,
    item_carrito_a_json,
    pedido_a_json,
)
from . import bp

_ENDPOINT_PEDIDOS = 'POST /api/v1/pedidos'
_ROLES_ADMIN = ('admin', 'administrador', 'superadmin')


class PedidoIn(BaseModel):
    doc_tipo: str = 'dni'
    doc_numero: str
    nombres: str | None = None
    apellido_paterno: str | None = None
    apellido_materno: str | None = None
    razon_social: str | None = None
    telefono: str
    correo: str | None = None
    metodo_pago: str = 'contra_entrega'
    tipo_entrega: str = 'recojo'
    direccion: str | None = None
    distrito: str | None = None
    lat: float | None = None
    lon: float | None = None
    notas: str | None = None


class CancelarIn(BaseModel):
    motivo: str
    detalle: str | None = Field(default=None, max_length=300)


class BoletaIn(BaseModel):
    documento: str
    nombre: str = Field(max_length=200)


def _usuario_id():
    return str(g.usuario['id'])


def _usuario_id_int():
    return int(g.usuario['id'])


def _es_admin():
    return (g.usuario.get('rol') or '').lower() in _ROLES_ADMIN


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


def _pedido(cur, venta_id):
    venta = pedidos_service.venta_por_id(cur, venta_id)
    if not venta:
        return None
    cancelacion = pedidos_service.cancelacion_pedido(cur, venta_id)
    if cancelacion:
        venta = dict(venta)
        venta['motivo_codigo'] = cancelacion.get('motivo_codigo')
        venta['motivo_detalle'] = cancelacion.get('motivo_detalle')
    items = pedidos_service.items_pedido(cur, venta_id)
    seguimiento = pedidos_service.seguimiento_pedido(cur, venta_id)
    return pedido_a_json(venta, items, seguimiento)


def _puede_ver(venta):
    if _es_admin():
        return True
    try:
        return int(venta.get('cliente_id')) == _usuario_id_int()
    except (TypeError, ValueError):
        return False


def _clave_idempotencia():
    clave = (request.headers.get('Idempotency-Key') or '').strip()
    if not pedidos_service.clave_idempotencia_valida(clave):
        raise ApiError(
            'IDEMPOTENCIA_REQUERIDA',
            'Falta el header Idempotency-Key (8 a 120 caracteres).',
            400,
        )
    return clave


# ─────────────────────────────────────────────
# CHECKOUT
# ─────────────────────────────────────────────
@bp.get('/checkout/opciones')
@doc(
    summary='Datos para armar el checkout',
    tags=['Pedidos'],
    auth=True,
    response_model='CheckoutOpciones',
    responses={200: 'Carrito, direcciones, metodos de pago y reglas de envio',
               401: 'Token invalido'},
)
@requiere_cliente
def checkout_opciones():
    mysql, cur = abrir_cursor()
    try:
        usuario_id = _usuario_id()
        ajustes = tienda_service.leer_ajustes(cur)
        carrito = carrito_a_json(
            tienda_service.items_carrito(cur, usuario_id), ajustes)
        cur.execute(
            "SELECT correo, nombre, apellido, telefono, documento_tipo, "
            "documento_numero FROM usuarios WHERE id=%s",
            (_usuario_id_int(),),
        )
        usuario = cur.fetchone() or {}
        direcciones = [
            direccion_a_json(fila)
            for fila in pedidos_service.direcciones_cliente(cur, usuario_id)
        ]
        minimo = tienda_service.minimo_delivery(ajustes)
        total = Decimal(carrito['total'])
        return ok({
            'moneda': 'PEN',
            'carrito': carrito,
            'usuario': {
                'nombre': usuario.get('nombre') or '',
                'apellido': usuario.get('apellido') or '',
                'telefono': usuario.get('telefono') or '',
                'correo': usuario.get('correo') or '',
                'documento_tipo': usuario.get('documento_tipo') or '',
                'documento_numero': usuario.get('documento_numero') or '',
            },
            'direcciones': direcciones,
            'metodos_pago': [
                {'codigo': 'transferencia', 'nombre': 'Transferencia bancaria',
                 'disponible': True},
                {'codigo': 'contra_entrega', 'nombre': 'Pago contra entrega',
                 'disponible': True},
                {'codigo': 'tarjeta', 'nombre': 'Tarjeta',
                 'disponible': False},
                {'codigo': 'yape', 'nombre': 'Yape', 'disponible': False},
            ],
            'entrega': {
                'distritos': list(tienda_service.DISTRITOS_DELIVERY),
                'minimo_delivery': dinero(minimo),
                'minimo_delivery_centimos': centimos(minimo),
                'elegible_delivery': bool(total >= minimo),
                'tienda': {
                    'direccion': ajustes.get('empresa_direccion') or '',
                    'lat': _flotante(ajustes.get('empresa_lat'), -12.0728939),
                    'lon': _flotante(ajustes.get('empresa_lon'), -75.1904488),
                },
            },
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el checkout.') from exc
    finally:
        cur.close()


def _flotante(valor, defecto):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


@bp.post('/checkout/validar')
@doc(
    summary='Validar el checkout antes de confirmar',
    tags=['Pedidos'],
    auth=True,
    request='PedidoIn',
    response_model='CheckoutValidacion',
    responses={200: 'Checkout valido y totales calculados',
               401: 'Token invalido',
               409: 'Carrito vacio, sin stock, delivery no disponible o pago no habilitado',
               422: 'Datos invalidos'},
)
@requiere_cliente
def checkout_validar():
    datos = validar_modelo(PedidoIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        validado = pedidos_service.validar_pedido(
            cur, _usuario_id(), datos.model_dump())
        return ok({
            'valido': True,
            'moneda': 'PEN',
            'items': [item_carrito_a_json(item) for item in validado['items']],
            'subtotal': dinero(validado['subtotal']),
            'subtotal_centimos': centimos(validado['subtotal']),
            'costo_envio': dinero(validado['envio_costo']),
            'costo_envio_centimos': centimos(validado['envio_costo']),
            'total': dinero(validado['total']),
            'total_centimos': centimos(validado['total']),
            'metodo_pago': validado['metodo_pago'],
            'tipo_entrega': validado['tipo_entrega'],
            'distrito': validado['distrito'],
        })
    except tienda_service.TiendaError as exc:
        raise _api_error(exc) from exc
    except Exception as exc:
        raise error_bd(exc, 'No se pudo validar el checkout.') from exc
    finally:
        cur.close()


# ─────────────────────────────────────────────
# PEDIDOS
# ─────────────────────────────────────────────
@bp.post('/pedidos')
@doc(
    summary='Crear un pedido',
    tags=['Pedidos'],
    auth=True,
    request='PedidoIn',
    response_model='Pedido',
    success_status=201,
    responses={201: 'Pedido creado',
               400: 'Falta Idempotency-Key',
               401: 'Token invalido',
               409: 'Carrito vacio, stock, delivery, pago o idempotencia',
               422: 'Datos invalidos'},
)
@requiere_cliente
def crear_pedido():
    datos = validar_modelo(PedidoIn, cuerpo_json())
    cuerpo = datos.model_dump()
    clave = _clave_idempotencia()
    pedido_hash = pedidos_service.hash_peticion(cuerpo)
    usuario_id = _usuario_id()

    mysql, cur = abrir_cursor()
    try:
        repetida = pedidos_service.reclamar_idempotencia(
            cur, usuario_id, clave, _ENDPOINT_PEDIDOS, pedido_hash)
        if repetida:
            return ok(repetida['data'], status=repetida.get('status', 200))

        validado = pedidos_service.validar_pedido(cur, usuario_id, cuerpo)
        resultado = pedidos_service.crear_pedido(cur, usuario_id, validado)

        venta = pedidos_service.venta_por_id(cur, resultado['venta_id'])
        items = pedidos_service.items_pedido(cur, resultado['venta_id'])
        seguimiento = pedidos_service.seguimiento_pedido(cur, resultado['venta_id'])
        data = pedido_a_json(venta, items, seguimiento)
        pedidos_service.marcar_idempotencia(cur, usuario_id, clave, 201, data)
        mysql.connection.commit()
    except tienda_service.TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo registrar el pedido.') from exc
    finally:
        cur.close()

    pedidos_service.enviar_boleta(validado['correo'], resultado['venta_id'])
    return ok(data, status=201)


@bp.get('/pedidos')
@doc(
    summary='Listar mis pedidos',
    tags=['Pedidos'],
    auth=True,
    response_model='PedidoLista',
    parameters=[
        {'name': 'page', 'in': 'query', 'schema': {'type': 'integer',
                                                   'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                    'default': 10}},
    ],
    responses={200: 'Pedidos del usuario', 401: 'Token invalido'},
)
@requiere_cliente
def listar_pedidos():
    page, per_page = paginacion(10, 50)
    mysql, cur = abrir_cursor()
    try:
        filas, total = pedidos_service.listar_pedidos(
            cur, _usuario_id(), page, per_page)
        data = []
        for venta in filas:
            items = pedidos_service.items_pedido(cur, venta['id'])
            seguimiento = pedidos_service.seguimiento_pedido(cur, venta['id'])
            data.append(pedido_a_json(venta, items, seguimiento))
        pages = (total + per_page - 1) // per_page if total else 0
        return ok(data, meta={
            'page': page, 'per_page': per_page,
            'total': total, 'pages': pages,
        })
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los pedidos.') from exc
    finally:
        cur.close()


@bp.get('/pedidos/<int:venta_id>')
@doc(
    summary='Detalle de un pedido',
    tags=['Pedidos'],
    auth=True,
    response_model='Pedido',
    responses={200: 'Pedido con items y seguimiento',
               401: 'Token invalido',
               404: 'Pedido no encontrado'},
)
@requiere_cliente
def detalle_pedido(venta_id):
    mysql, cur = abrir_cursor()
    try:
        venta = pedidos_service.venta_por_id(cur, venta_id)
        if not venta or not _puede_ver(venta):
            raise ApiError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)
        return ok(_pedido(cur, venta_id))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el pedido.') from exc
    finally:
        cur.close()


@bp.post('/pedidos/<int:venta_id>/cancelar')
@doc(
    summary='Cancelar un pedido',
    tags=['Pedidos'],
    auth=True,
    request='CancelarIn',
    response_model='Pedido',
    responses={200: 'Pedido cancelado y stock devuelto',
               401: 'Token invalido',
               404: 'Pedido no encontrado',
               409: 'Ya cancelado o entregado',
               422: 'Motivo invalido'},
)
@requiere_cliente
def cancelar_pedido(venta_id):
    datos = validar_modelo(CancelarIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        pedidos_service.cancelar_pedido(
            cur, _usuario_id(), venta_id, datos.motivo, datos.detalle,
            es_admin=_es_admin())
        mysql.connection.commit()
        return ok(_pedido(cur, venta_id))
    except tienda_service.TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo cancelar el pedido.') from exc
    finally:
        cur.close()


@bp.get('/pedidos/<int:venta_id>/seguimiento')
@doc(
    summary='Seguimiento de un pedido',
    tags=['Pedidos'],
    auth=True,
    response_model='SeguimientoPedido',
    responses={200: 'Estado y datos de entrega',
               401: 'Token invalido',
               404: 'Pedido no encontrado'},
)
@requiere_cliente
def seguimiento_pedido(venta_id):
    from ..serializers import seguimiento_a_json

    mysql, cur = abrir_cursor()
    try:
        venta = pedidos_service.venta_por_id(cur, venta_id)
        if not venta or not _puede_ver(venta):
            raise ApiError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)
        seguimiento = pedidos_service.seguimiento_pedido(cur, venta_id)
        if not seguimiento:
            raise ApiError('SEGUIMIENTO_NO_ENCONTRADO',
                           'El pedido aun no tiene seguimiento.', 404)
        return ok(seguimiento_a_json(seguimiento))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar el seguimiento.') from exc
    finally:
        cur.close()


# ─────────────────────────────────────────────
# BOLETA
# ─────────────────────────────────────────────
@bp.get('/pedidos/<int:venta_id>/boleta')
@doc(
    summary='Boleta en JSON',
    tags=['Pedidos'],
    auth=True,
    response_model='Pedido',
    responses={200: 'Datos de la boleta', 401: 'Token invalido',
               404: 'Pedido no encontrado'},
)
@requiere_cliente
def boleta_json(venta_id):
    mysql, cur = abrir_cursor()
    try:
        venta = pedidos_service.venta_por_id(cur, venta_id)
        if not venta or not _puede_ver(venta):
            raise ApiError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)
        return ok(_pedido(cur, venta_id))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la boleta.') from exc
    finally:
        cur.close()


@bp.get('/pedidos/<int:venta_id>/boleta.pdf')
@doc(
    summary='Boleta en PDF (binario)',
    tags=['Pedidos'],
    auth=True,
    responses={200: 'application/pdf', 401: 'Token invalido',
               404: 'Pedido no encontrado'},
)
@requiere_cliente
def boleta_pdf(venta_id):
    mysql, cur = abrir_cursor()
    try:
        venta = pedidos_service.venta_por_id(cur, venta_id)
        if not venta or not _puede_ver(venta):
            raise ApiError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)
    finally:
        cur.close()

    from app import generar_boleta_pdf
    pdf_bytes, _ = generar_boleta_pdf(venta_id)
    if not pdf_bytes:
        raise ApiError('BOLETA_NO_DISPONIBLE', 'No se pudo generar la boleta.', 404)
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename="boleta_{venta_id}.pdf"',
        },
    )


@bp.post('/pedidos/<int:venta_id>/boleta')
@doc(
    summary='Actualizar los datos de la boleta',
    tags=['Pedidos'],
    auth=True,
    request='BoletaIn',
    response_model='Pedido',
    responses={200: 'Boleta actualizada', 401: 'Token invalido',
               404: 'Pedido no encontrado', 422: 'Documento invalido'},
)
@requiere_cliente
def guardar_boleta(venta_id):
    datos = validar_modelo(BoletaIn, cuerpo_json())
    documento = datos.documento.strip()
    nombre = datos.nombre.strip()
    if not documento.isdigit() or len(documento) not in (8, 11):
        raise ApiError('DOC_INVALIDO',
                       'La boleta requiere un DNI (8) o RUC (11) numerico.', 422)
    if len(nombre) < 3:
        raise ApiError('NOMBRE_INVALIDO',
                       'El nombre de la boleta es demasiado corto.', 422)

    mysql, cur = abrir_cursor()
    try:
        venta = pedidos_service.venta_por_id(cur, venta_id)
        if not venta or not _puede_ver(venta):
            raise ApiError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)
        cur.execute("UPDATE ventas SET documento=%s, nombre=%s WHERE id=%s",
                    (documento, nombre, venta_id))
        mysql.connection.commit()
        return ok(_pedido(cur, venta_id))
    except ApiError:
        _rollback(mysql)
        raise
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo actualizar la boleta.') from exc
    finally:
        cur.close()
