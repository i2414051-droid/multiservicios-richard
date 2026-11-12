"""Pedidos de la API v1: checkout, alta, historial, seguimiento y cancelacion.

En esta fase no se cobra con Culqi: solo se aceptan metodos que quedan
`pendiente` de pago (transferencia o contra entrega). La logica de escritura es
la misma que usa `app.procesar_compra`, para que la web y la API no se
desincronicen:

  * la venta se crea y el stock se descuenta con `UPDATE ... WHERE stock>=`,
  * si el stock alcanzo a otro cliente, se aborta todo (rollback),
  * se crea el seguimiento y se vacia el carrito,
  * el alta es idempotente por `Idempotency-Key` (tabla `api_idempotencia`).
"""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from .tienda_service import (
    DISTRITOS_DELIVERY,
    TiendaError,
    minimo_delivery,
    costo_envio,
    leer_ajustes,
)

# Metodos que la API acepta en esta fase (sin cobro real).
METODOS_PAGO_DISPONIBLES = ('transferencia', 'contra_entrega')
METODOS_PAGO_BLOQUEADOS = ('tarjeta', 'yape')

# Estados de venta que todavia se pueden cancelar.
ESTADOS_CANCELABLES = ('en espera', 'pendiente')

_SOLO_LETRAS = r"^[A-ZÁÉÍÓÚÑÜ\s'.-]+$"
_CORREO = r'[^@\s]+@[^@\s]+\.[^@\s]+'
_CLAVE_IDEMPOTENCIA = r'[A-Za-z0-9_.:\-]{8,120}'

# Mismo recuadro que app.DELIVERY_LIMITES (Chilca, El Tambo, Huancayo).
DELIVERY_LIMITES = {'sur': -12.16, 'norte': -11.92,
                    'oeste': -75.33, 'este': -75.08}


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _decimal(valor):
    try:
        return Decimal(str(valor if valor is not None else 0))
    except (InvalidOperation, ValueError):
        return Decimal('0')


def dentro_de_delivery(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return (DELIVERY_LIMITES['sur'] <= lat <= DELIVERY_LIMITES['norte']
            and DELIVERY_LIMITES['oeste'] <= lon <= DELIVERY_LIMITES['este'])


def distrito_desde_punto(lat, lon):
    """Distrito aproximado, igual que app.distrito_desde_punto."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return ''
    if not dentro_de_delivery(lat, lon):
        return ''
    if lat >= -12.055:
        return 'El Tambo'
    if lat <= -12.072:
        return 'Chilca'
    return 'Huancayo'


def hash_peticion(datos):
    """SHA-256 del cuerpo canonico, para detectar reusos de la clave."""
    crudo = json.dumps(datos, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(crudo.encode('utf-8')).hexdigest()


def clave_idempotencia_valida(clave):
    return bool(re.fullmatch(_CLAVE_IDEMPOTENCIA, clave or ''))


# ─────────────────────────────────────────────
# VALIDACION DEL PEDIDO
# ─────────────────────────────────────────────
def _validar_documento(datos, usuario):
    doc_tipo = (datos.get('doc_tipo') or 'dni').strip().lower()
    if doc_tipo not in ('dni', 'ruc'):
        raise TiendaError('DOC_INVALIDO', 'El tipo de documento debe ser dni o ruc.', 422)
    doc_numero = str(datos.get('doc_numero') or '').strip()
    if not doc_numero.isdigit():
        raise TiendaError('DOC_INVALIDO',
                          'El numero de documento solo puede contener digitos.', 422)
    if doc_tipo == 'dni' and len(doc_numero) != 8:
        raise TiendaError('DOC_INVALIDO', 'El DNI debe tener 8 digitos.', 422)
    if doc_tipo == 'ruc' and len(doc_numero) != 11:
        raise TiendaError('DOC_INVALIDO', 'El RUC debe tener 11 digitos.', 422)

    telefono = str(datos.get('telefono') or '').strip()
    if not telefono.isdigit() or len(telefono) != 9:
        raise TiendaError('TELEFONO_INVALIDO',
                          'El celular debe tener 9 digitos.', 422)

    correo = (datos.get('correo') or usuario.get('correo') or '').strip().lower()
    if not re.fullmatch(_CORREO, correo or ''):
        raise TiendaError('CORREO_INVALIDO',
                          'Ingresa un correo electronico valido.', 422)

    if doc_tipo == 'dni':
        nombres = ' '.join(str(datos.get('nombres') or '').upper().split())
        paterno = ' '.join(str(datos.get('apellido_paterno') or '').upper().split())
        materno = ' '.join(str(datos.get('apellido_materno') or '').upper().split())
        if not (nombres and paterno and materno):
            raise TiendaError('COMPLETA_NOMBRES',
                              'Completa nombres y apellidos.', 422)
        for campo, valor in (('nombre', nombres),
                             ('apellido paterno', paterno),
                             ('apellido materno', materno)):
            if re.search(r'\d', valor) or not re.match(_SOLO_LETRAS, valor):
                raise TiendaError('NOMBRE_INVALIDO',
                                  f'El {campo} solo puede contener letras.', 422)
        apellidos = f'{paterno} {materno}'
    else:
        razon = ' '.join(str(datos.get('razon_social') or '').upper().split())
        if len(razon) < 3 or re.search(r'[\x00-\x1f\x7f<>]', razon):
            raise TiendaError('RAZON_SOCIAL_INVALIDA',
                              'Completa una razon social valida.', 422)
        nombres, apellidos = razon, ''

    return {
        'doc_tipo': doc_tipo,
        'documento': doc_numero,
        'nombres': nombres,
        'apellidos': apellidos,
        'telefono': telefono,
        'correo': correo,
    }


def _validar_entrega(datos, subtotal, ajustes):
    tipo_entrega = (datos.get('tipo_entrega') or 'recojo').strip().lower()
    if tipo_entrega not in ('recojo', 'delivery'):
        raise TiendaError('ENTREGA_INVALIDA',
                          'Elige recojo en tienda o delivery.', 422)

    if tipo_entrega == 'recojo':
        return {
            'tipo_entrega': 'recojo',
            'direccion_envio': ajustes.get('empresa_direccion') or '',
            'distrito': 'Chilca',
            'latitud': None,
            'longitud': None,
            'envio_costo': Decimal('0.00'),
        }

    minimo = minimo_delivery(ajustes)
    if _decimal(subtotal) < minimo:
        raise TiendaError(
            'DELIVERY_MINIMO',
            'Para envio la compra debe ser de S/ %s o mas; si es menor, elige '
            'recojo en tienda.' % minimo,
            409,
            {'minimo': str(minimo), 'subtotal': str(_decimal(subtotal))},
        )

    direccion = ' '.join(str(datos.get('direccion') or '').split())
    if len(direccion) < 8:
        raise TiendaError('DIRECCION_INVALIDA',
                          'Indica una direccion de entrega valida.', 422)

    lat_in, lon_in = datos.get('lat'), datos.get('lon')
    latitud = longitud = None
    if lat_in is not None and lon_in is not None:
        try:
            latitud, longitud = float(lat_in), float(lon_in)
        except (TypeError, ValueError):
            raise TiendaError('COORDENADAS_INVALIDAS',
                              'Las coordenadas no son validas.', 422)
        if not dentro_de_delivery(latitud, longitud):
            raise TiendaError('FUERA_DE_COBERTURA',
                              'El punto queda fuera de Chilca, El Tambo o Huancayo.',
                              422)
        distrito = (datos.get('distrito') or '').strip() \
            or distrito_desde_punto(latitud, longitud)
    else:
        distrito = (datos.get('distrito') or '').strip()
        if distrito not in DISTRITOS_DELIVERY:
            raise TiendaError(
                'DISTRITO_INVALIDO',
                'Elige un distrito de cobertura: %s.' % ', '.join(DISTRITOS_DELIVERY),
                422,
            )

    return {
        'tipo_entrega': 'delivery',
        'direccion_envio': direccion,
        'distrito': distrito or 'Huancayo',
        'latitud': latitud,
        'longitud': longitud,
        'envio_costo': costo_envio(subtotal, ajustes),
    }


def validar_pedido(cur, usuario_id, datos, ajustes=None):
    """Valida carrito, stock, pago y entrega. Devuelve el pedido calculado.

    No escribe nada: `crear_pedido` usa este resultado. Se usa igual para
    `POST /pedidos` y `POST /checkout/validar`.
    """
    from . import tienda_service

    metodo_pago = (datos.get('metodo_pago') or 'contra_entrega').strip().lower()
    if metodo_pago in METODOS_PAGO_BLOQUEADOS:
        raise TiendaError(
            'PAGO_NO_DISPONIBLE',
            'El pago con tarjeta o Yape se habilitara en una fase posterior. '
            'Usa transferencia o pago contra entrega.',
            409,
            {'metodos_disponibles': list(METODOS_PAGO_DISPONIBLES)},
        )
    if metodo_pago not in METODOS_PAGO_DISPONIBLES:
        raise TiendaError(
            'METODO_PAGO_INVALIDO',
            'Metodo de pago invalido. Usa: %s.'
            % ', '.join(METODOS_PAGO_DISPONIBLES),
            422,
        )

    usuario = {}
    cur.execute(
        "SELECT correo, nombre, apellido, telefono, documento_tipo, documento_numero "
        "FROM usuarios WHERE id=%s",
        (_entero(usuario_id),),
    )
    fila_usuario = cur.fetchone()
    if fila_usuario:
        usuario = fila_usuario
    documento = _validar_documento(datos, usuario)

    items = tienda_service.items_carrito(cur, usuario_id)
    if not items:
        raise TiendaError('CARRITO_VACIO', 'Tu carrito esta vacio.', 409)

    subtotal = Decimal('0')
    for item in items:
        if item['estado'] != 'activo':
            raise TiendaError('PRODUCTO_NO_DISPONIBLE',
                              f'El producto {item["nombre"]} ya no esta disponible.',
                              409, {'producto_id': item['producto_id']})
        if item['cantidad'] > item['stock']:
            raise TiendaError(
                'STOCK_INSUFICIENTE',
                f'Stock insuficiente para {item["nombre"]}: solo quedan '
                f'{item["stock"]} unidad(es).',
                409,
                {'producto_id': item['producto_id'], 'stock': item['stock'],
                 'en_carrito': item['cantidad']},
            )
        subtotal += _decimal(item['precio']) * item['cantidad']

    if ajustes is None:
        ajustes = leer_ajustes(cur)
    entrega = _validar_entrega(datos, subtotal, ajustes)
    notas = ' '.join(str(datos.get('notas') or '').split())[:300]
    total = subtotal + entrega['envio_costo']

    return {
        'items': items,
        'subtotal': subtotal,
        'envio_costo': entrega['envio_costo'],
        'total': total,
        'metodo_pago': metodo_pago,
        'notas': notas,
        'ajustes': ajustes,
        **documento,
        **entrega,
    }


# ─────────────────────────────────────────────
# ALTA DEL PEDIDO
# ─────────────────────────────────────────────
def crear_pedido(cur, usuario_id, validado):
    """Inserta venta, seguimiento y detalle; descuenta stock y vacia carrito."""
    from .tienda_service import almacen_db

    user_id = _entero(usuario_id)
    total = validado['total']
    envio_costo = validado['envio_costo']

    cur.execute(
        """INSERT INTO ventas
           (fecha, cliente_id, total, metodo_pago, direccion_envio, documento,
            doc_tipo, nombre, apellido, telefono, correo, notas_entrega,
            estado_pago, tipo_entrega, distrito, latitud, longitud, costo_envio,
            culqi_charge_id, culqi_token, culqi_respuesta, pagado_en)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pendiente',%s,%s,%s,%s,%s,
                   NULL,NULL,NULL,NULL)""",
        (_ahora_lima(), user_id, str(total), validado['metodo_pago'],
         validado['direccion_envio'], validado['documento'], validado['doc_tipo'],
         validado['nombres'], validado['apellidos'], validado['telefono'],
         validado['correo'], validado['notas'], validado['tipo_entrega'],
         validado['distrito'], validado['latitud'], validado['longitud'],
         str(envio_costo)),
    )
    venta_id = cur.lastrowid

    cur.execute(
        """INSERT INTO seguimiento_entregas
           (created_at, venta_id, estado, direccion_envio, notas, tipo_entrega,
            distrito, latitud, longitud, costo_envio)
           VALUES (%s, %s, 'pendiente', %s, %s, %s, %s, %s, %s, %s)""",
        (_ahora_lima(), venta_id, validado['direccion_envio'],
         'Seguimiento creado automaticamente por la API.', validado['tipo_entrega'],
         validado['distrito'], validado['latitud'], validado['longitud'],
         str(envio_costo)),
    )

    for item in validado['items']:
        cur.execute(
            "INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio) "
            "VALUES (%s,%s,%s,%s)",
            (venta_id, item['producto_id'], item['cantidad'], str(item['precio'])),
        )
        cur.execute(
            f"UPDATE {almacen_db()}.productos SET stock=stock-%s "
            f"WHERE id=%s AND stock>=%s",
            (item['cantidad'], item['producto_id'], item['cantidad']),
        )
        if cur.rowcount != 1:
            raise TiendaError(
                'STOCK_INSUFICIENTE',
                f'Stock insuficiente para: {item["nombre"]}.',
                409,
                {'producto_id': item['producto_id']},
            )
        _sincronizar_pedido(cur, item['producto_id'])

    cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (str(usuario_id),))
    cur.execute(
        """UPDATE usuarios SET
           nombre=IFNULL(nombre,%s), apellido=IFNULL(apellido,%s),
           telefono=IFNULL(telefono,%s), documento_tipo=IFNULL(documento_tipo,%s),
           documento_numero=IFNULL(documento_numero,%s)
           WHERE id=%s""",
        (validado['nombres'], validado['apellidos'], validado['telefono'],
         validado['doc_tipo'], validado['documento'], user_id),
    )
    return {'venta_id': venta_id, 'total': total, 'envio_costo': envio_costo}


def _ahora_lima():
    from datetime import datetime, timedelta, timezone
    zona_lima = timezone(timedelta(hours=-5))
    return datetime.now(timezone.utc).astimezone(zona_lima).replace(tzinfo=None)


def _sincronizar_pedido(cur, producto_id):
    """Reutiliza la logica de la web sin importarla al cargar el modulo."""
    try:
        from app import sincronizar_pedido_por_stock
        sincronizar_pedido_por_stock(cur, producto_id)
    except Exception:
        pass


def enviar_boleta(correo, venta_id):
    """Envio de boleta best-effort: nunca falla la respuesta del pedido."""
    try:
        from app import enviar_boleta_cliente
        enviar_boleta_cliente(correo, venta_id)
    except Exception:
        pass


# ─────────────────────────────────────────────
# LECTURA
# ─────────────────────────────────────────────
def venta_por_id(cur, venta_id):
    cur.execute("SELECT * FROM ventas WHERE id=%s", (_entero(venta_id),))
    return cur.fetchone()


def items_pedido(cur, venta_id):
    from .tienda_service import almacen_db
    cur.execute(
        f"""SELECT p.id AS producto_id, p.nombre, p.descripcion, p.imagen,
                   d.cantidad, d.precio
            FROM detalle_venta d
            JOIN {almacen_db()}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s ORDER BY d.id""",
        (_entero(venta_id),),
    )
    return cur.fetchall()


def seguimiento_pedido(cur, venta_id):
    cur.execute(
        """SELECT id, estado, direccion_envio, distrito, tipo_entrega,
                  fecha_estimada, fecha_entrega, notas, created_at,
                  latitud, longitud, costo_envio
           FROM seguimiento_entregas WHERE venta_id=%s ORDER BY id DESC LIMIT 1""",
        (_entero(venta_id),),
    )
    return cur.fetchone()


def cancelacion_pedido(cur, venta_id):
    cur.execute(
        "SELECT motivo_codigo, motivo_detalle, cancelado_por, created_at "
        "FROM cancelaciones WHERE venta_id=%s",
        (_entero(venta_id),),
    )
    return cur.fetchone()


def listar_pedidos(cur, usuario_id, page=1, per_page=10):
    user_id = _entero(usuario_id)
    cur.execute(
        "SELECT COUNT(*) AS total FROM ventas "
        "WHERE cliente_id=%s AND COALESCE(oculta,0)=0",
        (user_id,),
    )
    fila = cur.fetchone() or {}
    total = _entero(fila.get('total'))
    offset = max(0, (page - 1) * per_page)
    cur.execute(
        """SELECT v.*, c.motivo_codigo, c.motivo_detalle
           FROM ventas v LEFT JOIN cancelaciones c ON c.venta_id=v.id
           WHERE v.cliente_id=%s AND COALESCE(v.oculta,0)=0
           ORDER BY v.id DESC LIMIT %s OFFSET %s""",
        (user_id, per_page, offset),
    )
    return cur.fetchall(), total


def direcciones_cliente(cur, usuario_id):
    from .direcciones_service import listar as _listar
    return _listar(cur, usuario_id)


# ─────────────────────────────────────────────
# CANCELACION
# ─────────────────────────────────────────────
def cancelar_pedido(cur, usuario_id, venta_id, motivo, detalle, es_admin=False):
    from app import motivo_cancelacion_valido, registrar_cancelacion, restaurar_stock_venta

    venta = venta_por_id(cur, venta_id)
    if not venta:
        raise TiendaError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)
    if not es_admin and _entero(venta.get('cliente_id')) != _entero(usuario_id):
        raise TiendaError('PEDIDO_NO_ENCONTRADO', 'El pedido no existe.', 404)

    estado = (venta.get('estado') or '').strip().lower()
    if estado == 'cancelado':
        raise TiendaError('PEDIDO_YA_CANCELADO', 'Este pedido ya esta cancelado.', 409)
    if estado == 'entregado':
        raise TiendaError('PEDIDO_ENTREGADO',
                          'Ya no se puede cancelar un pedido entregado.', 409)

    motivo = (motivo or '').strip()
    detalle = (detalle or '').strip()[:300]
    if not motivo_cancelacion_valido(motivo):
        raise TiendaError('MOTIVO_INVALIDO', 'Elige un motivo de cancelacion.', 422)
    if motivo == 'otro' and not detalle:
        raise TiendaError('DETALLE_REQUERIDO',
                          'Describe el motivo de la cancelacion.', 422)

    cur.execute("UPDATE ventas SET estado='cancelado' WHERE id=%s", (_entero(venta_id),))
    cur.execute("UPDATE seguimiento_entregas SET estado='cancelado' WHERE venta_id=%s",
                (_entero(venta_id),))
    restaurar_stock_venta(cur, _entero(venta_id))
    registrar_cancelacion(cur, _entero(venta_id), motivo, detalle or None,
                          'admin' if es_admin else 'cliente')
    return {'motivo': motivo, 'detalle': detalle or None}


# ─────────────────────────────────────────────
# IDEMPOTENCIA
# ─────────────────────────────────────────────
def reclamar_idempotencia(cur, usuario_id, clave, endpoint, request_hash):
    """Devuelve la respuesta guardada si la clave ya se uso; None si es nueva.

    Si otra peticion esta en curso con la misma clave, lanza 409 para que el
    cliente reintente en unos segundos.
    """
    user_id = _entero(usuario_id)
    cur.execute(
        "SELECT id, request_hash, respuesta, "
        "TIMESTAMPDIFF(SECOND, creado_en, NOW()) AS edad "
        "FROM api_idempotencia WHERE usuario_id=%s AND clave=%s",
        (user_id, clave),
    )
    fila = cur.fetchone()
    if fila:
        if (fila.get('request_hash') or '') != request_hash:
            raise TiendaError(
                'IDEMPOTENCIA_CONFLICTO',
                'La clave de idempotencia ya se uso con otro cuerpo.', 409)
        respuesta = fila.get('respuesta')
        if respuesta:
            return json.loads(respuesta)
        if _entero(fila.get('edad')) < 600:
            raise TiendaError(
                'IDEMPOTENCIA_EN_PROCESO',
                'Hay una peticion en curso con la misma clave; reintenta en unos segundos.',
                409)
        cur.execute("DELETE FROM api_idempotencia WHERE id=%s", (fila['id'],))

    try:
        cur.execute(
            "INSERT INTO api_idempotencia (usuario_id, clave, endpoint, request_hash) "
            "VALUES (%s,%s,%s,%s)",
            (user_id, clave, endpoint, request_hash),
        )
    except Exception as exc:
        raise TiendaError(
            'IDEMPOTENCIA_EN_PROCESO',
            'Hay una peticion en curso con la misma clave; reintenta en unos segundos.',
            409) from exc
    return None


def marcar_idempotencia(cur, usuario_id, clave, status, data):
    payload = json.dumps({'status': status, 'data': data}, ensure_ascii=False)
    cur.execute(
        "UPDATE api_idempotencia SET respuesta=%s "
        "WHERE usuario_id=%s AND clave=%s AND respuesta IS NULL",
        (payload, _entero(usuario_id), clave),
    )
