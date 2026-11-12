"""Proveedores y pedidos a proveedor (admin) — Fase 5.

Reutiliza las mismas reglas que las pantallas `/proveedores`,
`/proveedores/editar/<id>` y `/productos-para-pedir` de la web:

  * un proveedor puede ser persona (DNI) o empresa (RUC) y el nombre completo
    se guarda ya armado en `nombre`,
  * no se admiten dos filas con el mismo documento + categoria,
  * la lista de Pedidos solo muestra los `pendiente`; un pedido con proveedor
    borrado cuenta como "sin asignar",
  * solo se puede asignar un proveedor de la misma categoria que el producto,
  * "marcar enviado" cierra TODOS los pendientes de un proveedor.
"""
import re

from .tienda_service import TiendaError, almacen_db

_TIPOS_DOCUMENTO = {'dni': 8, 'ruc': 11}
_RE_NOMBRE_PERSONA = re.compile(
    r'^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*$'
)
_RE_RAZON_SOCIAL = re.compile(
    r"^[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ&.\-'’,() ]+$"
)


def _texto(valor):
    if valor is None:
        return ''
    return str(valor).strip()


def _espacios(valor):
    """Colapsa los espacios internos (mismo criterio que la web)."""
    return ' '.join(_texto(valor).split())


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


# ─────────────────────────────────────────────
# VALIDACION
# ─────────────────────────────────────────────
def validar(datos):
    """Valida y normaliza un proveedor. Lanza TiendaError(422) si falla."""
    tipo = _texto(datos.get('tipo_documento')).lower() or 'dni'
    numero = re.sub(r'\D', '', _texto(datos.get('numero_documento')))
    celular = _texto(datos.get('celular'))
    direccion = _texto(datos.get('direccion'))
    categoria = _texto(datos.get('categoria'))
    notas = _texto(datos.get('notas'))

    if tipo not in _TIPOS_DOCUMENTO:
        raise TiendaError('VALIDACION',
                          'Selecciona un tipo de documento valido (DNI o RUC).',
                          422, {'campo': 'tipo_documento'})
    esperado = _TIPOS_DOCUMENTO[tipo]
    if len(numero) != esperado:
        raise TiendaError(
            'VALIDACION',
            f'El {tipo.upper()} debe tener exactamente {esperado} numeros.',
            422, {'campo': 'numero_documento'})

    if tipo == 'dni':
        nombres = _espacios(datos.get('nombres'))
        ap_paterno = _espacios(datos.get('apellido_paterno'))
        ap_materno = _espacios(datos.get('apellido_materno'))
        for etiqueta, valor in (('nombres', nombres),
                                ('apellido_paterno', ap_paterno),
                                ('apellido_materno', ap_materno)):
            if not valor:
                raise TiendaError('VALIDACION',
                                  f'El campo {etiqueta} es obligatorio.',
                                  422, {'campo': etiqueta})
            if not _RE_NOMBRE_PERSONA.match(valor) \
                    or sum(c.isalpha() for c in valor) < 2:
                raise TiendaError(
                    'VALIDACION',
                    f'El campo {etiqueta} solo puede contener letras y '
                    'espacios (sin numeros ni signos).',
                    422, {'campo': etiqueta})
        nombre = f'{nombres} {ap_paterno} {ap_materno}'[:200]
    else:
        nombres = _espacios(datos.get('razon_social'))
        ap_paterno = ap_materno = ''
        if not nombres:
            raise TiendaError('VALIDACION', 'La razon social es obligatoria.',
                              422, {'campo': 'razon_social'})
        if not _RE_RAZON_SOCIAL.match(nombres) \
                or sum(c.isalpha() for c in nombres) < 3:
            raise TiendaError(
                'VALIDACION',
                "La razon social solo puede contener letras, numeros y los "
                "signos . & - ' , ( ).",
                422, {'campo': 'razon_social'})
        nombres = nombres[:120]
        nombre = nombres[:200]

    if not celular:
        raise TiendaError('VALIDACION', 'El celular es obligatorio.', 422,
                          {'campo': 'celular'})
    if not re.fullmatch(r'\d{9}', celular):
        raise TiendaError('VALIDACION',
                          'El celular debe tener exactamente 9 numeros '
                          '(solo digitos).', 422, {'campo': 'celular'})
    if not direccion:
        raise TiendaError('VALIDACION', 'La direccion es obligatoria.', 422,
                          {'campo': 'direccion'})
    if not categoria:
        raise TiendaError('VALIDACION', 'La categoria es obligatoria.', 422,
                          {'campo': 'categoria'})

    return {
        'tipo_documento': tipo.upper(),
        'numero_documento': numero,
        'nombres': nombres,
        'apellido_paterno': ap_paterno,
        'apellido_materno': ap_materno,
        'razon_social': nombres if tipo == 'ruc' else '',
        'nombre': nombre,
        'celular': celular,
        'direccion': direccion,
        'categoria': categoria,
        'notas': notas,
    }


def _duplicado(cur, datos, excluir_id=None):
    sql = (f"SELECT id FROM {almacen_db()}.proveedores "
           f"WHERE tipo_documento=%s AND numero_documento=%s AND categoria=%s")
    params = [datos['tipo_documento'], datos['numero_documento'],
              datos['categoria']]
    if excluir_id is not None:
        sql += " AND id<>%s"
        params.append(_entero(excluir_id))
    cur.execute(sql + " LIMIT 1", tuple(params))
    return cur.fetchone() is not None


# ─────────────────────────────────────────────
# LECTURA
# ─────────────────────────────────────────────
def normalizar_filtros(args):
    def _get(nombre, defecto=''):
        valor = args.get(nombre, defecto)
        return _texto(valor)[:100] if valor is not None else defecto
    return {'buscar': _get('buscar'), 'categoria': _get('categoria')}


def listar(cur, filtros, page=1, per_page=20):
    where = "WHERE nombre LIKE %s"
    params = [f"%{filtros.get('buscar') or ''}%"]
    if filtros.get('categoria'):
        where += " AND categoria=%s"
        params.append(filtros['categoria'])

    cur.execute(f"SELECT COUNT(*) AS total FROM {almacen_db()}.proveedores "
                f"{where}", tuple(params))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"SELECT * FROM {almacen_db()}.proveedores {where} "
        f"ORDER BY nombre ASC LIMIT %s OFFSET %s",
        tuple(params) + (per_page, offset))
    return cur.fetchall(), total


def obtener(cur, proveedor_id):
    cur.execute(f"SELECT * FROM {almacen_db()}.proveedores WHERE id=%s",
                (_entero(proveedor_id),))
    return cur.fetchone()


# ─────────────────────────────────────────────
# ESCRITURA
# ─────────────────────────────────────────────
def crear(cur, datos):
    limpio = validar(datos)
    if _duplicado(cur, limpio):
        raise TiendaError('PROVEEDOR_DUPLICADO',
                          'Ya hay un proveedor agregado con este nombre y '
                          'categoria.', 409)
    cur.execute(
        f"""INSERT INTO {almacen_db()}.proveedores
            (nombre, tipo_documento, numero_documento, nombres,
             apellido_paterno, apellido_materno, celular, direccion,
             categoria, notas)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (limpio['nombre'], limpio['tipo_documento'], limpio['numero_documento'],
         limpio['nombres'], limpio['apellido_paterno'],
         limpio['apellido_materno'], limpio['celular'], limpio['direccion'],
         limpio['categoria'], limpio['notas']))
    return cur.lastrowid


def actualizar(cur, proveedor_id, datos):
    actual = obtener(cur, proveedor_id)
    if not actual:
        raise TiendaError('PROVEEDOR_NO_ENCONTRADO', 'El proveedor no existe.',
                          404)
    limpio = validar(datos)
    sin_cambio = (
        (actual.get('tipo_documento') or '').upper() == limpio['tipo_documento']
        and (actual.get('numero_documento') or '') == limpio['numero_documento']
        and (actual.get('categoria') or '') == limpio['categoria'])
    if not sin_cambio and _duplicado(cur, limpio, excluir_id=proveedor_id):
        raise TiendaError('PROVEEDOR_DUPLICADO',
                          'Ya hay un proveedor agregado con este nombre y '
                          'categoria.', 409)
    cur.execute(
        f"""UPDATE {almacen_db()}.proveedores SET
                nombre=%s, tipo_documento=%s, numero_documento=%s, nombres=%s,
                apellido_paterno=%s, apellido_materno=%s, celular=%s,
                direccion=%s, categoria=%s, notas=%s
            WHERE id=%s""",
        (limpio['nombre'], limpio['tipo_documento'], limpio['numero_documento'],
         limpio['nombres'], limpio['apellido_paterno'],
         limpio['apellido_materno'], limpio['celular'], limpio['direccion'],
         limpio['categoria'], limpio['notas'], _entero(proveedor_id)))
    return _entero(proveedor_id)


def eliminar(cur, proveedor_id):
    pid = _entero(proveedor_id)
    if not obtener(cur, pid):
        raise TiendaError('PROVEEDOR_NO_ENCONTRADO', 'El proveedor no existe.',
                          404)
    cur.execute(f"DELETE FROM {almacen_db()}.proveedores WHERE id=%s", (pid,))
    return pid


# ─────────────────────────────────────────────
# PEDIDOS A PROVEEDOR (productos_para_pedir)
# ─────────────────────────────────────────────
def _whatsapp_url(celular, mensaje):
    from urllib.parse import quote
    numero = ''.join(filter(str.isdigit, celular or ''))
    if not numero.startswith('51'):
        numero = '51' + numero
    return f"https://wa.me/{numero}?text={quote(mensaje)}"


def pedidos(cur, filtro='todos'):
    """Pedidos pendientes con sus grupos por proveedor (y URL de WhatsApp)."""
    filtro = (filtro or 'todos').strip().lower()
    if filtro not in ('todos', 'sin_asignar'):
        filtro = 'todos'
    sin_asignar_sql = "(pp.proveedor_id IS NULL OR pr.id IS NULL)"

    cur.execute(
        f"""SELECT pp.id, pp.producto_id, pp.cantidad_pedido, pp.fecha,
                   pp.estado, pp.proveedor_id AS proveedor_asignado,
                   p.nombre AS producto_nombre, p.stock AS stock_actual,
                   p.categoria,
                   pr.nombre AS proveedor_nombre,
                   pr.celular AS proveedor_celular
            FROM {almacen_db()}.productos_para_pedir pp
            JOIN {almacen_db()}.productos p ON pp.producto_id=p.id
            LEFT JOIN {almacen_db()}.proveedores pr ON pp.proveedor_id=pr.id
            WHERE pp.estado='pendiente'
              {'AND ' + sin_asignar_sql if filtro == 'sin_asignar' else ''}
            ORDER BY (pp.proveedor_id IS NULL) DESC,
                     pr.nombre IS NULL DESC, pr.nombre ASC, p.nombre ASC""")
    pedidos_rows = list(cur.fetchall())

    cur.execute(
        f"""SELECT COUNT(*) AS n
            FROM {almacen_db()}.productos_para_pedir pp
            LEFT JOIN {almacen_db()}.proveedores pr ON pp.proveedor_id=pr.id
            WHERE pp.estado='pendiente' AND {sin_asignar_sql}""")
    sin_asignar = _entero((cur.fetchone() or {}).get('n'))

    for ped in pedidos_rows:
        ped['colgado'] = bool(ped['proveedor_asignado']) \
            and not ped['proveedor_nombre']
        if ped['colgado']:
            ped['proveedor_asignado'] = None

    pedidos_rows.sort(key=lambda ped: (
        0 if not ped['proveedor_asignado'] else 1,
        (ped['proveedor_nombre'] or '').lower(),
        int(ped['stock_actual'] or 0),
        (ped['producto_nombre'] or '').strip().lower()))

    categorias = sorted({p['categoria'] for p in pedidos_rows if p['categoria']})
    por_categoria = {c: [] for c in categorias}
    if categorias:
        cur.execute(
            f"""SELECT id, nombre, celular, categoria
                FROM {almacen_db()}.proveedores
                WHERE categoria IN ({','.join(['%s'] * len(categorias))})
                ORDER BY nombre""", tuple(categorias))
        for pr in cur.fetchall():
            por_categoria.setdefault(pr['categoria'], []).append(pr)

    for ped in pedidos_rows:
        ped['proveedores'] = por_categoria.get(ped['categoria'] or '', [])
        ped['sin_proveedor'] = not ped['proveedores']

    grupos, indice = [], {}
    for ped in pedidos_rows:
        clave = ped['proveedor_asignado'] or 'sin_proveedor'
        if clave not in indice:
            indice[clave] = {
                'proveedor_id': ped['proveedor_asignado'],
                'proveedor_nombre': ped['proveedor_nombre'] or 'Sin proveedor',
                'proveedor_celular': ped['proveedor_celular'] or '',
                'productos': [],
            }
            grupos.append(indice[clave])
        indice[clave]['productos'].append(ped)

    for grupo in grupos:
        if grupo['proveedor_celular']:
            lista = "\n".join([
                f"- {p['producto_nombre']} x{p['cantidad_pedido']} "
                f"(Stock actual: {p['stock_actual']})"
                for p in grupo['productos']])
            msg = (f"Hola {grupo['proveedor_nombre']}, necesitamos reponer los "
                   f"siguientes productos:\n\n{lista}\n\n"
                   f"Gracias - Multiservicios Richard")
            grupo['whatsapp_url'] = _whatsapp_url(grupo['proveedor_celular'],
                                                  msg)
        else:
            grupo['whatsapp_url'] = None
        grupo['total_productos'] = len(grupo['productos'])

    return {'pedidos': pedidos_rows, 'grupos': grupos,
            'sin_asignar': sin_asignar, 'filtro': filtro}


def _pedido_pendiente(cur, pedido_id):
    cur.execute(
        f"""SELECT pp.id, pp.producto_id, pp.cantidad_pedido, pp.estado,
                   p.categoria
            FROM {almacen_db()}.productos_para_pedir pp
            JOIN {almacen_db()}.productos p ON pp.producto_id=p.id
            WHERE pp.id=%s AND pp.estado='pendiente'""",
        (_entero(pedido_id),))
    return cur.fetchone()


def actualizar_cantidad(cur, pedido_id, cantidad):
    cantidad = max(1, _entero(cantidad, 1))
    cur.execute(
        f"UPDATE {almacen_db()}.productos_para_pedir SET cantidad_pedido=%s "
        f"WHERE id=%s", (cantidad, _entero(pedido_id)))
    if cur.rowcount != 1:
        raise TiendaError('PEDIDO_NO_ENCONTRADO',
                          'El pedido no existe o ya no esta pendiente.', 404)
    return cantidad


def asignar_proveedor(cur, pedido_id, proveedor_id):
    """Asigna (o quita, con proveedor_id nulo) el proveedor de un pedido."""
    pedido = _pedido_pendiente(cur, pedido_id)
    if not pedido:
        raise TiendaError('PEDIDO_NO_ENCONTRADO',
                          'El pedido no existe o ya no esta pendiente.', 404)

    if proveedor_id in (None, '', 'null'):
        cur.execute(
            f"UPDATE {almacen_db()}.productos_para_pedir SET proveedor_id=NULL "
            f"WHERE id=%s", (_entero(pedido_id),))
        return None

    pid = _entero(proveedor_id, -1)
    if pid <= 0:
        raise TiendaError('VALIDACION', 'Proveedor no valido.', 422,
                          {'campo': 'proveedor_id'})
    cur.execute(
        f"SELECT nombre, celular, categoria FROM {almacen_db()}.proveedores "
        f"WHERE id=%s", (pid,))
    prov = cur.fetchone()
    if not prov:
        raise TiendaError('PROVEEDOR_NO_ENCONTRADO', 'El proveedor no existe.',
                          404)
    if (prov.get('categoria') or '') != (pedido.get('categoria') or ''):
        raise TiendaError(
            'CATEGORIA_DISTINTA',
            f'{prov["nombre"]} es de {prov.get("categoria") or "sin categoria"} '
            f'y el producto es de {pedido.get("categoria") or "sin categoria"}.',
            409)
    cur.execute(
        f"UPDATE {almacen_db()}.productos_para_pedir SET proveedor_id=%s "
        f"WHERE id=%s", (pid, _entero(pedido_id)))
    return prov


def quitar_proveedor(cur, pedido_id):
    cur.execute(
        f"UPDATE {almacen_db()}.productos_para_pedir SET proveedor_id=NULL "
        f"WHERE id=%s AND estado='pendiente'", (_entero(pedido_id),))
    if cur.rowcount != 1:
        raise TiendaError('PEDIDO_NO_ENCONTRADO',
                          'Ese pedido ya no esta pendiente.', 409)
    return True


def marcar_enviado(cur, proveedor_id):
    pid = _entero(proveedor_id, -1)
    if pid <= 0:
        raise TiendaError('VALIDACION',
                          'Selecciona el proveedor del grupo para marcarlo '
                          'como enviado.', 422, {'campo': 'proveedor_id'})
    cur.execute(f"SELECT nombre, celular FROM {almacen_db()}.proveedores "
                f"WHERE id=%s", (pid,))
    prov = cur.fetchone()
    if not prov:
        raise TiendaError('PROVEEDOR_NO_ENCONTRADO', 'El proveedor no existe.',
                          404)
    cur.execute(
        f"""UPDATE {almacen_db()}.productos_para_pedir
            SET estado='enviado'
            WHERE estado='pendiente' AND proveedor_id=%s""", (pid,))
    return _entero(getattr(cur, 'rowcount', 0)), prov
