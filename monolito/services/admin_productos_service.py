"""Productos (admin) de la API v1 — Fase 4.

Mismas tablas y reglas que la web (`app.agregar_producto`, `app.editar_producto`,
`app.eliminar_producto`):

  * la primera imagen de un alta es la principal y las demas van a la galeria,
  * el tope es `tienda_service.MAX_IMAGENES` (4),
  * la validez de una imagen se decide por sus primeros bytes, no por la
    extension ni por el Content-Type que declara el cliente,
  * tras crear o cambiar el stock se sincroniza `productos_para_pedir`.

Los endpoints de esta fase aceptan JSON para crear/actualizar el producto y
`multipart/form-data` para la imagen. `POST /admin/productos` no exige imagen:
la foto se sube despues a `POST /admin/productos/{id}/imagen`.
"""
import os
import secrets
from decimal import Decimal, InvalidOperation

from flask import current_app
from werkzeug.utils import secure_filename

from .tienda_service import MAX_IMAGENES, TiendaError, almacen_db

COLS = ('id, nombre, descripcion, precio, stock, categoria, imagen, estado, '
        'created_at')
ESTADOS = ('activo', 'inactivo')


def _texto(valor, limite):
    return ' '.join(str(valor if valor is not None else '').split())[:limite]


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


# ─────────────────────────────────────────────
# VALIDACION
# ─────────────────────────────────────────────
def _validar_nombre(valor):
    nombre = _texto(valor, 200)
    if not nombre:
        raise TiendaError('NOMBRE_REQUERIDO',
                          'El nombre del producto es obligatorio.', 422,
                          {'campo': 'nombre'})
    return nombre


def _validar_precio(valor):
    try:
        precio = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        raise TiendaError('PRECIO_INVALIDO', 'El precio no es valido.', 422,
                          {'campo': 'precio'})
    if precio < 0:
        raise TiendaError('PRECIO_INVALIDO',
                          'No se permiten precios negativos.', 422,
                          {'campo': 'precio'})
    return precio.quantize(Decimal('0.01'))


def _validar_stock(valor):
    try:
        stock = int(valor)
    except (TypeError, ValueError):
        raise TiendaError('STOCK_INVALIDO',
                          'El stock debe ser un numero entero.', 422,
                          {'campo': 'stock'})
    if stock < 0:
        raise TiendaError('STOCK_INVALIDO',
                          'No se permiten valores negativos en el stock.', 422,
                          {'campo': 'stock'})
    return stock


def _validar_estado(valor):
    estado = (valor or '').strip().lower()
    if estado not in ESTADOS:
        raise TiendaError('ESTADO_INVALIDO',
                          'El estado debe ser activo o inactivo.', 422,
                          {'campo': 'estado'})
    return estado


def _categoria(valor):
    return _texto(valor, 100) or 'Otros'


# ─────────────────────────────────────────────
# IMAGENES
# ─────────────────────────────────────────────
def mime_imagen(datos):
    """Tipo real de la imagen reutilizando la validacion de la web."""
    from app import _mime_imagen
    return _mime_imagen(datos)


def _procesar_archivo(archivo):
    """Valida y guarda una imagen. Devuelve (ruta_relativa, bytes, mime)."""
    from app import _EXT_IMAGEN

    datos = archivo.read() if hasattr(archivo, 'read') else archivo
    mime = mime_imagen(datos)
    if not mime:
        raise TiendaError(
            'IMAGEN_INVALIDA',
            'El archivo no es una imagen valida (JPG, PNG, GIF, WEBP o AVIF).',
            422,
        )
    nombre = getattr(archivo, 'filename', '') or ''
    base = os.path.splitext(secure_filename(nombre))[0][:60] or 'imagen'
    fn = secrets.token_hex(8) + '_' + base + _EXT_IMAGEN[mime]
    try:
        carpeta = current_app.config.get('UPLOAD_FOLDER')
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
            with open(os.path.join(carpeta, fn), 'wb') as manejador:
                manejador.write(datos)
    except Exception:
        # La copia en disco es solo cache; los bytes viven en la BD.
        pass
    return 'uploads/' + fn, datos, mime


def _sincronizar(cur, producto_id):
    """Reutiliza la logica de la web sin importarla al cargar el modulo."""
    try:
        from app import sincronizar_pedido_por_stock
        sincronizar_pedido_por_stock(cur, producto_id)
    except Exception:
        pass


# ─────────────────────────────────────────────
# LECTURA
# ─────────────────────────────────────────────
def listar(cur, buscar='', page=1, per_page=15):
    """Productos (activos e inactivos) paginados, como `/admin`."""
    where = 'WHERE p.nombre LIKE %s'
    params = [f'%{buscar}%']
    cur.execute(
        f"SELECT COUNT(*) AS total FROM {almacen_db()}.productos p {where}",
        tuple(params),
    )
    total = _entero((cur.fetchone() or {}).get('total'))
    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"SELECT p.* FROM {almacen_db()}.productos p {where} "
        f"ORDER BY p.id DESC LIMIT %s OFFSET %s",
        tuple(params) + (per_page, offset),
    )
    return cur.fetchall(), total


def alertas(cur):
    """Contadores del panel `/admin` (pedidos a proveedor y proveedores)."""
    pendientes = 0
    proveedores = 0
    try:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM {almacen_db()}.productos_para_pedir "
            f"WHERE estado='pendiente'")
        pendientes = _entero((cur.fetchone() or {}).get('total'))
    except Exception:
        pass
    try:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM {almacen_db()}.proveedores")
        proveedores = _entero((cur.fetchone() or {}).get('total'))
    except Exception:
        pass
    return {'pedidos_pendientes': pendientes, 'total_proveedores': proveedores}


def obtener(cur, producto_id):
    cur.execute(
        f"SELECT {COLS} FROM {almacen_db()}.productos WHERE id=%s",
        (_entero(producto_id),),
    )
    return cur.fetchone()


def imagenes(cur, producto_id):
    """Filas de la galeria (id, imagen, orden) para poder borrar por id."""
    cur.execute(
        f"SELECT id, imagen, orden FROM {almacen_db()}.productos_imagenes "
        f"WHERE producto_id=%s ORDER BY orden, id",
        (_entero(producto_id),),
    )
    return cur.fetchall()


def _imagenes_extra(cur, producto_id):
    """Rutas de la galeria, sin exponer los bytes."""
    return [fila.get('imagen') for fila in imagenes(cur, producto_id)
            if fila.get('imagen')]


# ─────────────────────────────────────────────
# ESCRITURA
# ─────────────────────────────────────────────
def crear(cur, datos):
    nombre = _validar_nombre(datos.get('nombre'))
    descripcion = _texto(datos.get('descripcion'), 2000)
    precio = _validar_precio(datos.get('precio'))
    stock = _validar_stock(datos.get('stock'))
    categoria = _categoria(datos.get('categoria'))
    estado = _validar_estado(datos.get('estado')) if datos.get('estado') \
        else 'activo'

    cur.execute(
        f"INSERT INTO {almacen_db()}.productos "
        f"(nombre, descripcion, precio, stock, categoria, estado) "
        f"VALUES (%s,%s,%s,%s,%s,%s)",
        (nombre, descripcion, str(precio), stock, categoria, estado),
    )
    producto_id = cur.lastrowid
    _sincronizar(cur, producto_id)
    return producto_id


def actualizar(cur, producto_id, datos):
    """Actualiza solo las claves presentes en `datos` (PATCH)."""
    actual = obtener(cur, producto_id)
    if not actual:
        raise TiendaError('PRODUCTO_NO_ENCONTRADO',
                          'El producto no existe.', 404)

    nombre = _validar_nombre(datos.get('nombre', actual.get('nombre')))
    descripcion = _texto(
        datos.get('descripcion', actual.get('descripcion')), 2000)
    precio = _validar_precio(datos.get('precio', actual.get('precio')))
    stock = _validar_stock(datos.get('stock', actual.get('stock')))
    categoria = _categoria(datos.get('categoria', actual.get('categoria')))
    estado = _validar_estado(datos.get('estado', actual.get('estado')))

    cur.execute(
        f"UPDATE {almacen_db()}.productos SET nombre=%s, descripcion=%s, "
        f"precio=%s, stock=%s, categoria=%s, estado=%s WHERE id=%s",
        (nombre, descripcion, str(precio), stock, categoria, estado,
         _entero(producto_id)),
    )
    _sincronizar(cur, producto_id)
    return obtener(cur, producto_id)


def cambiar_estado(cur, producto_id, estado):
    actual = obtener(cur, producto_id)
    if not actual:
        raise TiendaError('PRODUCTO_NO_ENCONTRADO',
                          'El producto no existe.', 404)
    estado = _validar_estado(estado)
    cur.execute(
        f"UPDATE {almacen_db()}.productos SET estado=%s WHERE id=%s",
        (estado, _entero(producto_id)),
    )
    return obtener(cur, producto_id)


def eliminar_definitivo(cur, producto_id):
    actual = obtener(cur, producto_id)
    if not actual:
        raise TiendaError('PRODUCTO_NO_ENCONTRADO',
                          'El producto no existe.', 404)
    cur.execute(
        f"DELETE FROM {almacen_db()}.productos_imagenes WHERE producto_id=%s",
        (_entero(producto_id),),
    )
    cur.execute(
        f"DELETE FROM {almacen_db()}.productos WHERE id=%s",
        (_entero(producto_id),),
    )
    return True


def agregar_imagenes(cur, producto_id, archivos, principal=False):
    """Agrega imagenes: si `principal` es True, la primera reemplaza la principal."""
    producto = obtener(cur, producto_id)
    if not producto:
        raise TiendaError('PRODUCTO_NO_ENCONTRADO',
                          'El producto no existe.', 404)
    archivos = [a for a in (archivos or []) if a]
    if not archivos:
        raise TiendaError('IMAGEN_REQUERIDA',
                          'Debes adjuntar al menos una imagen.', 422)

    extras = imagenes(cur, producto_id)
    total_actual = (1 if producto.get('imagen') else 0) + len(extras)
    # La primera imagen reemplaza a la principal solo si ya existia una.
    reemplaza = 1 if (principal and producto.get('imagen')) else 0
    final = total_actual + len(archivos) - reemplaza
    if final > MAX_IMAGENES:
        raise TiendaError(
            'IMAGENES_MAXIMO',
            f'Un producto admite como maximo {MAX_IMAGENES} imagenes.',
            409,
            {'maximo': MAX_IMAGENES, 'actuales': total_actual,
             'quedarian': final},
        )

    procesadas = [_procesar_archivo(a) for a in archivos]

    if principal:
        ruta, datos, mime = procesadas.pop(0)
        cur.execute(
            f"UPDATE {almacen_db()}.productos SET imagen=%s, imagen_datos=%s, "
            f"imagen_mime=%s WHERE id=%s",
            (ruta, datos, mime, _entero(producto_id)),
        )

    orden_base = max([_entero(f.get('orden')) for f in extras], default=0) + 1
    for i, (ruta, datos, mime) in enumerate(procesadas, start=orden_base):
        cur.execute(
            f"INSERT INTO {almacen_db()}.productos_imagenes "
            f"(producto_id, imagen, imagen_datos, imagen_mime, orden) "
            f"VALUES (%s,%s,%s,%s,%s)",
            (_entero(producto_id), ruta, datos, mime, i),
        )


def eliminar_imagen(cur, producto_id, imagen_id):
    cur.execute(
        f"DELETE FROM {almacen_db()}.productos_imagenes "
        f"WHERE id=%s AND producto_id=%s",
        (_entero(imagen_id), _entero(producto_id)),
    )
    return _entero(getattr(cur, 'rowcount', 0)) > 0


def quitar_principal(cur, producto_id):
    """Quita la imagen principal; si hay galeria promueve la primera (como la web)."""
    producto = obtener(cur, producto_id)
    if not producto:
        raise TiendaError('PRODUCTO_NO_ENCONTRADO',
                          'El producto no existe.', 404)

    extras = imagenes(cur, producto_id)
    if extras:
        primera = extras[0]
        cur.execute(
            f"SELECT imagen_datos, imagen_mime FROM {almacen_db()}.productos_imagenes "
            f"WHERE id=%s AND producto_id=%s",
            (_entero(primera.get('id')), _entero(producto_id)),
        )
        blob = cur.fetchone() or {}
        cur.execute(
            f"UPDATE {almacen_db()}.productos SET imagen=%s, imagen_datos=%s, "
            f"imagen_mime=%s WHERE id=%s",
            (primera.get('imagen'), blob.get('imagen_datos'),
             blob.get('imagen_mime'), _entero(producto_id)),
        )
        cur.execute(
            f"DELETE FROM {almacen_db()}.productos_imagenes "
            f"WHERE id=%s AND producto_id=%s",
            (_entero(primera.get('id')), _entero(producto_id)),
        )
    else:
        cur.execute(
            f"UPDATE {almacen_db()}.productos SET imagen=NULL, imagen_datos=NULL, "
            f"imagen_mime=NULL WHERE id=%s",
            (_entero(producto_id),),
        )
    return obtener(cur, producto_id)
