"""Consultas de catalogo, carrito y configuracion de la tienda.

Se usan tanto por la API v1 como (a futuro) por cualquier otro cliente. La
fuente de verdad sigue siendo la base de datos del monolito:

  * productos y productos_imagenes viven en la BD de almacen,
  * carrito y ajustes viven en la BD principal.

No se duplican reglas de negocio: el stock maximo, el tope de 500 unidades y
las reglas de envio son las mismas que usa la web.
"""
import os
from decimal import Decimal, InvalidOperation

from flask import current_app


class TiendaError(Exception):
    """Error de negocio del catalogo/carrito, independiente de Flask."""

    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']
DISTRITOS_DELIVERY = ('Chilca', 'El Tambo', 'Huancayo')
CANTIDAD_MAXIMA = 500
MAX_IMAGENES = 4

COLS_PRODUCTO = ('id, nombre, descripcion, precio, stock, categoria, '
                 'imagen, estado, created_at')

ORDENES = {
    'nuevo': 'id DESC',
    'precio_asc': 'precio ASC, id DESC',
    'precio_desc': 'precio DESC, id DESC',
    'nombre_asc': 'nombre ASC, id DESC',
    'nombre_desc': 'nombre DESC, id DESC',
}

AJUSTES_DEFECTO = {
    'empresa_direccion': 'Av. Leoncio Prado 1499 - Chilca, Huancayo.',
    'empresa_lat': '-12.0728939',
    'empresa_lon': '-75.1904488',
    'envio_delivery': '15',
    'envio_gratis_desde': '300',
    'delivery_minimo': '50',
}

_COLS_ITEM = ('c.id AS item_id, c.producto_id, c.cantidad, '
              'c.created_at AS agregado_en, p.nombre, p.descripcion, '
              'p.precio, p.stock, p.categoria, p.imagen, p.estado')


def almacen_db():
    """Nombre de la BD de almacen, respetando el guard de app.py."""
    return (current_app.config.get('ALMACEN_DB')
            or os.environ.get('MYSQL_DB_ALMACEN', 'proyecto_gestion_almacen'))


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


def _flotante(valor, defecto=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


# ─────────────────────────────────────────────
# CATALOGO
# ─────────────────────────────────────────────
def listar_productos(cur, buscar='', categoria='Todos',
                     solo_disponibles=False, orden='nuevo',
                     page=1, per_page=20):
    """Devuelve (filas, total) con la misma semantica que /api/productos."""
    condiciones = ["estado='activo'"]
    valores = []

    if buscar:
        condiciones.append('nombre LIKE %s')
        valores.append('%' + buscar + '%')
    if categoria and categoria != 'Todos':
        condiciones.append('categoria=%s')
        valores.append(categoria)
    if solo_disponibles:
        condiciones.append('stock > 0')

    where = ' AND '.join(condiciones)
    cur.execute(
        f"SELECT COUNT(*) AS total FROM {almacen_db()}.productos "
        f"WHERE {where}",
        tuple(valores),
    )
    fila = cur.fetchone() or {}
    total = _entero(fila.get('total'))

    orden_sql = ORDENES.get(orden, ORDENES['nuevo'])
    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"SELECT {COLS_PRODUCTO} FROM {almacen_db()}.productos "
        f"WHERE {where} ORDER BY {orden_sql} LIMIT %s OFFSET %s",
        tuple(valores) + (per_page, offset),
    )
    return cur.fetchall(), total


def obtener_producto(cur, producto_id, bloquear=False, solo_activo=False):
    """Ficha de producto. `bloquear` usa SELECT ... FOR UPDATE para el carrito."""
    sql = f"SELECT {COLS_PRODUCTO} FROM {almacen_db()}.productos WHERE id=%s"
    if solo_activo:
        sql += " AND estado='activo'"
    if bloquear:
        sql += ' FOR UPDATE'
    cur.execute(sql, (int(producto_id),))
    return cur.fetchone()


def imagenes_extra(cur, ids):
    """Mapa {producto_id: [rutas]} con las imagenes adicionales."""
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return {}
    marcas = ','.join(['%s'] * len(ids))
    cur.execute(
        f"SELECT producto_id, imagen FROM {almacen_db()}.productos_imagenes "
        f"WHERE producto_id IN ({marcas}) AND imagen IS NOT NULL "
        f"ORDER BY producto_id, orden, id",
        tuple(ids),
    )
    mapa = {}
    for fila in cur.fetchall():
        mapa.setdefault(fila['producto_id'], []).append(fila['imagen'])
    return mapa


def categorias_con_conteo(cur):
    """Categorias de la tienda, incluyendo las que no tienen productos."""
    cur.execute(
        f"SELECT categoria, COUNT(*) AS total_productos, "
        f"SUM(CASE WHEN stock > 0 THEN 1 ELSE 0 END) AS disponibles "
        f"FROM {almacen_db()}.productos WHERE estado='activo' "
        f"GROUP BY categoria"
    )
    mapa = {}
    for fila in cur.fetchall():
        nombre = fila.get('categoria')
        if nombre:
            mapa[nombre] = {
                'total_productos': _entero(fila.get('total_productos')),
                'disponibles': _entero(fila.get('disponibles')),
            }

    resultado = []
    for nombre in CATEGORIAS:
        datos = mapa.pop(nombre, {'total_productos': 0, 'disponibles': 0})
        resultado.append({'nombre': nombre, **datos})
    for nombre in sorted(mapa):
        resultado.append({'nombre': nombre, **mapa[nombre]})
    return resultado


# ─────────────────────────────────────────────
# CONFIGURACION / ENVIO
# ─────────────────────────────────────────────
def leer_ajustes(cur):
    """Lee los ajustes publicos de la tienda; si la tabla falla usa defaults."""
    valores = dict(AJUSTES_DEFECTO)
    claves = tuple(AJUSTES_DEFECTO)
    marcas = ','.join(['%s'] * len(claves))
    try:
        cur.execute(
            f"SELECT clave, valor FROM ajustes WHERE clave IN ({marcas})",
            claves,
        )
        for fila in cur.fetchall():
            clave = fila.get('clave')
            if clave in valores:
                valores[clave] = fila.get('valor')
    except Exception as exc:
        # La tienda debe seguir mostrando el catalogo aunque falte la tabla
        # `ajustes` en una instalacion vieja. El error queda en el log.
        try:
            current_app.logger.warning(
                'API v1: no se pudieron leer los ajustes: %s', exc)
        except Exception:
            pass
    return valores


def envio_delivery(ajustes):
    return _decimal(ajustes.get('envio_delivery') or '15').quantize(Decimal('0.01'))


def envio_gratis_desde(ajustes):
    return _decimal(ajustes.get('envio_gratis_desde') or '0').quantize(Decimal('0.01'))


def minimo_delivery(ajustes):
    return _decimal(ajustes.get('delivery_minimo') or '50').quantize(Decimal('0.01'))


def costo_envio(subtotal, ajustes):
    """Misma regla que app.costo_envio: tarifa plana, gratis desde el monto."""
    total = _decimal(subtotal)
    gratis_desde = envio_gratis_desde(ajustes)
    if gratis_desde > 0 and total >= gratis_desde:
        return Decimal('0.00')
    return envio_delivery(ajustes)


# ─────────────────────────────────────────────
# CARRITO
# ─────────────────────────────────────────────
def _fila_item(fila):
    """Copia una fila de carrito lista para serializar."""
    return {
        'item_id': _entero(fila.get('item_id')),
        'producto_id': _entero(fila.get('producto_id')),
        'cantidad': _entero(fila.get('cantidad')),
        'agregado_en': fila.get('agregado_en'),
        'nombre': fila.get('nombre') or '',
        'descripcion': fila.get('descripcion') or '',
        'precio': fila.get('precio'),
        'stock': _entero(fila.get('stock')),
        'categoria': fila.get('categoria') or '',
        'imagen': fila.get('imagen'),
        'estado': (fila.get('estado') or 'activo').lower(),
    }


def items_carrito(cur, usuario_id, bloquear=False):
    """Lineas del carrito agrupadas por producto.

    La tabla `carrito` no tiene una clave unica historica, asi que puede haber
    filas duplicadas para el mismo producto. Se suman aqui para que el cliente
    nunca vea dos lineas iguales.
    """
    sql = (f"SELECT {_COLS_ITEM} FROM carrito c "
           f"JOIN {almacen_db()}.productos p ON c.producto_id=p.id "
           f"WHERE c.usuario_id=%s ORDER BY c.id")
    if bloquear:
        sql += ' FOR UPDATE'
    cur.execute(sql, (str(usuario_id),))

    agrupados = {}
    for fila in cur.fetchall():
        item = _fila_item(fila)
        pid = item['producto_id']
        if pid in agrupados:
            agrupados[pid]['cantidad'] += item['cantidad']
        else:
            agrupados[pid] = item
    return list(agrupados.values())


def item_carrito(cur, usuario_id, producto_id, bloquear=False):
    """Una linea del carrito (o None) con la cantidad total si hay duplicados."""
    sql = (f"SELECT {_COLS_ITEM} FROM carrito c "
           f"JOIN {almacen_db()}.productos p ON c.producto_id=p.id "
           f"WHERE c.usuario_id=%s AND c.producto_id=%s ORDER BY c.id")
    if bloquear:
        sql += ' FOR UPDATE'
    cur.execute(sql, (str(usuario_id), int(producto_id)))
    filas = cur.fetchall()
    if not filas:
        return None

    item = _fila_item(filas[0])
    item['cantidad'] = sum(_entero(f.get('cantidad')) for f in filas)
    item['_ids'] = [_entero(f.get('item_id')) for f in filas]
    return item


def _guardar_cantidad(cur, usuario_id, producto_id, cantidad, ids):
    """Actualiza la primera fila y elimina duplicados historicos."""
    if not ids:
        cur.execute(
            "UPDATE carrito SET cantidad=%s WHERE usuario_id=%s AND producto_id=%s",
            (int(cantidad), str(usuario_id), int(producto_id)),
        )
        return
    cur.execute(
        "UPDATE carrito SET cantidad=%s WHERE id=%s",
        (int(cantidad), ids[0]),
    )
    if len(ids) > 1:
        marcas = ','.join(['%s'] * len(ids[1:]))
        cur.execute(
            f"DELETE FROM carrito WHERE id IN ({marcas})",
            tuple(ids[1:]),
        )


def _validar_producto_compra(cur, producto_id):
    producto = obtener_producto(cur, producto_id, bloquear=True)
    if not producto:
        raise TiendaError('PRODUCTO_NO_ENCONTRADO', 'El producto no existe.', 404)
    estado = (producto.get('estado') or '').lower()
    if estado != 'activo':
        raise TiendaError('PRODUCTO_NO_DISPONIBLE',
                       'El producto no esta disponible.', 409)
    stock = _entero(producto.get('stock'))
    if stock <= 0:
        raise TiendaError('SIN_STOCK',
                       'El producto no tiene stock disponible.', 409,
                       {'stock': 0})
    return producto, stock


def agregar_item(cur, usuario_id, producto_id, cantidad):
    """Suma unidades al carrito respetando stock y tope de 500."""
    producto, stock = _validar_producto_compra(cur, producto_id)
    actual = item_carrito(cur, usuario_id, producto_id, bloquear=True)
    en_carrito = _entero(actual.get('cantidad')) if actual else 0
    nueva = en_carrito + int(cantidad)

    if nueva > CANTIDAD_MAXIMA:
        raise TiendaError(
            'CANTIDAD_MAXIMA',
            f'No puedes comprar mas de {CANTIDAD_MAXIMA} unidades del mismo producto.',
            409,
            {'maximo': CANTIDAD_MAXIMA, 'stock': stock, 'en_carrito': en_carrito},
        )
    if nueva > stock:
        raise TiendaError(
            'STOCK_INSUFICIENTE',
            f'Stock insuficiente: solo hay {stock} unidades disponibles.',
            409,
            {'stock': stock, 'en_carrito': en_carrito,
             'maximo': min(stock, CANTIDAD_MAXIMA)},
        )

    if actual:
        _guardar_cantidad(cur, usuario_id, producto_id, nueva,
                          actual.get('_ids') or [])
    else:
        cur.execute(
            "INSERT INTO carrito (usuario_id, producto_id, cantidad) "
            "VALUES (%s,%s,%s)",
            (str(usuario_id), int(producto_id), nueva),
        )
    return {'cantidad': nueva, 'stock': stock,
            'nombre': producto.get('nombre') or '',
            'precio': producto.get('precio')}


def actualizar_item(cur, usuario_id, producto_id, cantidad):
    """Fija la cantidad de una linea. Cantidad 0 elimina la linea."""
    if int(cantidad) <= 0:
        if not eliminar_item(cur, usuario_id, producto_id):
            raise TiendaError('ITEM_NO_ENCONTRADO',
                           'El producto no esta en el carrito.', 404)
        return {'eliminado': True, 'cantidad': 0}

    producto, stock = _validar_producto_compra(cur, producto_id)
    if int(cantidad) > CANTIDAD_MAXIMA:
        raise TiendaError(
            'CANTIDAD_MAXIMA',
            f'No puedes comprar mas de {CANTIDAD_MAXIMA} unidades del mismo producto.',
            409,
            {'maximo': CANTIDAD_MAXIMA, 'stock': stock},
        )
    if int(cantidad) > stock:
        raise TiendaError(
            'STOCK_INSUFICIENTE',
            f'Stock insuficiente: solo hay {stock} unidades disponibles.',
            409,
            {'stock': stock, 'maximo': min(stock, CANTIDAD_MAXIMA)},
        )

    actual = item_carrito(cur, usuario_id, producto_id, bloquear=True)
    if not actual:
        raise TiendaError('ITEM_NO_ENCONTRADO',
                       'El producto no esta en el carrito.', 404)

    _guardar_cantidad(cur, usuario_id, producto_id, cantidad,
                      actual.get('_ids') or [])
    return {'eliminado': False, 'cantidad': int(cantidad),
            'stock': stock, 'nombre': producto.get('nombre') or '',
            'precio': producto.get('precio')}


def eliminar_item(cur, usuario_id, producto_id):
    """Elimina todas las filas del producto. Devuelve cuantas borro."""
    cur.execute(
        "DELETE FROM carrito WHERE usuario_id=%s AND producto_id=%s",
        (str(usuario_id), int(producto_id)),
    )
    return _entero(getattr(cur, 'rowcount', 0))


def vaciar_carrito(cur, usuario_id):
    """Elimina todo el carrito del usuario. Devuelve cuantas lineas borro."""
    cur.execute(
        "DELETE FROM carrito WHERE usuario_id=%s",
        (str(usuario_id),),
    )
    return _entero(getattr(cur, 'rowcount', 0))
