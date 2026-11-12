"""Entregas / seguimiento (admin) — Fase 5.

Reutiliza las mismas reglas que `/entregas`, `/entregas/crear/<venta_id>`,
`/entregas/actualizar/<id>`, `/entregas/eliminar/<id>`,
`/entregas/detalle/<id>`, `/entregas/agregar-productos/<id>` y
`/entregas/asignar/<venta_id>` de la web:

  * "cancelado" es terminal en la entrega y en la venta,
  * el estado de la entrega se replica a `ventas.estado` (en espera/entregado/
    cancelado),
  * al cancelar como admin se devuelve el stock y se registra el motivo,
  * una venta puede tener varias lineas de seguimiento: se trabaja con la
    ultima.
"""
from datetime import datetime, timedelta, timezone

from .tienda_service import TiendaError, almacen_db

ESTADOS_ENTREGA = ('pendiente', 'en camino', 'entregado', 'cancelado')
_VENTA_ESTADO = {'pendiente': 'en espera', 'en camino': 'en espera',
                 'entregado': 'entregado', 'cancelado': 'cancelado'}


def _ahora_lima():
    zona_lima = timezone(timedelta(hours=-5))
    return datetime.now(timezone.utc).astimezone(zona_lima).replace(tzinfo=None)


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _invalidate_kpi():
    try:
        from app import _invalidar_cache_kpi
        _invalidar_cache_kpi()
    except Exception:
        pass


def normalizar_filtros(args):
    filtro = str(args.get('filtro', 'todas') or 'todas').strip().lower()
    if filtro not in ('todas', 'pendientes', 'en_camino', 'entregado',
                      'cancelado'):
        filtro = 'todas'
    motivo = args.get('motivo', '')
    return {'filtro': filtro,
            'motivo': (str(motivo).strip() if motivo is not None else '')[:50]}


def listar(cur, filtros, page=1, per_page=20):
    from app import motivo_cancelacion_valido

    select = """
        SELECT e.id, e.venta_id, e.estado, e.direccion_envio, e.fecha_estimada,
               e.fecha_entrega, e.notas, e.created_at,
               v.total, v.nombre AS cliente_nombre, v.documento AS cliente_doc,
               u.correo AS cliente_correo,
               c.motivo_codigo, c.motivo_detalle, c.cancelado_por
    """
    frm = """
        FROM seguimiento_entregas e
        JOIN ventas v ON e.venta_id=v.id
        LEFT JOIN usuarios u ON v.cliente_id=u.id
        LEFT JOIN cancelaciones c ON c.venta_id=v.id
    """
    conds = ["COALESCE(v.oculta,0)=0"]
    params = []
    filtro = filtros.get('filtro')
    if filtro == 'pendientes':
        conds.append("e.estado='pendiente'")
    elif filtro == 'en_camino':
        conds.append("e.estado='en camino'")
    elif filtro == 'entregado':
        conds.append("e.estado='entregado'")
    elif filtro == 'cancelado':
        conds.append("e.estado='cancelado'")
    if motivo_cancelacion_valido(filtros.get('motivo') or ''):
        conds.append("c.motivo_codigo=%s")
        params.append(filtros['motivo'])
    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    cur.execute("SELECT COUNT(*) AS total " + frm + where, tuple(params))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(select + frm + where + " ORDER BY e.created_at DESC "
                "LIMIT %s OFFSET %s", tuple(params) + (per_page, offset))
    return cur.fetchall(), total


def obtener(cur, entrega_id):
    eid = _entero(entrega_id)
    cur.execute(
        """SELECT e.*, v.total, v.nombre AS cliente_nombre,
                  v.documento AS cliente_doc, v.fecha AS venta_fecha,
                  u.correo AS cliente_correo,
                  c.motivo_codigo, c.motivo_detalle, c.cancelado_por
           FROM seguimiento_entregas e
           JOIN ventas v ON e.venta_id=v.id
           LEFT JOIN usuarios u ON v.cliente_id=u.id
           LEFT JOIN cancelaciones c ON c.venta_id=v.id
           WHERE e.id=%s""", (eid,))
    entrega = cur.fetchone()
    if not entrega:
        return None, [], []

    cur.execute(
        f"""SELECT d.producto_id, p.nombre, p.imagen, d.cantidad, d.precio
            FROM detalle_venta d
            JOIN {almacen_db()}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
            ORDER BY d.id""".format(alm=almacen_db()),
        (entrega['venta_id'],))
    productos_venta = cur.fetchall()

    cur.execute(
        f"""SELECT ep.id, ep.producto_id, ep.cantidad,
                   p.nombre AS producto_nombre, p.stock AS stock_actual
            FROM entrega_productos ep
            LEFT JOIN {almacen_db()}.productos p ON ep.producto_id=p.id
            WHERE ep.entrega_id=%s ORDER BY ep.id""".format(alm=almacen_db()),
        (eid,))
    productos_entrega = cur.fetchall()
    return entrega, productos_venta, productos_entrega


def crear(cur, datos):
    vid = _entero(datos.get('venta_id'), -1)
    if vid <= 0:
        raise TiendaError('VALIDACION', 'Indica la venta de la entrega.', 422,
                          {'campo': 'venta_id'})
    cur.execute("SELECT id FROM ventas WHERE id=%s", (vid,))
    if not cur.fetchone():
        raise TiendaError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)
    cur.execute("SELECT id FROM seguimiento_entregas WHERE venta_id=%s LIMIT 1",
                (vid,))
    if cur.fetchone():
        raise TiendaError('ENTREGA_EXISTENTE',
                          'Ya existe seguimiento para esta venta.', 409)

    direccion = str(datos.get('direccion_envio') or '')
    fecha_est = datos.get('fecha_estimada') or None
    notas = str(datos.get('notas') or '')
    cur.execute(
        """INSERT INTO seguimiento_entregas
               (created_at, venta_id, estado, direccion_envio, fecha_estimada,
                notas)
           VALUES (%s,%s,'pendiente',%s,%s,%s)""",
        (_ahora_lima(), vid, direccion, fecha_est, notas))
    entrega_id = cur.lastrowid
    _invalidate_kpi()
    return entrega_id


def actualizar(cur, entrega_id, datos):
    """Cambia estado/notas de una entrega replicando a la venta."""
    from app import _invalidar_cache_kpi, registrar_cancelacion, \
        restaurar_stock_venta

    eid = _entero(entrega_id)
    nuevo = str(datos.get('estado') or 'pendiente').strip().lower()
    if nuevo not in ESTADOS_ENTREGA:
        raise TiendaError('ESTADO_INVALIDO',
                          'El estado debe ser pendiente, en camino, entregado '
                          'o cancelado.', 422, {'campo': 'estado'})
    notas = str(datos.get('notas') or '')

    cur.execute("SELECT id, venta_id, estado FROM seguimiento_entregas "
                "WHERE id=%s", (eid,))
    fila = cur.fetchone()
    if not fila:
        raise TiendaError('ENTREGA_NO_ENCONTRADA', 'La entrega no existe.', 404)
    anterior = fila['estado']
    venta_id = fila['venta_id']

    cur.execute("SELECT id, estado FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta:
        raise TiendaError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)
    venta_anterior = venta['estado']

    if anterior == 'cancelado' or venta_anterior == 'cancelado':
        raise TiendaError('ENTREGA_CANCELADA',
                          f'La venta #{venta_id} ya esta cancelada. Motivo no '
                          'editable.', 409)

    if nuevo == 'entregado':
        cur.execute("UPDATE seguimiento_entregas SET estado=%s, notas=%s, "
                    "fecha_entrega=%s WHERE id=%s",
                    (nuevo, notas, _ahora_lima(), eid))
    else:
        cur.execute("UPDATE seguimiento_entregas SET estado=%s, notas=%s "
                    "WHERE id=%s", (nuevo, notas, eid))

    venta_estado = _VENTA_ESTADO.get(nuevo, 'en espera')
    cur.execute("UPDATE ventas SET estado=%s WHERE id=%s",
                (venta_estado, venta_id))
    if nuevo == 'cancelado' and (venta_anterior or '') != 'cancelado':
        restaurar_stock_venta(cur, venta_id)
        registrar_cancelacion(cur, venta_id, 'otro',
                              'Cancelado por administrador', 'admin')
    try:
        _invalidar_cache_kpi()
    except Exception:
        pass
    return {'id': eid, 'venta_id': venta_id, 'estado_anterior': anterior,
            'estado': nuevo, 'venta_estado': venta_estado}


def eliminar(cur, entrega_id):
    eid = _entero(entrega_id)
    cur.execute("SELECT id FROM seguimiento_entregas WHERE id=%s", (eid,))
    if not cur.fetchone():
        raise TiendaError('ENTREGA_NO_ENCONTRADA', 'La entrega no existe.', 404)
    cur.execute("DELETE FROM seguimiento_entregas WHERE id=%s", (eid,))
    _invalidate_kpi()
    return eid


def _filas_productos(items):
    filas = []
    for item in items or []:
        pid = _entero(item.get('producto_id'), -1)
        cant = _entero(item.get('cantidad'), 1)
        if pid > 0 and cant > 0:
            filas.append({'producto_id': pid, 'cantidad': cant})
    return filas


def set_productos(cur, entrega_id, items):
    """Reemplaza los productos de la entrega (y no toca el stock)."""
    eid = _entero(entrega_id)
    cur.execute("SELECT id FROM seguimiento_entregas WHERE id=%s", (eid,))
    if not cur.fetchone():
        raise TiendaError('ENTREGA_NO_ENCONTRADA', 'La entrega no existe.', 404)
    cur.execute("DELETE FROM entrega_productos WHERE entrega_id=%s", (eid,))
    filas = _filas_productos(items)
    for fila in filas:
        cur.execute("INSERT INTO entrega_productos (entrega_id, producto_id, "
                    "cantidad) VALUES (%s,%s,%s)",
                    (eid, fila['producto_id'], fila['cantidad']))
    _invalidate_kpi()
    return len(filas)


def asignar(cur, venta_id, datos, items):
    """Crea o actualiza el seguimiento de una venta con sus productos."""
    vid = _entero(venta_id)
    cur.execute("SELECT id, total, nombre, documento FROM ventas WHERE id=%s",
                (vid,))
    if not cur.fetchone():
        raise TiendaError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)

    direccion = str(datos.get('direccion_envio') or '')
    fecha_est = datos.get('fecha_estimada') or None
    notas = str(datos.get('notas') or '')
    cur.execute("""SELECT id FROM seguimiento_entregas
                   WHERE venta_id=%s ORDER BY id DESC LIMIT 1""", (vid,))
    existente = cur.fetchone()
    if existente:
        entrega_id = existente['id']
        cur.execute("""UPDATE seguimiento_entregas
                       SET direccion_envio=%s, fecha_estimada=%s, notas=%s
                       WHERE id=%s""", (direccion, fecha_est, notas, entrega_id))
        cur.execute("DELETE FROM entrega_productos WHERE entrega_id=%s",
                    (entrega_id,))
        accion = 'actualizada'
    else:
        cur.execute("""INSERT INTO seguimiento_entregas
                           (created_at, venta_id, estado, direccion_envio,
                            fecha_estimada, notas)
                       VALUES (%s,%s,'pendiente',%s,%s,%s)""",
                    (_ahora_lima(), vid, direccion, fecha_est, notas))
        entrega_id = cur.lastrowid
        accion = 'creada'

    for fila in _filas_productos(items):
        cur.execute("INSERT INTO entrega_productos (entrega_id, producto_id, "
                    "cantidad) VALUES (%s,%s,%s)",
                    (entrega_id, fila['producto_id'], fila['cantidad']))
    _invalidate_kpi()
    return entrega_id, accion
