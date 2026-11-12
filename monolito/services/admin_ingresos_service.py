"""Ingresos de productos (admin) — Fase 5.

Reutiliza las mismas reglas que `/ingresos`, `/registrar-ingreso`,
`/comprobante-ingreso/<id>`, `/ingresos/anular/<id>` y los PDF
`/ingresos/pdf/<id>` y `/ingresos/pdf` de la web:

  * al registrar un ingreso sube el stock, actualiza el precio de venta y
    reactiva el producto,
  * el nombre del proveedor y de cada producto se guardan como "foto" del
    momento del registro,
  * el ingreso no se borra: se anula (terminal) y revierte el stock que sumo,
  * los reportes PDF son semanal o mensual (resumen + detalle por ingreso).
"""
from datetime import date, datetime, timedelta, timezone

from .tienda_service import TiendaError, almacen_db

MOTIVOS_ANULACION = {
    'duplicado': 'Ingreso duplicado',
    'datos': 'Datos incorrectos (producto, cantidad o precio)',
    'devolucion': 'El proveedor devolvio la mercaderia',
    'carga': 'Error de carga',
    'otro': 'Otro motivo',
}

MESES_ES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
            'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')


def _ahora_lima():
    zona_lima = timezone(timedelta(hours=-5))
    return datetime.now(timezone.utc).astimezone(zona_lima).replace(tzinfo=None)


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _flotante(valor, defecto=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


# ─────────────────────────────────────────────
# FILTROS Y LECTURA
# ─────────────────────────────────────────────
def normalizar_filtros(args):
    def _get(nombre, defecto=''):
        valor = args.get(nombre, defecto)
        return str(valor).strip() if valor is not None else defecto

    estado = _get('estado', 'todos').lower()
    if estado not in ('todos', 'vigentes', 'anulados'):
        estado = 'todos'
    return {
        'buscar_proveedor': _get('buscar_proveedor')[:100],
        'buscar_producto': _get('buscar_producto')[:100],
        'fecha_desde': _get('fecha_desde')[:10],
        'fecha_hasta': _get('fecha_hasta')[:10],
        'estado': estado,
    }


def _where(filtros, alias='i'):
    partes, params = [], []
    if filtros.get('buscar_proveedor'):
        partes.append(f"COALESCE(NULLIF({alias}.proveedor_nombre,''), p.nombre) "
                      f"LIKE %s")
        params.append(f"%{filtros['buscar_proveedor']}%")
    if filtros.get('buscar_producto'):
        partes.append(
            f"EXISTS (SELECT 1 FROM {almacen_db()}.detalle_ingreso di2 "
            f"LEFT JOIN {almacen_db()}.productos pr ON di2.producto_id=pr.id "
            f"WHERE di2.ingreso_id={alias}.id AND "
            f"COALESCE(NULLIF(di2.producto_nombre,''), pr.nombre) LIKE %s)")
        params.append(f"%{filtros['buscar_producto']}%")
    if filtros.get('fecha_desde'):
        partes.append(f"DATE({alias}.fecha) >= %s")
        params.append(filtros['fecha_desde'])
    if filtros.get('fecha_hasta'):
        partes.append(f"DATE({alias}.fecha) <= %s")
        params.append(filtros['fecha_hasta'])
    if filtros.get('estado') == 'vigentes':
        partes.append(f"COALESCE({alias}.estado,'vigente') <> 'anulado'")
    elif filtros.get('estado') == 'anulados':
        partes.append(f"{alias}.estado = 'anulado'")
    where = ("WHERE " + " AND ".join(partes)) if partes else ""
    return where, params


_SELECT_LISTA = """
    SELECT i.id, i.fecha, i.notas, COALESCE(i.estado,'vigente') AS estado,
           i.motivo_anulacion, i.anulado_en, i.anulado_por,
           COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
           COALESCE(NULLIF(i.proveedor_documento,''),
                    CONCAT_WS(' ', p.tipo_documento, p.numero_documento))
               AS proveedor_documento,
           p.categoria AS proveedor_categoria,
           COUNT(DISTINCT di.producto_id) AS productos_distintos,
           COALESCE(SUM(di.cantidad), 0) AS total_cantidad,
           COALESCE(SUM(di.cantidad * di.precio_compra), 0) AS total_compra,
           COALESCE(SUM(di.cantidad * di.precio_venta), 0) AS total_venta
    FROM {alm}.ingresos i
    LEFT JOIN {alm}.proveedores p ON i.proveedor_id=p.id
    LEFT JOIN {alm}.detalle_ingreso di ON di.ingreso_id=i.id
"""


def listar(cur, filtros, page=1, per_page=20):
    where, params = _where(filtros)
    alm = almacen_db()
    cur.execute(
        f"SELECT COUNT(*) AS total FROM {alm}.ingresos i "
        f"LEFT JOIN {alm}.proveedores p ON i.proveedor_id=p.id {where}",
        tuple(params))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        _SELECT_LISTA.format(alm=alm) + f"{where} "
        f"GROUP BY i.id, i.fecha, i.notas, i.estado, i.motivo_anulacion, "
        f"i.anulado_en, i.anulado_por, i.proveedor_nombre, p.nombre, "
        f"p.tipo_documento, p.numero_documento, p.categoria "
        f"ORDER BY i.fecha DESC LIMIT %s OFFSET %s",
        tuple(params) + (per_page, offset))
    return cur.fetchall(), total


def _datos_ingreso(cur, ingreso_id):
    alm = almacen_db()
    cur.execute(
        f"""SELECT i.id, i.fecha, i.notas, COALESCE(i.estado,'vigente') AS estado,
                   i.motivo_anulacion, i.anulado_en, i.anulado_por,
                   COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre)
                       AS proveedor_nombre,
                   COALESCE(NULLIF(i.proveedor_documento,''),
                            CONCAT_WS(' ', p.tipo_documento, p.numero_documento))
                       AS proveedor_documento,
                   p.celular AS proveedor_celular,
                   p.categoria AS proveedor_categoria
            FROM {alm}.ingresos i
            LEFT JOIN {alm}.proveedores p ON i.proveedor_id=p.id
            WHERE i.id=%s""", (_entero(ingreso_id),))
    ingreso = cur.fetchone()
    if not ingreso:
        return None, []
    cur.execute(
        f"""SELECT di.cantidad, di.precio_compra, di.precio_venta,
                   COALESCE(NULLIF(di.producto_nombre,''), pr.nombre)
                       AS producto_nombre,
                   pr.stock AS stock_actual
            FROM {alm}.detalle_ingreso di
            LEFT JOIN {alm}.productos pr ON di.producto_id=pr.id
            WHERE di.ingreso_id=%s
            ORDER BY producto_nombre""", (_entero(ingreso_id),))
    items = cur.fetchall()
    for item in items:
        item['subtotal_compra'] = _flotante(item['cantidad']) \
            * _flotante(item['precio_compra'])
    return ingreso, items


def obtener(cur, ingreso_id):
    ingreso, items = _datos_ingreso(cur, ingreso_id)
    if not ingreso:
        return None, [], {}
    total_compra = sum(_entero(i['cantidad']) * _flotante(i['precio_compra'])
                       for i in items)
    total_venta = sum(_entero(i['cantidad']) * _flotante(i['precio_venta'])
                      for i in items)
    totales = {'total_compra': total_compra, 'total_venta': total_venta,
               'total_cantidad': sum(_entero(i['cantidad']) for i in items),
               'productos_distintos': len(items)}
    return ingreso, items, totales


def _anios_ingresos(cur):
    actual = _ahora_lima().year
    try:
        cur.execute(f"SELECT MIN(YEAR(fecha)) AS minimo FROM "
                    f"{almacen_db()}.ingresos")
        minimo = _entero((cur.fetchone() or {}).get('minimo'), actual) or actual
    except Exception:
        minimo = actual
    minimo = min(minimo, actual)
    return list(range(actual, minimo - 1, -1))


def catalogo_periodos(cur):
    """Anios y motivos para el selector de filtros/PDF."""
    return {'anios': _anios_ingresos(cur), 'meses': list(MESES_ES),
            'motivos': MOTIVOS_ANULACION}


# ─────────────────────────────────────────────
# ESCRITURA
# ─────────────────────────────────────────────
def _validar_items(items):
    filas, vistos, errores = [], set(), []
    for i, item in enumerate(items or []):
        n = i + 1
        fallos = []
        pid = _entero(item.get('producto_id'), -1)
        cant = _entero(item.get('cantidad'), -1)
        pc = _flotante(item.get('precio_compra'), -1)
        pv = _flotante(item.get('precio_venta'), -1)
        if pid <= 0:
            fallos.append('producto invalido')
        if cant <= 0:
            fallos.append('cantidad debe ser mayor a 0')
        if pc < 0:
            fallos.append('precio de compra invalido')
        if pv <= 0:
            fallos.append('precio de venta debe ser mayor a 0')
        if pid in vistos:
            fallos.append('producto repetido en el ingreso')
        if fallos:
            errores.append(f'Fila {n}: ' + ', '.join(fallos) + '.')
            continue
        vistos.add(pid)
        filas.append({'producto_id': pid, 'cantidad': cant,
                      'precio_compra': pc, 'precio_venta': pv})
    if not filas and not errores:
        errores.append('Agrega al menos un producto al ingreso.')
    if errores:
        raise TiendaError('VALIDACION', ' '.join(errores), 422,
                          {'errores': errores})
    return filas


def crear(cur, datos):
    """Registra un ingreso: repone stock y actualiza precio de venta."""
    from app import sincronizar_pedido_por_stock

    proveedor_id = datos.get('proveedor_id')
    pid = _entero(proveedor_id, -1)
    if not proveedor_id or pid <= 0:
        raise TiendaError('VALIDACION', 'Debes seleccionar un proveedor.', 422,
                          {'campo': 'proveedor_id'})

    filas = _validar_items(datos.get('items'))
    notas = str(datos.get('notas') or '').strip()

    cur.execute(
        f"SELECT nombre, tipo_documento, numero_documento "
        f"FROM {almacen_db()}.proveedores WHERE id=%s", (pid,))
    prov = cur.fetchone()
    if not prov:
        raise TiendaError('PROVEEDOR_NO_ENCONTRADO',
                          'El proveedor seleccionado no existe.', 404)
    prov_nombre = prov.get('nombre') or ''
    prov_documento = ''
    if prov.get('numero_documento'):
        prov_documento = f"{prov.get('tipo_documento') or ''} " \
                         f"{prov['numero_documento']}".strip()

    ids = [f['producto_id'] for f in filas]
    cur.execute(
        f"SELECT id, nombre FROM {almacen_db()}.productos "
        f"WHERE id IN ({','.join(['%s'] * len(ids))})", tuple(ids))
    nombres = {r['id']: r['nombre'] for r in cur.fetchall()}

    cur.execute(
        f"""INSERT INTO {almacen_db()}.ingresos
            (proveedor_id, notas, fecha, estado, proveedor_nombre,
             proveedor_documento)
            VALUES (%s,%s,%s,'vigente',%s,%s)""",
        (pid, notas, _ahora_lima(), prov_nombre, prov_documento))
    ingreso_id = cur.lastrowid

    for fila in filas:
        cur.execute(
            f"""INSERT INTO {almacen_db()}.detalle_ingreso
                (ingreso_id, producto_id, producto_nombre, cantidad,
                 precio_compra, precio_venta)
                VALUES (%s,%s,%s,%s,%s,%s)""",
            (ingreso_id, fila['producto_id'],
             nombres.get(fila['producto_id'], ''), fila['cantidad'],
             fila['precio_compra'], fila['precio_venta']))
        cur.execute(
            f"UPDATE {almacen_db()}.productos "
            f"SET stock=stock+%s, precio=%s, estado='activo' WHERE id=%s",
            (fila['cantidad'], fila['precio_venta'], fila['producto_id']))

    for fila in filas:
        sincronizar_pedido_por_stock(cur, fila['producto_id'])
    return ingreso_id


def anular(cur, ingreso_id, motivo, motivo_detalle, admin):
    """Anula un ingreso (terminal) y revierte el stock que sumo."""
    from app import sincronizar_pedido_por_stock

    iid = _entero(ingreso_id)
    motivo = (motivo or '').strip().lower()
    detalle = ' '.join(str(motivo_detalle or '').split())
    if motivo not in MOTIVOS_ANULACION:
        raise TiendaError('VALIDACION', 'Selecciona un motivo de anulacion.',
                          422, {'campo': 'motivo'})
    if motivo == 'otro' and len(detalle) < 5:
        raise TiendaError('VALIDACION',
                          'Cuando eliges "Otro motivo" hay que describirlo.',
                          422, {'campo': 'motivo_detalle'})
    texto = MOTIVOS_ANULACION[motivo]
    if detalle:
        texto = f'{texto}: {detalle}' if motivo == 'otro' \
            else f'{texto} ({detalle})'
    texto = texto[:255]

    cur.execute(
        f"SELECT COALESCE(estado,'vigente') AS estado FROM "
        f"{almacen_db()}.ingresos WHERE id=%s", (iid,))
    ingreso = cur.fetchone()
    if not ingreso:
        raise TiendaError('INGRESO_NO_ENCONTRADO', 'El ingreso no existe.', 404)
    if ingreso['estado'] == 'anulado':
        raise TiendaError('INGRESO_ANULADO',
                          f'El ingreso #{iid} ya esta anulado.', 409)

    cur.execute(
        f"SELECT producto_id, cantidad FROM {almacen_db()}.detalle_ingreso "
        f"WHERE ingreso_id=%s", (iid,))
    items = cur.fetchall()

    revertidos = faltaron = 0
    for item in items:
        cant = _entero(item['cantidad'])
        cur.execute(f"SELECT stock FROM {almacen_db()}.productos WHERE id=%s",
                    (item['producto_id'],))
        prod = cur.fetchone()
        actual = _entero(prod['stock']) if prod else 0
        if actual >= cant:
            cur.execute(
                f"UPDATE {almacen_db()}.productos SET stock=stock-%s "
                f"WHERE id=%s", (cant, item['producto_id']))
        else:
            cur.execute(f"UPDATE {almacen_db()}.productos SET stock=0 "
                        f"WHERE id=%s", (item['producto_id'],))
            faltaron += 1
        revertidos += 1

    for item in items:
        sincronizar_pedido_por_stock(cur, item['producto_id'])

    cur.execute(
        f"""UPDATE {almacen_db()}.ingresos
            SET estado='anulado', motivo_anulacion=%s, anulado_en=%s,
                anulado_por=%s
            WHERE id=%s AND COALESCE(estado,'vigente')='vigente'""",
        (texto, _ahora_lima(), admin or 'administrador', iid))
    if cur.rowcount != 1:
        raise TiendaError('INGRESO_ANULADO',
                          f'El ingreso #{iid} ya estaba anulado.', 409)
    return {'id': iid, 'estado': 'anulado', 'motivo_anulacion': texto,
            'revertidos': revertidos, 'faltaron': faltaron}


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────
def _datos_ingresos_pdf(cur, desde, hasta):
    alm = almacen_db()
    cur.execute(
        f"""SELECT i.id, i.fecha, i.notas, COALESCE(i.estado,'vigente') AS estado,
                   i.motivo_anulacion,
                   COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre)
                       AS proveedor_nombre,
                   p.categoria AS proveedor_categoria,
                   COUNT(DISTINCT di.producto_id) AS productos_distintos,
                   COALESCE(SUM(di.cantidad), 0) AS total_cantidad,
                   COALESCE(SUM(di.cantidad * di.precio_compra), 0)
                       AS total_compra
            FROM {alm}.ingresos i
            LEFT JOIN {alm}.proveedores p ON i.proveedor_id=p.id
            LEFT JOIN {alm}.detalle_ingreso di ON di.ingreso_id=i.id
            WHERE DATE(i.fecha) BETWEEN %s AND %s
            GROUP BY i.id, i.fecha, i.notas, i.estado, i.motivo_anulacion,
                     i.proveedor_nombre, p.nombre, p.categoria
            ORDER BY i.fecha ASC, i.id ASC""",
        (desde, hasta))
    ingresos = cur.fetchall()
    detalles = {}
    if ingresos:
        ids = [r['id'] for r in ingresos]
        cur.execute(
            f"""SELECT di.ingreso_id, di.cantidad, di.precio_compra,
                       di.precio_venta,
                       COALESCE(NULLIF(di.producto_nombre,''), pr.nombre)
                           AS producto_nombre
                FROM {alm}.detalle_ingreso di
                LEFT JOIN {alm}.productos pr ON di.producto_id=pr.id
                WHERE di.ingreso_id IN ({','.join(['%s'] * len(ids))})
                ORDER BY di.ingreso_id, producto_nombre""",
            tuple(ids))
        for d in cur.fetchall():
            detalles.setdefault(d['ingreso_id'], []).append(d)
    return ingresos, detalles


def pdf_ingreso(cur, ingreso_id):
    """PDF binario de un ingreso. Devuelve (bytes, nombre)."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, \
        TableStyle
    from app import _pdf_encabezado, _pdf_estilos, _pdf_fecha, \
        _pdf_tabla_productos

    ingreso, items, totales = obtener(cur, ingreso_id)
    if not ingreso:
        raise TiendaError('INGRESO_NO_ENCONTRADO', 'El ingreso no existe.', 404)

    styles = _pdf_estilos()
    anulado = (ingreso['estado'] or 'vigente') == 'anulado'
    datos = [('Fecha', _pdf_fecha(ingreso['fecha'])),
             ('Estado', 'ANULADO' if anulado else 'Vigente'),
             ('Proveedor', ingreso['proveedor_nombre'] or 'Sin proveedor')]
    if ingreso['proveedor_documento']:
        datos.append(('Documento', ingreso['proveedor_documento']))
    if ingreso['proveedor_celular']:
        datos.append(('Celular', ingreso['proveedor_celular']))
    if ingreso['proveedor_categoria']:
        datos.append(('Categoria', ingreso['proveedor_categoria']))
    if anulado and ingreso['motivo_anulacion']:
        datos.append(('Motivo de anulacion', ingreso['motivo_anulacion']))
    if anulado and ingreso['anulado_en']:
        datos.append(('Anulado', f"{_pdf_fecha(ingreso['anulado_en'])} - "
                                 f"{ingreso['anulado_por'] or '-'}"))
    if ingreso['notas']:
        datos.append(('Notas', ingreso['notas']))

    elems = _pdf_encabezado(styles, 'Comprobante de ingreso',
                            f"Ingreso #{ingreso['id']}", datos)
    if anulado:
        elems.append(Paragraph('<b>INGRESO ANULADO - no cuenta en los totales '
                               'del almacen.</b>', styles['Anulado']))
        elems.append(Spacer(1, 0.12 * 72))
    tabla, total_compra, total_venta = _pdf_tabla_productos(styles, items)
    elems.append(Paragraph('<b>Productos ingresados</b>',
                           styles['TituloSeccion']))
    elems.append(tabla)
    elems.append(Spacer(1, 0.18 * 72))

    resumen = Table([
        ['Productos distintos', str(len(items))],
        ['Cantidad total', str(totales['total_cantidad'])],
        ['Total compra', f'S/ {total_compra:.2f}'],
        ['Total venta (estimado)', f'S/ {total_venta:.2f}'],
    ], colWidths=[180, 140], hAlign='LEFT')
    resumen.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elems.append(resumen)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=36, bottomMargin=36,
                            title=f'Ingreso {ingreso_id}',
                            author='Multiservicios Richard')
    doc.build(elems)
    nombre = f'ingreso_{ingreso_id}_{_ahora_lima():%Y%m%d}.pdf'
    return buf.getvalue(), nombre


def pdf_periodo(cur, periodo, anio=None, mes=None, semana=None):
    """PDF binario de los ingresos de una semana o mes. (bytes, nombre)."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, \
        Spacer
    from app import _bloques_semana_mes, _periodo_ingresos, _pdf_encabezado, \
        _pdf_estilos, _pdf_fecha, _pdf_tabla_productos, _pdf_tabla_resumen

    periodo = 'semana' if str(periodo or '').strip().lower() == 'semana' \
        else 'mes'
    hoy = _ahora_lima().date()
    invalido = 'El periodo seleccionado no es valido.'

    referencia, numero = None, None
    if anio not in (None, '') or mes not in (None, ''):
        try:
            a = int(anio) if anio not in (None, '') else hoy.year
            m = int(mes) if mes not in (None, '') else hoy.month
            if not (1 <= m <= 12 and 1900 <= a <= 9999):
                raise ValueError
            referencia = date(a, m, 1)
        except (TypeError, ValueError):
            raise TiendaError('VALIDACION', invalido, 422,
                              {'campo': 'periodo'})
    if semana not in (None, ''):
        try:
            numero = int(semana)
        except (TypeError, ValueError):
            raise TiendaError('VALIDACION', invalido, 422,
                              {'campo': 'semana'})
    if periodo == 'semana' and referencia is not None and numero is not None:
        bloques = _bloques_semana_mes(referencia.year, referencia.month)
        if not 1 <= numero <= len(bloques):
            raise TiendaError('VALIDACION', invalido, 422, {'campo': 'semana'})

    desde, hasta, etiqueta = _periodo_ingresos(periodo, referencia, numero)
    ingresos, detalles = _datos_ingresos_pdf(cur, desde, hasta)
    if not ingresos:
        raise TiendaError('SIN_DATOS',
                          'No hay ingresos registrados en el periodo '
                          'seleccionado.', 404)

    styles = _pdf_estilos()
    vigentes = [r for r in ingresos if (r['estado'] or 'vigente') != 'anulado']
    n_anulados = len(ingresos) - len(vigentes)
    titulo = ('Reporte de ingresos semanal' if periodo == 'semana'
              else 'Reporte de ingresos mensual')
    resumen_cab = f"{len(vigentes)} vigente(s)"
    if n_anulados:
        resumen_cab += f" y {n_anulados} anulado(s)"
    elems = _pdf_encabezado(styles, titulo, etiqueta, [
        ('Periodo', f"{desde.strftime('%d/%m/%Y')} al "
                    f"{hasta.strftime('%d/%m/%Y')}"),
        ('Ingresos', resumen_cab),
        ('Generado', _ahora_lima().strftime('%d/%m/%Y %H:%M')),
    ])
    elems.append(Paragraph('<b>Resumen</b>', styles['TituloSeccion']))
    elems.append(_pdf_tabla_resumen(styles, ingresos))
    if n_anulados:
        elems.append(Spacer(1, 0.06 * 72))
        elems.append(Paragraph(f'Los {n_anulados} ingreso(s) anulado(s) no '
                               'incluyen en los totales.', styles['Anulado']))
    elems.append(Spacer(1, 0.2 * 72))
    elems.append(Paragraph('<b>Detalle por ingreso</b>',
                           styles['TituloSeccion']))
    for r in ingresos:
        items = detalles.get(r['id'], [])
        anulado = (r['estado'] or 'vigente') == 'anulado'
        encabezado = (('ANULADO - ' if anulado else '') +
                      f"Ingreso #{r['id']} - {_pdf_fecha(r['fecha'])} - "
                      f"{r['proveedor_nombre'] or 'Sin proveedor'}")
        bloque = [Paragraph('<b>' + encabezado.replace('&', '&amp;')
                            .replace('<', '&lt;').replace('>', '&gt;') + '</b>',
                            styles['TituloAnulado'] if anulado
                            else styles['TituloSeccion'])]
        if anulado and r['motivo_anulacion']:
            bloque.append(Paragraph('Motivo de anulacion: '
                                    + str(r['motivo_anulacion']), styles['Dato']))
        if r['notas']:
            bloque.append(Paragraph('Notas: ' + str(r['notas']),
                                    styles['Dato']))
        bloque.append(_pdf_tabla_productos(styles, items)[0])
        elems.append(KeepTogether(bloque))
        elems.append(Spacer(1, 0.1 * 72))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=36, bottomMargin=36, title=titulo,
                            author='Multiservicios Richard')
    doc.build(elems)
    nombre = (f"ingresos_{periodo}_{desde.strftime('%Y%m%d')}_"
              f"{hasta.strftime('%Y%m%d')}.pdf")
    return buf.getvalue(), nombre
