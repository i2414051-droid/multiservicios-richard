"""Ventas (admin) de la API v1 — Fase 4.

Reutiliza los mismos filtros y contadores que `/historial-compras` de la web:
busqueda por correo, motivo de cancelacion, estado, metodo de pago, tipo de
entrega, distrito, categoria y visibilidad (ocultas). El cambio de estado
replica exactamente las reglas de `app.actualizar_estado_venta` (cancelado es
terminal, se devuelve stock y se registra el motivo).
"""
from .tienda_service import TiendaError, almacen_db

ESTADOS_VALIDOS = ('entregado', 'en espera', 'cancelado')
PAGOS = ('tarjeta', 'yape', 'transferencia', 'efectivo', 'plin')


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _ahora_lima():
    from datetime import datetime, timedelta, timezone
    zona_lima = timezone(timedelta(hours=-5))
    return datetime.now(timezone.utc).astimezone(zona_lima).replace(tzinfo=None)


# ─────────────────────────────────────────────
# FILTROS
# ─────────────────────────────────────────────
def normalizar(args):
    """Normaliza los filtros de la query string. `args` puede ser MultiDict o dict."""
    def _get(nombre, defecto=''):
        valor = args.get(nombre, defecto)
        return str(valor).strip() if valor is not None else defecto

    return {
        'buscar': _get('buscar')[:100],
        'motivo': _get('motivo'),
        'estado': _get('estado').lower(),
        'pago': _get('pago').lower(),
        'entrega': _get('entrega').lower(),
        'distrito': _get('distrito'),
        'categoria': _get('categoria'),
        'ocultas': _get('ocultas', '0') == '1',
    }


def _where(filtros):
    from app import motivo_cancelacion_valido

    where = "WHERE u.correo LIKE %s"
    params = [f"%{filtros.get('buscar') or ''}%"]

    if not filtros.get('ocultas'):
        where += " AND COALESCE(v.oculta,0)=0"

    motivo = filtros.get('motivo') or ''
    if motivo_cancelacion_valido(motivo):
        where += " AND c.motivo_codigo=%s"
        params.append(motivo)

    estado = filtros.get('estado') or ''
    if estado in ('entregado', 'cancelado', 'espera', 'en espera'):
        where += " AND v.estado=%s"
        params.append('en espera' if estado in ('espera', 'en espera') else estado)

    pago = filtros.get('pago') or ''
    if pago in PAGOS:
        where += " AND v.metodo_pago=%s"
        params.append(pago)
    elif pago == 'sin_dato':
        where += " AND COALESCE(NULLIF(v.metodo_pago, ''), 'sin_dato')='sin_dato'"

    entrega = filtros.get('entrega') or ''
    if entrega in ('recojo', 'delivery'):
        where += " AND v.tipo_entrega=%s"
        params.append(entrega)

    if filtros.get('distrito'):
        where += " AND v.distrito=%s"
        params.append(filtros['distrito'])

    if filtros.get('categoria'):
        where += (f" AND EXISTS (SELECT 1 FROM detalle_venta d2 "
                  f"JOIN {almacen_db()}.productos p2 ON p2.id=d2.producto_id "
                  f"WHERE d2.venta_id=v.id AND p2.categoria=%s)")
        params.append(filtros['categoria'])

    return where, params


def _items(cur, venta_id):
    cur.execute(
        f"""SELECT p.id AS producto_id, p.nombre, p.descripcion, p.imagen,
                   d.cantidad, d.precio
            FROM detalle_venta d
            JOIN {almacen_db()}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s ORDER BY d.id""",
        (_entero(venta_id),),
    )
    return cur.fetchall()


# ─────────────────────────────────────────────
# LECTURA
# ─────────────────────────────────────────────
def totales(cur):
    """Gran total (entregado), monto en espera y ventas ocultas."""
    cur.execute(
        "SELECT SUM(total) AS gran_total FROM ventas "
        "WHERE COALESCE(oculta,0)=0 AND estado='entregado'")
    gran_total = (cur.fetchone() or {}).get('gran_total') or 0

    cur.execute(
        "SELECT SUM(total) AS monto_espera FROM ventas "
        "WHERE COALESCE(oculta,0)=0 AND estado='en espera'")
    monto_en_espera = (cur.fetchone() or {}).get('monto_espera') or 0

    n_ocultas = 0
    try:
        cur.execute("SELECT COUNT(*) AS n FROM ventas WHERE oculta=1")
        n_ocultas = _entero((cur.fetchone() or {}).get('n'))
    except Exception:
        pass
    return {'gran_total': gran_total, 'monto_en_espera': monto_en_espera,
            'ocultas': n_ocultas}


def listar(cur, filtros, page=1, per_page=20):
    where, params = _where(filtros)
    join_cancel = "LEFT JOIN cancelaciones c ON c.venta_id=v.id"

    cur.execute(
        f"SELECT COUNT(*) AS total FROM ventas v "
        f"JOIN usuarios u ON v.cliente_id=u.id {join_cancel} {where}",
        tuple(params),
    )
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"""SELECT v.id, u.correo, u.id AS cliente_id, v.total, v.fecha,
                   v.documento, v.doc_tipo, v.nombre AS titular, v.apellido,
                   v.telefono, v.estado, v.estado_pago, v.metodo_pago,
                   v.tipo_entrega, v.distrito, v.direccion_envio, v.costo_envio,
                   COALESCE(v.oculta,0) AS oculta,
                   c.motivo_codigo, c.motivo_detalle, c.cancelado_por
            FROM ventas v JOIN usuarios u ON v.cliente_id=u.id {join_cancel}
            {where} ORDER BY v.id DESC LIMIT %s OFFSET %s""",
        tuple(params) + (per_page, offset),
    )
    filas = cur.fetchall()
    for venta in filas:
        venta['productos'] = _items(cur, venta['id'])
    return filas, total


def filas_export(cur, filtros):
    """Todas las ventas del filtro, sin paginar, para CSV/PDF."""
    where, params = _where(filtros)
    cur.execute(
        f"""SELECT v.id, u.correo, v.total, v.fecha, v.documento,
                   v.nombre AS titular, v.estado, v.estado_pago, v.metodo_pago,
                   v.tipo_entrega, v.distrito
            FROM ventas v JOIN usuarios u ON v.cliente_id=u.id
            LEFT JOIN cancelaciones c ON c.venta_id=v.id
            {where} ORDER BY v.id DESC""",
        tuple(params),
    )
    return cur.fetchall()


def obtener(cur, venta_id):
    cur.execute(
        """SELECT v.*, u.correo, u.id AS cliente_id,
                  c.motivo_codigo, c.motivo_detalle, c.cancelado_por
           FROM ventas v JOIN usuarios u ON v.cliente_id=u.id
           LEFT JOIN cancelaciones c ON c.venta_id=v.id
           WHERE v.id=%s""",
        (_entero(venta_id),),
    )
    venta = cur.fetchone()
    if not venta:
        return None, []
    return venta, _items(cur, venta_id)


# ─────────────────────────────────────────────
# ESCRITURA
# ─────────────────────────────────────────────
def actualizar_estado(cur, venta_id, estado):
    from app import _invalidar_cache_kpi, registrar_cancelacion, restaurar_stock_venta

    estado = (estado or '').strip().lower()
    if estado not in ESTADOS_VALIDOS:
        raise TiendaError('ESTADO_INVALIDO',
                          'El estado debe ser entregado, en espera o cancelado.',
                          422, {'campo': 'estado'})

    vid = _entero(venta_id)
    cur.execute("SELECT id, estado FROM ventas WHERE id=%s", (vid,))
    venta = cur.fetchone()
    if not venta:
        raise TiendaError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)

    anterior = (venta.get('estado') or '').strip().lower()
    if anterior == 'cancelado':
        raise TiendaError('VENTA_CANCELADA',
                          'La venta ya esta cancelada; su motivo no es editable.',
                          409)

    cur.execute("UPDATE ventas SET estado=%s WHERE id=%s", (estado, vid))
    if estado == 'entregado':
        cur.execute(
            "UPDATE seguimiento_entregas SET estado='entregado', "
            "fecha_entrega=%s WHERE venta_id=%s",
            (_ahora_lima(), vid),
        )
    else:
        seg_estado = {'en espera': 'pendiente',
                      'cancelado': 'cancelado'}.get(estado, 'pendiente')
        cur.execute(
            "UPDATE seguimiento_entregas SET estado=%s WHERE venta_id=%s",
            (seg_estado, vid),
        )

    if estado == 'cancelado' and anterior != 'cancelado':
        restaurar_stock_venta(cur, vid)
        registrar_cancelacion(cur, vid, 'otro', 'Cancelado por administrador',
                              'admin')

    try:
        _invalidar_cache_kpi()
    except Exception:
        pass
    return vid


def cambiar_visibilidad(cur, venta_id, oculta):
    vid = _entero(venta_id)
    cur.execute("SELECT id FROM ventas WHERE id=%s", (vid,))
    if not cur.fetchone():
        raise TiendaError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)
    cur.execute("UPDATE ventas SET oculta=%s WHERE id=%s",
                (1 if oculta else 0, vid))
    return bool(oculta)


def limpiar(cur):
    """Borra el historial completo (detalle y ventas), como `/limpiar`."""
    cur.execute("DELETE FROM detalle_venta")
    borrados = _entero(getattr(cur, 'rowcount', 0))
    cur.execute("DELETE FROM ventas")
    borrados += _entero(getattr(cur, 'rowcount', 0))
    return borrados
