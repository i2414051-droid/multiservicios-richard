"""Salidas de productos (admin) — Fase 5.

Reutiliza las mismas reglas que `/salidas`, `/registrar-salida`,
`/comprobante-salida/<id>` y `/eliminar-salida/<id>` de la web:

  * al registrar una salida se descuenta el stock (nunca por debajo de 0),
  * no se admite una salida si algun producto no tiene stock suficiente,
  * eliminar una salida devuelve el stock y reevalua los pedidos a proveedor,
  * el PDF de la salida es nuevo en la API (la web no lo tenia).
"""
from datetime import datetime, timedelta, timezone

from .tienda_service import TiendaError, almacen_db


def _ahora_lima():
    zona_lima = timezone(timedelta(hours=-5))
    return datetime.now(timezone.utc).astimezone(zona_lima).replace(tzinfo=None)


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def normalizar_filtros(args):
    valor = args.get('buscar', '')
    return {'buscar': (str(valor).strip() if valor is not None else '')[:100]}


def listar(cur, filtros, page=1, per_page=20):
    alm = almacen_db()
    buscar = f"%{filtros.get('buscar') or ''}%"
    cur.execute(
        f"""SELECT COUNT(*) AS total FROM {alm}.salidas s
            LEFT JOIN ventas v ON s.venta_id=v.id
            WHERE COALESCE(v.nombre, '') LIKE %s""", (buscar,))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"""SELECT s.id, s.fecha, s.notas,
                   v.id AS venta_id, v.nombre AS cliente_nombre,
                   v.total AS venta_total,
                   (SELECT SUM(ds.cantidad) FROM {alm}.detalle_salida ds
                    WHERE ds.salida_id=s.id) AS total_items,
                   (SELECT COALESCE(SUM(ds.cantidad * p.precio), 0)
                    FROM {alm}.detalle_salida ds
                    JOIN {alm}.productos p ON p.id=ds.producto_id
                    WHERE ds.salida_id=s.id) AS valor_estimado
            FROM {alm}.salidas s
            LEFT JOIN ventas v ON s.venta_id=v.id
            WHERE COALESCE(v.nombre, '') LIKE %s
            ORDER BY s.fecha DESC LIMIT %s OFFSET %s""",
        (buscar, per_page, offset))
    return cur.fetchall(), total


def obtener(cur, salida_id):
    alm = almacen_db()
    cur.execute(
        f"""SELECT s.id, s.fecha, s.notas,
                   v.id AS venta_id, v.nombre AS cliente_nombre,
                   v.documento AS cliente_doc, v.total AS venta_total,
                   v.fecha AS venta_fecha
            FROM {alm}.salidas s
            LEFT JOIN ventas v ON s.venta_id=v.id
            WHERE s.id=%s""", (_entero(salida_id),))
    salida = cur.fetchone()
    if not salida:
        return None, []
    cur.execute(
        f"""SELECT ds.producto_id, ds.cantidad, pr.nombre AS producto_nombre,
                   pr.stock AS stock_actual
            FROM {alm}.detalle_salida ds
            LEFT JOIN {alm}.productos pr ON ds.producto_id=pr.id
            WHERE ds.salida_id=%s ORDER BY pr.nombre""",
        (_entero(salida_id),))
    return salida, cur.fetchall()


def _validar_items(cur, items):
    """Devuelve filas [{producto_id, cantidad}] o lanza si falta stock."""
    filas, errores = [], []
    for item in items or []:
        pid = _entero(item.get('producto_id'), -1)
        cant = _entero(item.get('cantidad'), 0)
        if pid <= 0 or cant <= 0:
            continue
        cur.execute(f"SELECT nombre, stock FROM {almacen_db()}.productos "
                    f"WHERE id=%s", (pid,))
        prod = cur.fetchone()
        if not prod:
            errores.append(f'ID {pid}: el producto no existe')
            continue
        stock_actual = _entero(prod.get('stock'))
        if stock_actual < cant:
            errores.append(f'{prod.get("nombre") or ("ID %s" % pid)}: stock '
                           f'insuficiente (hay {stock_actual}, necesitas '
                           f'{cant})')
            continue
        filas.append({'producto_id': pid, 'cantidad': cant})
    if errores:
        raise TiendaError('STOCK_INSUFICIENTE',
                          'No se puede registrar la salida: '
                          + '; '.join(errores), 409, {'errores': errores})
    if not filas:
        raise TiendaError('VALIDACION',
                          'Agrega al menos un producto con cantidad mayor a 0.',
                          422)
    return filas


def crear(cur, datos):
    from app import sincronizar_pedido_por_stock

    venta_id = datos.get('venta_id')
    vid = _entero(venta_id, 0) or None
    if vid is not None:
        cur.execute("SELECT id FROM ventas WHERE id=%s", (vid,))
        if not cur.fetchone():
            raise TiendaError('VENTA_NO_ENCONTRADA', 'La venta no existe.', 404)

    filas = _validar_items(cur, datos.get('items'))
    notas = str(datos.get('notas') or '').strip()

    cur.execute(
        f"INSERT INTO {almacen_db()}.salidas (venta_id, notas) VALUES (%s, %s)",
        (vid, notas))
    salida_id = cur.lastrowid
    for fila in filas:
        cur.execute(
            f"INSERT INTO {almacen_db()}.detalle_salida "
            f"(salida_id, producto_id, cantidad) VALUES (%s,%s,%s)",
            (salida_id, fila['producto_id'], fila['cantidad']))
        cur.execute(
            f"UPDATE {almacen_db()}.productos SET stock=stock-%s "
            f"WHERE id=%s AND stock>=%s",
            (fila['cantidad'], fila['producto_id'], fila['cantidad']))
        sincronizar_pedido_por_stock(cur, fila['producto_id'])
    return salida_id


def eliminar(cur, salida_id):
    from app import sincronizar_pedido_por_stock

    sid = _entero(salida_id)
    cur.execute(f"SELECT id FROM {almacen_db()}.salidas WHERE id=%s", (sid,))
    if not cur.fetchone():
        raise TiendaError('SALIDA_NO_ENCONTRADA', 'La salida no existe.', 404)

    cur.execute(
        f"SELECT producto_id, cantidad FROM {almacen_db()}.detalle_salida "
        f"WHERE salida_id=%s", (sid,))
    items = cur.fetchall()
    for item in items:
        cur.execute(
            f"UPDATE {almacen_db()}.productos SET stock=stock+%s WHERE id=%s",
            (item['cantidad'], item['producto_id']))
        sincronizar_pedido_por_stock(cur, item['producto_id'])
    cur.execute(f"DELETE FROM {almacen_db()}.detalle_salida WHERE salida_id=%s",
                (sid,))
    cur.execute(f"DELETE FROM {almacen_db()}.salidas WHERE id=%s", (sid,))
    return sid


def pdf_salida(cur, salida_id):
    """PDF binario de una salida. Devuelve (bytes, nombre)."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    salida, items = obtener(cur, salida_id)
    if not salida:
        raise TiendaError('SALIDA_NO_ENCONTRADA', 'La salida no existe.', 404)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Dato', parent=styles['Normal'], fontSize=9,
                              leading=12))
    styles.add(ParagraphStyle(name='Cabecera', parent=styles['Normal'],
                              fontSize=8, leading=10))

    def _fecha(valor):
        if not valor:
            return '-'
        return valor.strftime('%d/%m/%Y %H:%M') if hasattr(valor, 'strftime') \
            else str(valor)

    elems = [Paragraph('<b>MULTISERVICIOS RICHARD</b>', styles['Title']),
             Paragraph('Comprobante de salida', styles['Heading3']),
             Paragraph(f"Salida #{salida['id']}", styles['Dato']),
             Spacer(1, 0.2 * 72)]
    datos = [('Fecha', _fecha(salida['fecha']))]
    if salida.get('venta_id'):
        datos.append(('Venta', f"#{salida['venta_id']}"))
    if salida.get('cliente_nombre'):
        datos.append(('Cliente', salida['cliente_nombre']))
    if salida.get('cliente_doc'):
        datos.append(('Documento', salida['cliente_doc']))
    if salida.get('notas'):
        datos.append(('Notas', salida['notas']))
    elems.append(Table(
        [[Paragraph(f'<b>{k}</b>', styles['Dato']),
          Paragraph(str(v), styles['Dato'])] for k, v in datos],
        colWidths=[110, 390], hAlign='LEFT'))
    elems.append(Spacer(1, 0.2 * 72))

    filas = [[Paragraph(f'<b>{c}</b>', styles['Cabecera']) for c in
              ['Producto', 'Cantidad']]]
    total = 0
    for it in items:
        cant = _entero(it['cantidad'])
        total += cant
        filas.append([Paragraph(str(it.get('producto_nombre') or ''), styles['Dato']),
                      str(cant)])
    if items:
        filas.append([Paragraph('<b>Total</b>', styles['Dato']),
                      Paragraph(f'<b>{total}</b>', styles['Dato'])])
    tabla = Table(filas, colWidths=[420, 80], repeatRows=1)
    tabla.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f39c12')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    elems.append(tabla)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=36, bottomMargin=36,
                            title=f'Salida {salida_id}',
                            author='Multiservicios Richard')
    doc.build(elems)
    nombre = f'salida_{salida_id}_{_ahora_lima():%Y%m%d}.pdf'
    return buf.getvalue(), nombre
