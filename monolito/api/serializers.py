"""Serializadores de la API v1.

Convierten filas de MySQL (DictCursor) en JSON estable. Reglas:
  * dinero como string con dos decimales (nunca float),
  * fechas ISO 8601,
  * nunca devolver `password`, `imagen_datos` ni tokens internos.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import url_for

from services import reclamos_service, tienda_service

ESTADOS_CLIENTE = {
    'activo': {'nombre': 'Activo', 'color': '#28a745'},
    'inactivo': {'nombre': 'Inactivo', 'color': '#6c757d'},
    'bloqueado': {'nombre': 'Bloqueado', 'color': '#dc3545'},
    'pendiente_verificacion': {'nombre': 'Pendiente verificacion', 'color': '#ffc107'},
    'moroso': {'nombre': 'Moroso', 'color': '#fd7e14'},
}


def iso(valor):
    """datetime/date -> ISO 8601; None -> None."""
    if valor is None:
        return None
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    return str(valor)


def dinero(valor):
    """Decimal/float -> string '12.50'."""
    if valor is None:
        return '0.00'
    return str(Decimal(str(valor)).quantize(Decimal('0.01')))


def entero(valor, por_defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto


def centimos(valor):
    """Decimal/float/str -> centimos enteros, sin perder precision."""
    try:
        return int((Decimal(str(valor if valor is not None else 0)) * 100)
                   .quantize(Decimal('1')))
    except (InvalidOperation, ValueError):
        return 0


def usuario_a_json(usuario):
    return {
        'id': entero(usuario.get('id')),
        'correo': usuario.get('correo') or '',
        'rol': (usuario.get('rol') or 'cliente').lower(),
        'nombre': usuario.get('nombre') or '',
        'apellido': usuario.get('apellido') or '',
        'telefono': usuario.get('telefono') or '',
        'documento_tipo': usuario.get('documento_tipo') or '',
        'documento_numero': usuario.get('documento_numero') or '',
        'estado': usuario.get('estado') or 'activo',
        'created_at': iso(usuario.get('created_at')),
    }


def estado_cliente_a_json(estado):
    estado = estado or 'activo'
    datos = ESTADOS_CLIENTE.get(estado, {'nombre': estado, 'color': '#6c757d'})
    return {'estado': estado, 'nombre': datos['nombre'], 'color': datos['color']}


def tokens_a_json(access_token, refresh_token, expires_in, usuario):
    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'token_type': 'Bearer',
        'expires_in': int(expires_in),
        'usuario': usuario_a_json(usuario),
    }


# ─────────────────────────────────────────────
# CATALOGO
# ─────────────────────────────────────────────
def _thumb(ruta):
    """Misma regla que la web: solo imagenes/ tiene carpeta thumbs/."""
    if isinstance(ruta, str) and ruta.startswith('imagenes/'):
        return 'imagenes/thumbs/' + ruta[len('imagenes/'):]
    return ruta


def _url_imagen(ruta, thumb=False):
    if not ruta:
        return None
    if thumb:
        ruta = _thumb(ruta)
    return url_for('static', filename=ruta, _external=True)


def producto_a_json(producto, extras=None):
    """Ficha publica de producto. Nunca expone imagen_datos ni mime."""
    extras = [ruta for ruta in (extras or []) if ruta]
    principal = producto.get('imagen')
    galeria = ([principal] if principal else []) + extras
    galeria = galeria[:tienda_service.MAX_IMAGENES]
    stock = entero(producto.get('stock'))
    estado = (producto.get('estado') or 'activo').lower()

    return {
        'id': entero(producto.get('id')),
        'nombre': producto.get('nombre') or '',
        'descripcion': producto.get('descripcion') or '',
        'precio': dinero(producto.get('precio')),
        'precio_centimos': centimos(producto.get('precio')),
        'stock': stock,
        'categoria': producto.get('categoria') or '',
        'disponible': estado == 'activo' and stock > 0,
        'imagen': _url_imagen(principal, thumb=True),
        'imagen_original': _url_imagen(principal),
        'imagenes': [_url_imagen(ruta) for ruta in galeria],
        'imagenes_thumb': [_url_imagen(ruta, thumb=True) for ruta in galeria],
        'imagenes_extra': [_url_imagen(ruta) for ruta in extras],
        'created_at': iso(producto.get('created_at')),
    }


# ─────────────────────────────────────────────
# CARRITO
# ─────────────────────────────────────────────
def item_carrito_a_json(fila):
    precio = Decimal(str(fila.get('precio') or 0))
    cantidad = entero(fila.get('cantidad'))
    stock = entero(fila.get('stock'))
    estado = (fila.get('estado') or 'activo').lower()
    subtotal = precio * cantidad
    disponible = estado == 'activo' and stock > 0
    suficiente = disponible and cantidad <= stock

    motivo = None
    if estado != 'activo':
        motivo = 'inactivo'
    elif stock <= 0:
        motivo = 'sin_stock'
    elif cantidad > stock:
        motivo = 'stock_insuficiente'

    return {
        'item_id': entero(fila.get('item_id')),
        'producto_id': entero(fila.get('producto_id')),
        'nombre': fila.get('nombre') or '',
        'categoria': fila.get('categoria') or '',
        'precio': dinero(precio),
        'precio_centimos': centimos(precio),
        'cantidad': cantidad,
        'subtotal': dinero(subtotal),
        'subtotal_centimos': centimos(subtotal),
        'stock': stock,
        'maximo': min(stock, tienda_service.CANTIDAD_MAXIMA) if stock > 0 else 0,
        'disponible': disponible,
        'stock_suficiente': suficiente,
        'puede_comprar': disponible and suficiente,
        'motivo_no_disponible': motivo,
        'imagen': _url_imagen(fila.get('imagen'), thumb=True),
        'imagen_original': _url_imagen(fila.get('imagen')),
        'agregado_en': iso(fila.get('agregado_en')),
    }


def carrito_a_json(filas, ajustes=None):
    ajustes = ajustes or tienda_service.AJUSTES_DEFECTO
    items = [item_carrito_a_json(fila) for fila in filas]

    total = Decimal('0')
    total_disponible = Decimal('0')
    cantidad_items = 0
    for fila in filas:
        precio = Decimal(str(fila.get('precio') or 0))
        cantidad = entero(fila.get('cantidad'))
        stock = entero(fila.get('stock'))
        estado = (fila.get('estado') or 'activo').lower()
        total += precio * cantidad
        cantidad_items += cantidad
        if estado == 'activo' and stock > 0 and cantidad <= stock:
            total_disponible += precio * cantidad

    envio = tienda_service.costo_envio(total, ajustes)
    minimo = tienda_service.minimo_delivery(ajustes)
    gratis_desde = tienda_service.envio_gratis_desde(ajustes)
    gratis = gratis_desde > 0 and total >= gratis_desde
    elegible = total >= minimo
    costo_estimado = envio if elegible else Decimal('0')
    total_con_envio = total + costo_estimado

    return {
        'items': items,
        'cantidad_items': cantidad_items,
        'lineas': len(filas),
        'total': dinero(total),
        'total_centimos': centimos(total),
        'total_disponible': dinero(total_disponible),
        'total_disponible_centimos': centimos(total_disponible),
        'hay_no_disponibles': any(not item['puede_comprar'] for item in items),
        'envio': {
            'costo': dinero(envio),
            'costo_centimos': centimos(envio),
            'gratis_desde': dinero(gratis_desde),
            'gratis_desde_centimos': centimos(gratis_desde),
            'gratis': bool(gratis),
            'minimo_delivery': dinero(minimo),
            'minimo_delivery_centimos': centimos(minimo),
            'elegible_delivery': bool(elegible),
            'costo_estimado': dinero(costo_estimado),
            'costo_estimado_centimos': centimos(costo_estimado),
            'distritos': list(tienda_service.DISTRITOS_DELIVERY),
        },
        'total_con_envio': dinero(total_con_envio),
        'total_con_envio_centimos': centimos(total_con_envio),
        'moneda': 'PEN',
    }


def config_tienda_a_json(ajustes):
    def _flotante(valor, defecto):
        try:
            return float(valor)
        except (TypeError, ValueError):
            return defecto

    envio = tienda_service.envio_delivery(ajustes)
    gratis_desde = tienda_service.envio_gratis_desde(ajustes)
    minimo = tienda_service.minimo_delivery(ajustes)
    return {
        'moneda': 'PEN',
        'empresa': {
            'direccion': ajustes.get('empresa_direccion') or '',
            'lat': _flotante(ajustes.get('empresa_lat'), -12.0728939),
            'lon': _flotante(ajustes.get('empresa_lon'), -75.1904488),
        },
        'envio': {
            'costo': dinero(envio),
            'costo_centimos': centimos(envio),
            'gratis_desde': dinero(gratis_desde),
            'gratis_desde_centimos': centimos(gratis_desde),
            'minimo_delivery': dinero(minimo),
            'minimo_delivery_centimos': centimos(minimo),
            'distritos': list(tienda_service.DISTRITOS_DELIVERY),
        },
        'categorias': list(tienda_service.CATEGORIAS),
    }


# ─────────────────────────────────────────────
# PEDIDOS
# ─────────────────────────────────────────────
def item_pedido_a_json(fila):
    precio = Decimal(str(fila.get('precio') or 0))
    cantidad = entero(fila.get('cantidad'))
    subtotal = precio * cantidad
    return {
        'producto_id': entero(fila.get('producto_id')),
        'nombre': fila.get('nombre') or '',
        'descripcion': fila.get('descripcion') or '',
        'cantidad': cantidad,
        'precio': dinero(precio),
        'precio_centimos': centimos(precio),
        'subtotal': dinero(subtotal),
        'subtotal_centimos': centimos(subtotal),
        'imagen': _url_imagen(fila.get('imagen'), thumb=True),
        'imagen_original': _url_imagen(fila.get('imagen')),
    }


def seguimiento_a_json(fila):
    if not fila:
        return None
    return {
        'estado': fila.get('estado') or '',
        'direccion': fila.get('direccion_envio') or '',
        'distrito': fila.get('distrito') or '',
        'tipo_entrega': fila.get('tipo_entrega') or '',
        'fecha_estimada': iso(fila.get('fecha_estimada')),
        'fecha_entrega': iso(fila.get('fecha_entrega')),
        'notas': fila.get('notas') or '',
        'creado_en': iso(fila.get('created_at')),
        'lat': fila.get('latitud'),
        'lon': fila.get('longitud'),
        'costo_envio': dinero(fila.get('costo_envio')),
        'costo_envio_centimos': centimos(fila.get('costo_envio')),
    }


def pedido_a_json(venta, items=None, seguimiento=None):
    """Venta + detalle + seguimiento con dinero como string y centimos."""
    total = Decimal(str(venta.get('total') or 0))
    envio = Decimal(str(venta.get('costo_envio') or 0))
    subtotal = total - envio
    motivo = venta.get('motivo_codigo')
    cancelacion = None
    if motivo or (venta.get('estado') or '') == 'cancelado':
        cancelacion = {
            'motivo': motivo,
            'detalle': venta.get('motivo_detalle'),
        }

    return {
        'id': entero(venta.get('id')),
        'fecha': iso(venta.get('fecha')),
        'estado': venta.get('estado') or '',
        'estado_pago': venta.get('estado_pago') or 'pendiente',
        'metodo_pago': venta.get('metodo_pago') or '',
        'moneda': 'PEN',
        'subtotal': dinero(subtotal),
        'subtotal_centimos': centimos(subtotal),
        'costo_envio': dinero(envio),
        'costo_envio_centimos': centimos(envio),
        'total': dinero(total),
        'total_centimos': centimos(total),
        'cliente': {
            'nombre': venta.get('nombre') or '',
            'apellido': venta.get('apellido') or '',
            'telefono': venta.get('telefono') or '',
            'correo': venta.get('correo') or '',
        },
        'documento': {
            'tipo': venta.get('doc_tipo') or '',
            'numero': venta.get('documento') or '',
        },
        'entrega': {
            'tipo': venta.get('tipo_entrega') or 'recojo',
            'direccion': venta.get('direccion_envio') or '',
            'distrito': venta.get('distrito') or '',
            'lat': venta.get('latitud'),
            'lon': venta.get('longitud'),
            'notas': venta.get('notas_entrega') or '',
        },
        'items': [item_pedido_a_json(item) for item in (items or [])],
        'seguimiento': seguimiento_a_json(seguimiento),
        'cancelacion': cancelacion,
    }


def direccion_a_json(fila):
    return {
        'id': entero(fila.get('id')),
        'direccion': fila.get('direccion') or '',
        'distrito': fila.get('distrito') or '',
        'referencia': fila.get('referencia') or '',
        'predeterminada': bool(fila.get('predeterminada')),
        'created_at': iso(fila.get('created_at')),
    }


# ─────────────────────────────────────────────
# RECLAMOS (Libro de Reclamaciones)
# ─────────────────────────────────────────────
def reclamo_a_json(fila, incluir_token=True):
    """Hoja de reclamacion. La respuesta solo aparece si ya fue contestada."""
    if not fila:
        return None

    fecha_limite = fila.get('fecha_limite')
    dias = reclamos_service.dias_para_limite(fecha_limite)
    estado = fila.get('estado') or 'pendiente'
    vencido = estado == 'pendiente' and dias is not None and dias < 0
    monto = fila.get('bien_monto')

    datos = {
        'id': entero(fila.get('id')),
        'codigo': fila.get('codigo') or '',
        'tipo': fila.get('tipo') or '',
        'estado': estado,
        'fecha': iso(fila.get('fecha')),
        'fecha_limite': iso(fecha_limite),
        'dias_restantes': dias,
        'vencido': bool(vencido),
        'consumidor': {
            'nombre': fila.get('consumidor_nombre') or '',
            'documento_tipo': fila.get('consumidor_documento_tipo') or '',
            'documento': fila.get('consumidor_documento') or '',
            'domicilio': fila.get('consumidor_domicilio') or '',
            'telefono': fila.get('consumidor_telefono') or '',
            'correo': fila.get('consumidor_correo') or '',
        },
        'bien': {
            'tipo': fila.get('bien_tipo') or '',
            'descripcion': fila.get('bien_descripcion') or '',
            'monto': None if monto is None else dinero(monto),
            'monto_centimos': None if monto is None else centimos(monto),
        },
        'detalle': fila.get('detalle') or '',
        'pedido': fila.get('pedido') or '',
        'respuesta': None,
        'created_at': iso(fila.get('created_at')),
    }

    if fila.get('respuesta'):
        datos['respuesta'] = {
            'texto': fila.get('respuesta'),
            'fecha': iso(fila.get('fecha_respuesta')),
            'respondido_por': fila.get('respondido_por') or '',
        }
    if incluir_token:
        datos['token'] = fila.get('token') or ''
    return datos


# ─────────────────────────────────────────────
# ADMIN (Fase 4)
# ─────────────────────────────────────────────
def imagen_producto_a_json(fila):
    """Fila de `productos_imagenes` (galeria) con id para poder borrarla."""
    return {
        'id': entero(fila.get('id')),
        'url': _url_imagen(fila.get('imagen')),
        'url_thumb': _url_imagen(fila.get('imagen'), thumb=True),
        'orden': entero(fila.get('orden')),
    }


def producto_admin_a_json(producto, extras=None, filas_extra=None):
    """Producto con su estado real (activo/inactivo) y la galeria con ids."""
    datos = producto_a_json(producto, extras)
    datos['estado'] = (producto.get('estado') or 'activo').lower()
    datos['imagenes_detalle'] = [
        imagen_producto_a_json(fila) for fila in (filas_extra or [])
    ]
    return datos


def venta_admin_a_json(venta, items=None):
    """Venta para el panel: cliente, pago, entrega, motivo y detalle."""
    total = Decimal(str(venta.get('total') or 0))
    envio = Decimal(str(venta.get('costo_envio') or 0))
    motivo = venta.get('motivo_codigo') or venta.get('motivo')
    cancelacion = None
    if motivo or (venta.get('estado') or '') == 'cancelado':
        cancelacion = {
            'motivo': motivo,
            'detalle': venta.get('motivo_detalle'),
            'cancelado_por': venta.get('cancelado_por'),
        }

    return {
        'id': entero(venta.get('id')),
        'fecha': iso(venta.get('fecha')),
        'estado': venta.get('estado') or '',
        'estado_pago': venta.get('estado_pago') or 'pendiente',
        'metodo_pago': venta.get('metodo_pago') or '',
        'oculta': bool(venta.get('oculta')),
        'moneda': 'PEN',
        'subtotal': dinero(total - envio),
        'subtotal_centimos': centimos(total - envio),
        'costo_envio': dinero(envio),
        'costo_envio_centimos': centimos(envio),
        'total': dinero(total),
        'total_centimos': centimos(total),
        'cliente': {
            'id': entero(venta.get('cliente_id')),
            'correo': venta.get('correo') or '',
            'nombre': venta.get('titular') or venta.get('nombre') or '',
            'apellido': venta.get('apellido') or '',
            'telefono': venta.get('telefono') or '',
        },
        'documento': {
            'tipo': venta.get('doc_tipo') or '',
            'numero': venta.get('documento') or '',
        },
        'entrega': {
            'tipo': venta.get('tipo_entrega') or 'recojo',
            'direccion': venta.get('direccion_envio') or '',
            'distrito': venta.get('distrito') or '',
        },
        'items': [item_pedido_a_json(item) for item in (items or [])],
        'cancelacion': cancelacion,
    }


def cliente_admin_a_json(fila):
    total = fila.get('total_compras')
    return {
        'id': entero(fila.get('id')),
        'correo': fila.get('correo') or '',
        'nombre': fila.get('nombre') or '',
        'apellido': fila.get('apellido') or '',
        'telefono': fila.get('telefono') or '',
        'rol': (fila.get('rol') or 'cliente').lower(),
        'documento_tipo': fila.get('documento_tipo') or '',
        'documento_numero': fila.get('documento_numero') or '',
        'estado': fila.get('estado') or 'activo',
        'estado_detalle': estado_cliente_a_json(fila.get('estado')),
        'num_compras': entero(fila.get('num_compras')),
        'total_compras': dinero(total),
        'total_compras_centimos': centimos(total),
        'created_at': iso(fila.get('created_at')),
    }


def usuario_permiso_a_json(fila):
    return {
        'id': entero(fila.get('id')),
        'correo': fila.get('correo') or '',
        'rol': (fila.get('rol') or 'cliente').lower(),
        'estado': fila.get('estado') or 'activo',
        'estado_detalle': estado_cliente_a_json(fila.get('estado')),
    }


def historial_estado_a_json(fila):
    return {
        'id': entero(fila.get('id')),
        'anterior': fila.get('estado_anterior'),
        'nuevo': fila.get('estado_nuevo'),
        'motivo': fila.get('motivo') or '',
        'admin': fila.get('admin_correo'),
        'fecha': iso(fila.get('created_at')),
    }


# ─────────────────────────────────────────────
# ADMIN: PROVEEDORES Y PEDIDOS A PROVEEDOR (Fase 5)
# ─────────────────────────────────────────────
def proveedor_a_json(fila):
    return {
        'id': entero(fila.get('id')),
        'nombre': fila.get('nombre') or '',
        'tipo_documento': (fila.get('tipo_documento') or 'DNI').upper(),
        'numero_documento': fila.get('numero_documento') or '',
        'nombres': fila.get('nombres') or '',
        'apellido_paterno': fila.get('apellido_paterno') or '',
        'apellido_materno': fila.get('apellido_materno') or '',
        'celular': fila.get('celular') or '',
        'direccion': fila.get('direccion') or '',
        'categoria': fila.get('categoria') or '',
        'notas': fila.get('notas') or '',
        'created_at': iso(fila.get('created_at')),
    }


def _pedido_compra_base(fila):
    return {
        'id': entero(fila.get('id')),
        'producto_id': entero(fila.get('producto_id')),
        'producto_nombre': fila.get('producto_nombre') or '',
        'stock_actual': entero(fila.get('stock_actual')),
        'categoria': fila.get('categoria') or '',
        'cantidad_pedido': entero(fila.get('cantidad_pedido'), 1),
        'fecha': iso(fila.get('fecha')),
        'estado': fila.get('estado') or 'pendiente',
    }


def pedido_compra_a_json(fila):
    datos = _pedido_compra_base(fila)
    proveedor_id = fila.get('proveedor_asignado')
    datos.update({
        'proveedor': None if not proveedor_id else {
            'id': entero(proveedor_id),
            'nombre': fila.get('proveedor_nombre') or '',
            'celular': fila.get('proveedor_celular') or '',
        },
        'proveedores': [
            {'id': entero(p.get('id')), 'nombre': p.get('nombre') or '',
             'celular': p.get('celular') or '',
             'categoria': p.get('categoria') or ''}
            for p in (fila.get('proveedores') or [])
        ],
        'sin_proveedor': bool(fila.get('sin_proveedor')),
        'colgado': bool(fila.get('colgado')),
    })
    return datos


def grupo_compra_a_json(grupo):
    return {
        'proveedor_id': grupo.get('proveedor_id'),
        'proveedor_nombre': grupo.get('proveedor_nombre') or '',
        'proveedor_celular': grupo.get('proveedor_celular') or '',
        'whatsapp_url': grupo.get('whatsapp_url'),
        'total_productos': entero(grupo.get('total_productos')),
        'productos': [_pedido_compra_base(p)
                      for p in (grupo.get('productos') or [])],
    }


# ─────────────────────────────────────────────
# ADMIN: INGRESOS (Fase 5)
# ─────────────────────────────────────────────
def ingreso_item_a_json(fila):
    precio_compra = Decimal(str(fila.get('precio_compra') or 0))
    precio_venta = Decimal(str(fila.get('precio_venta') or 0))
    cantidad = entero(fila.get('cantidad'))
    return {
        'producto_id': entero(fila.get('producto_id')),
        'producto_nombre': fila.get('producto_nombre') or '',
        'cantidad': cantidad,
        'precio_compra': dinero(precio_compra),
        'precio_compra_centimos': centimos(precio_compra),
        'precio_venta': dinero(precio_venta),
        'precio_venta_centimos': centimos(precio_venta),
        'subtotal_compra': dinero(precio_compra * cantidad),
        'subtotal_compra_centimos': centimos(precio_compra * cantidad),
        'stock_actual': None if fila.get('stock_actual') is None
        else entero(fila.get('stock_actual')),
    }


def ingreso_a_json(fila, items=None, totales=None):
    total_compra = fila.get('total_compra')
    total_venta = fila.get('total_venta')
    total_cantidad = fila.get('total_cantidad')
    if totales:
        total_compra = totales.get('total_compra')
        total_venta = totales.get('total_venta')
        total_cantidad = totales.get('total_cantidad')
    datos = {
        'id': entero(fila.get('id')),
        'fecha': iso(fila.get('fecha')),
        'estado': fila.get('estado') or 'vigente',
        'anulado': (fila.get('estado') or 'vigente') == 'anulado',
        'motivo_anulacion': fila.get('motivo_anulacion'),
        'anulado_en': iso(fila.get('anulado_en')),
        'anulado_por': fila.get('anulado_por'),
        'proveedor': {
            'nombre': fila.get('proveedor_nombre') or '',
            'documento': fila.get('proveedor_documento') or '',
            'categoria': fila.get('proveedor_categoria') or '',
            'celular': fila.get('proveedor_celular') or '',
        },
        'notas': fila.get('notas') or '',
        'productos_distintos': entero(fila.get('productos_distintos'),
                                      len(items or [])),
        'total_cantidad': entero(total_cantidad),
        'total_compra': dinero(total_compra),
        'total_compra_centimos': centimos(total_compra),
        'total_venta': dinero(total_venta),
        'total_venta_centimos': centimos(total_venta),
        'moneda': 'PEN',
    }
    if items is not None:
        datos['items'] = [ingreso_item_a_json(item) for item in items]
    return datos


# ─────────────────────────────────────────────
# ADMIN: SALIDAS (Fase 5)
# ─────────────────────────────────────────────
def salida_a_json(fila, items=None):
    venta_total = fila.get('venta_total')
    valor = fila.get('valor_estimado')
    total_items = fila.get('total_items')
    if items is not None:
        total_items = sum(entero(item.get('cantidad')) for item in items)
    datos = {
        'id': entero(fila.get('id')),
        'fecha': iso(fila.get('fecha')),
        'notas': fila.get('notas') or '',
        'venta_id': fila.get('venta_id'),
        'cliente': {
            'nombre': fila.get('cliente_nombre') or '',
            'documento': fila.get('cliente_doc') or '',
        },
        'venta_total': None if venta_total is None else dinero(venta_total),
        'venta_total_centimos': None if venta_total is None
        else centimos(venta_total),
        'total_items': entero(total_items),
        'valor_estimado': None if valor is None else dinero(valor),
        'valor_estimado_centimos': None if valor is None else centimos(valor),
    }
    if items is not None:
        datos['items'] = [{
            'producto_id': entero(item.get('producto_id')),
            'producto_nombre': item.get('producto_nombre') or '',
            'cantidad': entero(item.get('cantidad')),
            'stock_actual': None if item.get('stock_actual') is None
            else entero(item.get('stock_actual')),
        } for item in items]
    return datos


# ─────────────────────────────────────────────
# ADMIN: ENTREGAS (Fase 5)
# ─────────────────────────────────────────────
def entrega_a_json(fila, productos_venta=None, productos_entrega=None):
    total = fila.get('total')
    motivo = fila.get('motivo_codigo')
    cancelacion = None
    if motivo or (fila.get('estado') or '') == 'cancelado':
        cancelacion = {'motivo': motivo,
                       'detalle': fila.get('motivo_detalle'),
                       'cancelado_por': fila.get('cancelado_por')}
    datos = {
        'id': entero(fila.get('id')),
        'venta_id': fila.get('venta_id'),
        'estado': fila.get('estado') or 'pendiente',
        'direccion_envio': fila.get('direccion_envio') or '',
        'fecha_estimada': iso(fila.get('fecha_estimada')),
        'fecha_entrega': iso(fila.get('fecha_entrega')),
        'notas': fila.get('notas') or '',
        'creado_en': iso(fila.get('created_at')),
        'cliente': {
            'nombre': fila.get('cliente_nombre') or '',
            'documento': fila.get('cliente_doc') or '',
            'correo': fila.get('cliente_correo') or '',
        },
        'venta_total': dinero(total),
        'venta_total_centimos': centimos(total),
        'moneda': 'PEN',
        'cancelacion': cancelacion,
    }
    if productos_venta is not None:
        datos['productos_venta'] = [item_pedido_a_json(p)
                                    for p in productos_venta]
    if productos_entrega is not None:
        datos['productos_entrega'] = [{
            'id': entero(p.get('id')),
            'producto_id': entero(p.get('producto_id')),
            'producto_nombre': p.get('producto_nombre') or '',
            'cantidad': entero(p.get('cantidad'), 1),
            'stock_actual': None if p.get('stock_actual') is None
            else entero(p.get('stock_actual')),
        } for p in productos_entrega]
    return datos


# ─────────────────────────────────────────────
# ADMIN: INVENTARIO (Fase 5)
# ─────────────────────────────────────────────
def inventario_producto_a_json(fila, critico=15, normal=50):
    stock = entero(fila.get('stock'))
    estado = (fila.get('estado') or 'activo').lower()
    if estado != 'activo':
        nivel = 'inactivo'
    elif stock <= 0:
        nivel = 'sin_stock'
    elif stock <= critico:
        nivel = 'critico'
    elif stock < normal:
        nivel = 'bajo'
    else:
        nivel = 'normal'
    return {
        'id': entero(fila.get('id')),
        'nombre': fila.get('nombre') or '',
        'stock': stock,
        'precio': dinero(fila.get('precio')),
        'precio_centimos': centimos(fila.get('precio')),
        'categoria': fila.get('categoria') or '',
        'estado': estado,
        'nivel': nivel,
        'imagen': _url_imagen(fila.get('imagen'), thumb=True),
        'imagen_original': _url_imagen(fila.get('imagen')),
    }
