"""Pruebas de contrato del almacen (Fase 5) sin MySQL real.

La BD de juguete interpreta las consultas que generan
`services.admin_proveedores_service`, `services.admin_ingresos_service`,
`services.admin_salidas_service`, `services.admin_entregas_service` y
`services.admin_inventario_service`. Se prueba el cableado HTTP, los permisos,
las reglas de negocio y la forma del JSON.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

import app as monolito
from api import security

ADMIN = {
    'id': 1, 'correo': 'admin@mail.com', 'rol': 'admin', 'nombre': 'Admin',
    'apellido': 'Root', 'telefono': '999999999', 'documento_tipo': 'dni',
    'documento_numero': '00000000', 'estado': 'activo', 'created_at': None,
    'token_version': 0,
}
CLIENTE = dict(ADMIN, id=7, correo='ana@example.com', rol='cliente')


def _producto(id, nombre, precio, stock, categoria='Herramientas',
              estado='activo', imagen=None):
    return {
        'id': id, 'nombre': nombre, 'descripcion': '', 'precio': Decimal(precio),
        'stock': stock, 'categoria': categoria, 'imagen': imagen,
        'imagen_datos': None, 'imagen_mime': None, 'estado': estado,
        'created_at': datetime(2026, 1, 1, 12, 0, 0),
    }


def _proveedor(id, nombre, categoria='Herramientas', tipo='DNI',
               numero='12345678', celular='999888777'):
    return {
        'id': id, 'nombre': nombre, 'tipo_documento': tipo,
        'numero_documento': numero, 'nombres': nombre, 'apellido_paterno': '',
        'apellido_materno': '', 'celular': celular, 'direccion': 'Av. Peru 123',
        'categoria': categoria, 'notas': '',
        'created_at': datetime(2026, 1, 1, 12, 0, 0),
    }


class BDJuguete:
    def __init__(self):
        self.productos = {
            1: _producto(1, 'Taladro', '120.50', 5, imagen='imagenes/t.jpg'),
            2: _producto(2, 'Cable', '10.00', 0, categoria='Electricos',
                         estado='inactivo'),
            3: _producto(3, 'Foco', '15.00', 20, categoria='Electricos'),
            4: _producto(4, 'Martillo', '30.00', 60),
            5: _producto(5, 'Sierra', '99.00', 0),
        }
        self.proveedores = {
            1: _proveedor(1, 'Ferreteria Uno', 'Herramientas'),
            2: _proveedor(2, 'Electricos SAC', 'Electricos', tipo='RUC',
                          numero='20123456789'),
        }
        self.pedidos = [
            {'id': 1, 'producto_id': 1, 'cantidad_pedido': 2, 'proveedor_id': 1,
             'estado': 'pendiente', 'fecha': datetime(2026, 3, 1, 9, 0, 0)},
            {'id': 2, 'producto_id': 2, 'cantidad_pedido': 1, 'proveedor_id': None,
             'estado': 'pendiente', 'fecha': datetime(2026, 3, 2, 9, 0, 0)},
        ]
        self.ingresos = []
        self.detalle_ingreso = []
        self.salidas = []
        self.detalle_salida = []
        self.ventas = {
            1: {'id': 1, 'total': Decimal('200.00'), 'nombre': 'Ana',
                'documento': '12345678', 'estado': 'en espera',
                'cliente_id': 7, 'oculta': 0, 'fecha': datetime(2026, 3, 1)},
        }
        self.detalle_venta = [
            {'id': 1, 'venta_id': 1, 'producto_id': 1, 'cantidad': 1,
             'precio': Decimal('120.50')},
        ]
        self.seguimiento = []
        self.entrega_productos = []
        self.cancelaciones = []
        self.usuarios = {7: {'id': 7, 'correo': 'ana@example.com'}}
        self.usuario = dict(ADMIN)
        self._next = {'proveedor': 3, 'ingreso': 1, 'salida': 1,
                      'seguimiento': 1, 'entrega_producto': 1, 'pedido': 3}
        self.connection = self

    def cursor(self):
        return CursorFalso(self)

    def commit(self):
        return None

    def rollback(self):
        return None


class CursorFalso:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self._one = None
        self.rowcount = 0
        self.lastrowid = 0

    def _productos_filtrados(self, s, buscar):
        import re as _re
        filas = list(self.db.productos.values())
        if buscar:
            filas = [f for f in filas if buscar.lower() in f['nombre'].lower()]
        if "p.stock=0 AND p.estado='activo'" in s:
            filas = [f for f in filas
                     if f['estado'] == 'activo' and f['stock'] == 0]
        elif 'p.stock BETWEEN 1 AND' in s:
            lim = int(_re.search(r'BETWEEN 1 AND (\d+)', s).group(1))
            filas = [f for f in filas
                     if f['estado'] == 'activo' and 1 <= f['stock'] <= lim]
        elif 'p.stock BETWEEN' in s:
            m = _re.search(r'BETWEEN (\d+) AND (\d+)', s)
            lo, hi = int(m.group(1)), int(m.group(2))
            filas = [f for f in filas
                     if f['estado'] == 'activo' and lo <= f['stock'] <= hi]
        elif 'p.stock >= ' in s:
            lim = int(_re.search(r'stock >= (\d+)', s).group(1))
            filas = [f for f in filas
                     if f['estado'] == 'activo' and f['stock'] >= lim]
        elif "p.estado='inactivo'" in s:
            filas = [f for f in filas if f['estado'] == 'inactivo']
        return filas

    def _producto_json(self, p, cantidad=None, precio_compra=None,
                       precio_venta=None, nombre_override=None):
        return {
            'id': p['id'], 'producto_id': p['id'],
            'nombre': p['nombre'], 'producto_nombre': nombre_override
            or p['nombre'], 'stock': p['stock'], 'stock_actual': p['stock'],
            'precio': p['precio'], 'categoria': p['categoria'],
            'imagen': p['imagen'], 'estado': p['estado'],
            'cantidad': cantidad, 'precio_compra': precio_compra,
            'precio_venta': precio_venta,
        }

    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        p = tuple(params or ())

        # ── Proveedores ──
        if s.startswith('SELECT COUNT(*) AS total FROM') and '.proveedores' in s \
                and 'WHERE nombre LIKE' in s:
            filas = list(self.db.proveedores.values())
            buscar = str(p[0]).strip('%') if p else ''
            if buscar:
                filas = [f for f in filas if buscar.lower() in f['nombre'].lower()]
            if 'AND categoria=%s' in s and len(p) > 1:
                filas = [f for f in filas if f['categoria'] == p[1]]
            self._one = {'total': len(filas)}
            return
        if s.startswith('SELECT * FROM') and '.proveedores' in s \
                and 'WHERE id=%s' in s:
            self._one = dict(self.db.proveedores.get(int(p[0]))) \
                if int(p[0]) in self.db.proveedores else None
            return
        if s.startswith('SELECT * FROM') and '.proveedores' in s:
            filas = list(self.db.proveedores.values())
            buscar = str(p[0]).strip('%') if p else ''
            if buscar:
                filas = [f for f in filas if buscar.lower() in f['nombre'].lower()]
            if 'AND categoria=%s' in s and len(p) > 1:
                filas = [f for f in filas if f['categoria'] == p[1]]
            self._rows = [dict(f) for f in filas]
            return
        if s.startswith('SELECT id FROM') and '.proveedores' in s \
                and 'tipo_documento=%s' in s:
            tipo, numero, categoria = p[0], p[1], p[2]
            excluir = int(p[3]) if len(p) > 3 else None
            filas = [f for f in self.db.proveedores.values()
                     if f['tipo_documento'] == tipo
                     and f['numero_documento'] == numero
                     and f['categoria'] == categoria and f['id'] != excluir]
            self._one = {'id': filas[0]['id']} if filas else None
            return
        if s.startswith('INSERT INTO') and '.proveedores' in s:
            nombre, tipo, numero, nombres, ap, am, celular, direccion, \
                categoria, notas = p
            nid = self.db._next['proveedor']
            self.db._next['proveedor'] += 1
            self.db.proveedores[nid] = _proveedor(nid, nombre, categoria, tipo,
                                                  numero, celular)
            self.db.proveedores[nid].update({
                'nombres': nombres, 'apellido_paterno': ap,
                'apellido_materno': am, 'direccion': direccion, 'notas': notas})
            self.lastrowid = nid
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.proveedores' in s:
            nombre, tipo, numero, nombres, ap, am, celular, direccion, \
                categoria, notas, pid = p
            self.db.proveedores[int(pid)].update({
                'nombre': nombre, 'tipo_documento': tipo,
                'numero_documento': numero, 'nombres': nombres,
                'apellido_paterno': ap, 'apellido_materno': am,
                'celular': celular, 'direccion': direccion,
                'categoria': categoria, 'notas': notas})
            self.rowcount = 1
            return
        if s.startswith('DELETE FROM') and '.proveedores' in s:
            self.db.proveedores.pop(int(p[0]), None)
            self.rowcount = 1
            return
        if s.startswith('SELECT id, nombre, celular, categoria FROM') \
                and '.proveedores' in s:
            cats = set(p)
            self._rows = [dict(f) for f in self.db.proveedores.values()
                          if f['categoria'] in cats]
            return
        if s.startswith('SELECT nombre, tipo_documento, numero_documento FROM') \
                and '.proveedores' in s:
            prov = self.db.proveedores.get(int(p[0]))
            self._one = {'nombre': prov['nombre'],
                         'tipo_documento': prov['tipo_documento'],
                         'numero_documento': prov['numero_documento']} if prov \
                else None
            return
        if s.startswith('SELECT nombre, celular, categoria FROM') \
                and '.proveedores' in s:
            prov = self.db.proveedores.get(int(p[0]))
            self._one = {'nombre': prov['nombre'], 'celular': prov['celular'],
                         'categoria': prov['categoria']} if prov else None
            return
        if s.startswith('SELECT nombre, celular FROM') and '.proveedores' in s:
            prov = self.db.proveedores.get(int(p[0]))
            self._one = {'nombre': prov['nombre'], 'celular': prov['celular']} \
                if prov else None
            return

        # ── Pedidos a proveedor ──
        if '.productos_para_pedir pp' in s and s.startswith('SELECT COUNT(*)'):
            sin_asignar = sum(
                1 for d in self.db.pedidos
                if d['estado'] == 'pendiente'
                and (d['proveedor_id'] is None
                     or d['proveedor_id'] not in self.db.proveedores))
            self._one = {'n': sin_asignar}
            return
        if '.productos_para_pedir pp' in s and 'p.categoria' in s \
                and 'WHERE pp.id=%s' in s:
            d = next((x for x in self.db.pedidos
                      if x['id'] == int(p[0]) and x['estado'] == 'pendiente'),
                     None)
            if not d:
                self._one = None
            else:
                prod = self.db.productos[d['producto_id']]
                self._one = {'id': d['id'], 'producto_id': d['producto_id'],
                             'cantidad_pedido': d['cantidad_pedido'],
                             'estado': d['estado'], 'categoria': prod['categoria']}
            return
        if '.productos_para_pedir pp' in s and 'pp.producto_id' in s \
                and 'COUNT(DISTINCT' not in s and 'SELECT COUNT' not in s:
            filas = []
            for d in self.db.pedidos:
                if d['estado'] != 'pendiente':
                    continue
                prov = self.db.proveedores.get(d['proveedor_id'])
                if 'AND (pp.proveedor_id IS NULL OR pr.id IS NULL)' in s \
                        and prov is not None:
                    continue
                prod = self.db.productos[d['producto_id']]
                filas.append({
                    'id': d['id'], 'producto_id': d['producto_id'],
                    'cantidad_pedido': d['cantidad_pedido'], 'fecha': d['fecha'],
                    'estado': d['estado'],
                    'proveedor_asignado': d['proveedor_id'],
                    'producto_nombre': prod['nombre'], 'stock_actual': prod['stock'],
                    'categoria': prod['categoria'],
                    'proveedor_nombre': prov['nombre'] if prov else None,
                    'proveedor_celular': prov['celular'] if prov else None})
            self._rows = filas
            return
        if '.productos_para_pedir' in s and s.startswith('UPDATE') \
                and 'cantidad_pedido=%s' in s:
            d = next((x for x in self.db.pedidos if x['id'] == int(p[1])), None)
            if d:
                d['cantidad_pedido'] = p[0]
                self.rowcount = 1
            return
        if '.productos_para_pedir' in s and s.startswith('UPDATE') \
                and 'SET proveedor_id=%s' in s:
            d = next((x for x in self.db.pedidos if x['id'] == int(p[1])), None)
            if d:
                d['proveedor_id'] = int(p[0])
                self.rowcount = 1
            return
        if '.productos_para_pedir' in s and s.startswith('UPDATE') \
                and 'proveedor_id=NULL' in s:
            idx = int(p[0])
            d = next((x for x in self.db.pedidos if x['id'] == idx), None)
            if d and ('estado=\'pendiente\'' not in s
                      or d['estado'] == 'pendiente'):
                d['proveedor_id'] = None
                self.rowcount = 1
            return
        if '.productos_para_pedir' in s and s.startswith('UPDATE') \
                and "estado='enviado'" in s:
            pid = int(p[0])
            n = 0
            for d in self.db.pedidos:
                if d['estado'] == 'pendiente' and d['proveedor_id'] == pid:
                    d['estado'] = 'enviado'
                    n += 1
            self.rowcount = n
            return

        # ── Ingresos ──
        if s.startswith('SELECT COUNT(*) AS total FROM') and '.ingresos' in s:
            self._one = {'total': len(self.db.ingresos)}
            return
        if s.startswith('SELECT MIN(YEAR(fecha))') and '.ingresos' in s:
            self._one = {'minimo': 2026}
            return
        if s.startswith('SELECT COALESCE(estado') and '.ingresos' in s:
            ing = next((x for x in self.db.ingresos if x['id'] == int(p[0])),
                       None)
            self._one = {'estado': (ing['estado'] if ing else None)} if ing \
                else None
            return
        if s.startswith('SELECT i.id') and '.ingresos i' in s \
                and 'COUNT(DISTINCT' in s:
            self._rows = [dict(x) for x in self.db.ingresos]
            return
        if s.startswith('SELECT i.id') and '.ingresos i' in s:
            ing = next((x for x in self.db.ingresos if x['id'] == int(p[0])),
                       None)
            self._one = dict(ing) if ing else None
            return
        if s.startswith('SELECT di.cantidad') and 'detalle_ingreso' in s:
            dets = [d for d in self.db.detalle_ingreso
                    if d['ingreso_id'] == int(p[0])]
            self._rows = [{
                'cantidad': d['cantidad'],
                'precio_compra': d['precio_compra'],
                'precio_venta': d['precio_venta'],
                'producto_nombre': d['producto_nombre'],
                'stock_actual': self.db.productos.get(d['producto_id'], {}).get(
                    'stock'),
            } for d in dets]
            return
        if s.startswith('SELECT producto_id, cantidad FROM') \
                and 'detalle_ingreso' in s:
            self._rows = [{'producto_id': d['producto_id'],
                           'cantidad': d['cantidad']}
                          for d in self.db.detalle_ingreso
                          if d['ingreso_id'] == int(p[0])]
            return
        if s.startswith('SELECT id, nombre FROM') and '.productos' in s:
            ids = [int(x) for x in p]
            self._rows = [{'id': i, 'nombre': self.db.productos[i]['nombre']}
                          for i in ids if i in self.db.productos]
            return
        if s.startswith('INSERT INTO') and '.ingresos' in s:
            pid, notas, fecha, prov_nombre, prov_documento = p
            iid = self.db._next['ingreso']
            self.db._next['ingreso'] += 1
            self.db.ingresos.append({
                'id': iid, 'proveedor_id': pid, 'notas': notas, 'fecha': fecha,
                'estado': 'vigente', 'motivo_anulacion': None,
                'anulado_en': None, 'anulado_por': None,
                'proveedor_nombre': prov_nombre,
                'proveedor_documento': prov_documento,
                'proveedor_categoria': self.db.proveedores.get(
                    int(pid), {}).get('categoria'),
                'proveedor_celular': self.db.proveedores.get(
                    int(pid), {}).get('celular')})
            self.lastrowid = iid
            self.rowcount = 1
            return
        if s.startswith('INSERT INTO') and 'detalle_ingreso' in s:
            iid, prod_id, prod_nombre, cant, pc, pv = p
            self.db.detalle_ingreso.append({
                'ingreso_id': iid, 'producto_id': prod_id,
                'producto_nombre': prod_nombre, 'cantidad': cant,
                'precio_compra': pc, 'precio_venta': pv})
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.ingresos' in s and "estado='anulado'" in s:
            iid = int(p[3])
            ing = next((x for x in self.db.ingresos if x['id'] == iid), None)
            if ing and (ing['estado'] or 'vigente') == 'vigente':
                ing.update({'estado': 'anulado', 'motivo_anulacion': p[0],
                            'anulado_en': p[1], 'anulado_por': p[2]})
                self.rowcount = 1
            return

        # ── Salidas ──
        if s.startswith('SELECT COUNT(*) AS total FROM') and '.salidas' in s:
            self._one = {'total': len(self.db.salidas)}
            return
        if s.startswith('SELECT s.id') and '.salidas s' in s \
                and 'valor_estimado' in s:
            self._rows = []
            for sal in self.db.salidas:
                items = [d for d in self.db.detalle_salida
                         if d['salida_id'] == sal['id']]
                venta = self.db.ventas.get(sal['venta_id'])
                self._rows.append({
                    'id': sal['id'], 'fecha': sal['fecha'], 'notas': sal['notas'],
                    'venta_id': sal['venta_id'],
                    'cliente_nombre': venta['nombre'] if venta else None,
                    'venta_total': venta['total'] if venta else None,
                    'total_items': sum(i['cantidad'] for i in items),
                    'valor_estimado': Decimal('0')})
            return
        if s.startswith('SELECT s.id') and '.salidas s' in s:
            sal = next((x for x in self.db.salidas if x['id'] == int(p[0])), None)
            if not sal:
                self._one = None
                return
            venta = self.db.ventas.get(sal['venta_id'])
            self._one = {
                'id': sal['id'], 'fecha': sal['fecha'], 'notas': sal['notas'],
                'venta_id': sal['venta_id'],
                'cliente_nombre': venta['nombre'] if venta else None,
                'cliente_doc': venta['documento'] if venta else None,
                'venta_total': venta['total'] if venta else None,
                'venta_fecha': venta['fecha'] if venta else None}
            return
        if s.startswith('SELECT id FROM') and '.salidas' in s:
            sal = next((x for x in self.db.salidas if x['id'] == int(p[0])), None)
            self._one = {'id': sal['id']} if sal else None
            return
        if s.startswith('SELECT ds.producto_id') and 'detalle_salida' in s:
            self._rows = [{
                'producto_id': d['producto_id'], 'cantidad': d['cantidad'],
                'producto_nombre': self.db.productos.get(
                    d['producto_id'], {}).get('nombre'),
                'stock_actual': self.db.productos.get(
                    d['producto_id'], {}).get('stock')}
                for d in self.db.detalle_salida
                if d['salida_id'] == int(p[0])]
            return
        if s.startswith('SELECT producto_id, cantidad FROM') \
                and 'detalle_salida' in s:
            self._rows = [{'producto_id': d['producto_id'],
                           'cantidad': d['cantidad']}
                          for d in self.db.detalle_salida
                          if d['salida_id'] == int(p[0])]
            return
        if s.startswith('INSERT INTO') and '.salidas' in s:
            vid, notas = p
            sid = self.db._next['salida']
            self.db._next['salida'] += 1
            self.db.salidas.append({'id': sid, 'venta_id': vid, 'notas': notas,
                                    'fecha': datetime(2026, 3, 3, 10, 0, 0)})
            self.lastrowid = sid
            self.rowcount = 1
            return
        if s.startswith('INSERT INTO') and 'detalle_salida' in s:
            self.db.detalle_salida.append({
                'salida_id': p[0], 'producto_id': p[1], 'cantidad': p[2]})
            self.rowcount = 1
            return
        if s.startswith('DELETE FROM') and 'detalle_salida' in s:
            self.db.detalle_salida = [d for d in self.db.detalle_salida
                                      if d['salida_id'] != int(p[0])]
            return
        if s.startswith('DELETE FROM') and '.salidas' in s:
            self.db.salidas = [x for x in self.db.salidas if x['id'] != int(p[0])]
            return

        # ── Productos: stock ──
        if s.startswith('SELECT nombre, stock FROM') and '.productos' in s:
            prod = self.db.productos.get(int(p[0]))
            self._one = {'nombre': prod['nombre'], 'stock': prod['stock']} \
                if prod else None
            return
        if s.startswith('SELECT stock FROM') and '.productos' in s:
            prod = self.db.productos.get(int(p[0]))
            self._one = {'stock': prod['stock']} if prod else None
            return
        if s.startswith('UPDATE') and '.productos' in s \
                and 'stock=stock+%s, precio=%s' in s:
            cant, precio, pid = p
            prod = self.db.productos[int(pid)]
            prod['stock'] += int(cant)
            prod['precio'] = Decimal(str(precio))
            prod['estado'] = 'activo'
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos' in s \
                and 'stock=stock-%s WHERE id=%s AND stock>=%s' in s:
            cant, pid, _min = int(p[0]), int(p[1]), int(p[2])
            prod = self.db.productos[int(pid)]
            prod['stock'] -= cant
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos' in s and 'stock=stock+%s' in s:
            cant, pid = p
            self.db.productos[int(pid)]['stock'] += int(cant)
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos' in s and 'stock=stock-%s' in s:
            cant, pid = p
            self.db.productos[int(pid)]['stock'] -= int(cant)
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos' in s and 'SET stock=0' in s:
            self.db.productos[int(p[0])]['stock'] = 0
            self.rowcount = 1
            return

        # ── Ventas / entregas ──
        if s.startswith('SELECT id FROM ventas WHERE id=%s'):
            self._one = {'id': int(p[0])} if int(p[0]) in self.db.ventas else None
            return
        if s.startswith('SELECT id, estado FROM ventas'):
            v = self.db.ventas.get(int(p[0]))
            self._one = {'id': v['id'], 'estado': v['estado']} if v else None
            return
        if s.startswith('SELECT id, total, nombre, documento FROM ventas'):
            v = self.db.ventas.get(int(p[0]))
            self._one = dict(v) if v else None
            return
        if s.startswith('UPDATE ventas SET estado=%s'):
            self.db.ventas[int(p[1])]['estado'] = p[0]
            self.rowcount = 1
            return
        if s.startswith('SELECT COUNT(*) AS total FROM seguimiento_entregas'):
            self._one = {'total': len(self.db.seguimiento)}
            return
        if s.startswith('SELECT e.id') and 'seguimiento_entregas e' in s:
            filas = []
            for e in self.db.seguimiento:
                venta = self.db.ventas.get(e['venta_id'])
                if not venta or venta.get('oculta'):
                    continue
                filas.append({
                    'id': e['id'], 'venta_id': e['venta_id'],
                    'estado': e['estado'], 'direccion_envio': e['direccion_envio'],
                    'fecha_estimada': e['fecha_estimada'],
                    'fecha_entrega': e.get('fecha_entrega'),
                    'notas': e['notas'], 'created_at': e['created_at'],
                    'total': venta['total'], 'cliente_nombre': venta['nombre'],
                    'cliente_doc': venta['documento'],
                    'cliente_correo': 'ana@example.com',
                    'motivo_codigo': None, 'motivo_detalle': None,
                    'cancelado_por': None})
            self._rows = filas
            return
        if s.startswith('SELECT e.*') and 'seguimiento_entregas e' in s:
            e = next((x for x in self.db.seguimiento if x['id'] == int(p[0])),
                     None)
            if not e:
                self._one = None
                return
            venta = self.db.ventas[e['venta_id']]
            self._one = dict(e, total=venta['total'],
                             cliente_nombre=venta['nombre'],
                             cliente_doc=venta['documento'],
                             venta_fecha=venta['fecha'],
                             cliente_correo='ana@example.com',
                             motivo_codigo=None, motivo_detalle=None,
                             cancelado_por=None)
            return
        if s.startswith('SELECT d.producto_id') and 'detalle_venta d' in s:
            self._rows = [dict(d, nombre=self.db.productos[d['producto_id']]['nombre'],
                               imagen=self.db.productos[d['producto_id']]['imagen'],
                               precio=d['precio'])
                          for d in self.db.detalle_venta
                          if d['venta_id'] == int(p[0])]
            return
        if s.startswith('SELECT ep.id') and 'entrega_productos ep' in s:
            self._rows = [{
                'id': e['id'], 'producto_id': e['producto_id'],
                'cantidad': e['cantidad'],
                'producto_nombre': self.db.productos.get(
                    e['producto_id'], {}).get('nombre'),
                'stock_actual': self.db.productos.get(
                    e['producto_id'], {}).get('stock')}
                for e in self.db.entrega_productos
                if e['entrega_id'] == int(p[0])]
            return
        if s.startswith('SELECT id FROM seguimiento_entregas WHERE venta_id=%s') \
                and 'LIMIT 1' in s:
            e = next((x for x in self.db.seguimiento
                      if x['venta_id'] == int(p[0])), None)
            self._one = {'id': e['id']} if e else None
            return
        if s.startswith('SELECT id FROM seguimiento_entregas WHERE venta_id=%s') \
                and 'ORDER BY id DESC' in s:
            e = next((x for x in reversed(self.db.seguimiento)
                      if x['venta_id'] == int(p[0])), None)
            self._one = {'id': e['id']} if e else None
            return
        if s.startswith('SELECT id, venta_id, estado FROM seguimiento_entregas'):
            e = next((x for x in self.db.seguimiento if x['id'] == int(p[0])),
                     None)
            self._one = dict(e) if e else None
            return
        if s.startswith('SELECT id FROM seguimiento_entregas WHERE id=%s'):
            e = next((x for x in self.db.seguimiento if x['id'] == int(p[0])),
                     None)
            self._one = {'id': e['id']} if e else None
            return
        if s.startswith('INSERT INTO seguimiento_entregas'):
            fecha, vid, direccion, fecha_est, notas = p
            sid = self.db._next['seguimiento']
            self.db._next['seguimiento'] += 1
            self.db.seguimiento.append({
                'id': sid, 'venta_id': int(vid), 'estado': 'pendiente',
                'direccion_envio': direccion, 'fecha_estimada': fecha_est,
                'fecha_entrega': None, 'notas': notas, 'created_at': fecha})
            self.lastrowid = sid
            self.rowcount = 1
            return
        if s.startswith('UPDATE seguimiento_entregas') \
                and 'fecha_entrega=%s' in s:
            e = next((x for x in self.db.seguimiento if x['id'] == int(p[3])), None)
            if e:
                e.update({'estado': p[0], 'notas': p[1], 'fecha_entrega': p[2]})
                self.rowcount = 1
            return
        if s.startswith('UPDATE seguimiento_entregas') and 'direccion_envio=%s' in s:
            e = next((x for x in self.db.seguimiento if x['id'] == int(p[3])), None)
            if e:
                e.update({'direccion_envio': p[0], 'fecha_estimada': p[1],
                          'notas': p[2]})
                self.rowcount = 1
            return
        if s.startswith('UPDATE seguimiento_entregas') and 'SET estado=%s' in s:
            e = next((x for x in self.db.seguimiento if x['id'] == int(p[2])), None)
            if e:
                e.update({'estado': p[0], 'notas': p[1]})
                self.rowcount = 1
            return
        if s.startswith('DELETE FROM seguimiento_entregas'):
            self.db.seguimiento = [x for x in self.db.seguimiento
                                   if x['id'] != int(p[0])]
            return
        if s.startswith('SELECT id FROM seguimiento_entregas WHERE venta_id=%s'):
            self._one = None
            return
        if s.startswith('DELETE FROM entrega_productos'):
            self.db.entrega_productos = [
                x for x in self.db.entrega_productos
                if x['entrega_id'] != int(p[0])]
            return
        if s.startswith('INSERT INTO entrega_productos'):
            eid = self.db._next['entrega_producto']
            self.db._next['entrega_producto'] += 1
            self.db.entrega_productos.append({
                'id': eid, 'entrega_id': p[0], 'producto_id': p[1],
                'cantidad': p[2]})
            self.rowcount = 1
            return

        # ── Inventario ──
        if s.startswith('SELECT COUNT(*) AS total FROM') and '.productos p' in s:
            buscar = str(p[0]).strip('%') if p else ''
            self._one = {'total': len(self._productos_filtrados(s, buscar))}
            return
        if s.startswith('SELECT COUNT(*) AS n FROM') and '.productos' in s:
            prods = list(self.db.productos.values())
            if 'estado=\'activo\' AND stock=0' in s:
                self._one = {'n': len([x for x in prods if x['estado'] == 'activo'
                                       and x['stock'] == 0])}
            elif 'stock BETWEEN 1 AND %s' in s:
                self._one = {'n': len([x for x in prods if x['estado'] == 'activo'
                                       and 1 <= x['stock'] <= int(p[0])])}
            elif 'stock BETWEEN %s AND %s' in s:
                self._one = {'n': len([
                    x for x in prods if x['estado'] == 'activo'
                    and int(p[0]) <= x['stock'] <= int(p[1])])}
            elif 'stock >= %s' in s:
                self._one = {'n': len([x for x in prods if x['estado'] == 'activo'
                                       and x['stock'] >= int(p[0])])}
            else:
                self._one = {'n': len(prods)}
            return
        if s.startswith('SELECT p.id, p.nombre, p.stock') and '.productos p' in s:
            buscar = str(p[0]).strip('%') if p else ''
            self._rows = [dict(f) for f in self._productos_filtrados(s, buscar)]
            return

        # Nada que emular: consulta vacia.
        self._rows = []
        self._one = None
        return

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows

    def close(self):
        return None


@pytest.fixture()
def client(tmp_path):
    monolito.app.config.update(TESTING=True)
    monolito.app.config['UPLOAD_FOLDER'] = str(tmp_path)
    with monolito.app.test_client() as cliente:
        yield cliente


@pytest.fixture()
def bd(monkeypatch):
    db = BDJuguete()
    monkeypatch.setattr('api.deps.get_mysql', lambda: db)
    monkeypatch.setattr(security, 'get_mysql', lambda: db)
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, '_schema_listo', True)
    monkeypatch.setattr('services.auth_service.buscar_usuario_por_id',
                        lambda _cur, _id: db.usuario)
    monkeypatch.setattr(monolito, 'sincronizar_pedido_por_stock',
                        lambda _cur, _pid: None)
    db.cancelaciones_registradas = []
    monkeypatch.setattr(monolito, 'restaurar_stock_venta',
                        lambda _cur, _vid: db.cancelaciones_registradas.append(
                            ('stock', _vid)))
    monkeypatch.setattr(monolito, 'registrar_cancelacion',
                        lambda _cur, vid, cod, det, por:
                        db.cancelaciones_registradas.append(
                            ('motivo', vid, cod, det, por)))
    return db


def _headers(rol='admin'):
    with monolito.app.app_context():
        token = create_access_token(
            identity='1', additional_claims={'ver': 0, 'rol': rol})
    return {'Authorization': 'Bearer %s' % token}


# ─────────────────────────────────────────────
# PERMISOS
# ─────────────────────────────────────────────
def test_almacen_requiere_admin(client, bd):
    bd.usuario = dict(CLIENTE)
    for ruta in ('/api/v1/admin/proveedores', '/api/v1/admin/ingresos',
                 '/api/v1/admin/salidas', '/api/v1/admin/entregas',
                 '/api/v1/admin/inventario/verificacion'):
        respuesta = client.get(ruta, headers=_headers('cliente'))
        assert respuesta.status_code == 403, ruta
        assert respuesta.get_json()['error']['code'] == 'SIN_PERMISO'


def test_almacen_sin_token(client, bd):
    assert client.get('/api/v1/admin/proveedores').status_code == 401


# ─────────────────────────────────────────────
# PROVEEDORES
# ─────────────────────────────────────────────
def test_listar_proveedores(client, bd):
    respuesta = client.get('/api/v1/admin/proveedores', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['meta']['total'] == 2


def test_listar_proveedores_categoria(client, bd):
    respuesta = client.get('/api/v1/admin/proveedores?categoria=Electricos',
                           headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 1


def test_crear_proveedor_dni(client, bd):
    respuesta = client.post('/api/v1/admin/proveedores', headers=_headers(),
                            json={'tipo_documento': 'dni',
                                  'numero_documento': '45678912',
                                  'nombres': 'Juan', 'apellido_paterno': 'Perez',
                                  'apellido_materno': 'Lopez',
                                  'celular': '987654321',
                                  'direccion': 'Av. Peru 123',
                                  'categoria': 'Repuestos'})
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['nombre'] == 'Juan Perez Lopez'
    assert data['tipo_documento'] == 'DNI'


def test_crear_proveedor_ruc(client, bd):
    respuesta = client.post('/api/v1/admin/proveedores', headers=_headers(),
                            json={'tipo_documento': 'ruc',
                                  'numero_documento': '20999999999',
                                  'razon_social': 'Ferreteria Sur SAC',
                                  'celular': '987654321',
                                  'direccion': 'Jr. Lima 1',
                                  'categoria': 'Otros'})
    assert respuesta.status_code == 201
    assert respuesta.get_json()['data']['nombre'] == 'Ferreteria Sur SAC'


def test_crear_proveedor_documento_invalido(client, bd):
    respuesta = client.post('/api/v1/admin/proveedores', headers=_headers(),
                            json={'tipo_documento': 'dni',
                                  'numero_documento': '12', 'nombres': 'Juan',
                                  'apellido_paterno': 'Perez',
                                  'apellido_materno': 'Lopez',
                                  'celular': '987654321',
                                  'direccion': 'x', 'categoria': 'Otros'})
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'VALIDACION'


def test_crear_proveedor_celular_invalido(client, bd):
    respuesta = client.post('/api/v1/admin/proveedores', headers=_headers(),
                            json={'tipo_documento': 'dni',
                                  'numero_documento': '45678912',
                                  'nombres': 'Juan', 'apellido_paterno': 'Perez',
                                  'apellido_materno': 'Lopez',
                                  'celular': '123', 'direccion': 'x',
                                  'categoria': 'Otros'})
    assert respuesta.status_code == 422


def test_crear_proveedor_duplicado(client, bd):
    respuesta = client.post('/api/v1/admin/proveedores', headers=_headers(),
                            json={'tipo_documento': 'dni',
                                  'numero_documento': '12345678',
                                  'nombres': 'Otro', 'apellido_paterno': 'Perez',
                                  'apellido_materno': 'Lopez',
                                  'celular': '987654321', 'direccion': 'x',
                                  'categoria': 'Herramientas'})
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'PROVEEDOR_DUPLICADO'


def test_ver_proveedor(client, bd):
    respuesta = client.get('/api/v1/admin/proveedores/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['nombre'] == 'Ferreteria Uno'


def test_ver_proveedor_inexistente(client, bd):
    respuesta = client.get('/api/v1/admin/proveedores/99', headers=_headers())
    assert respuesta.status_code == 404


def test_actualizar_proveedor(client, bd):
    respuesta = client.patch('/api/v1/admin/proveedores/1', headers=_headers(),
                             json={'tipo_documento': 'dni',
                                   'numero_documento': '12345678',
                                   'nombres': 'Ferreteria', 'apellido_paterno': 'Uno',
                                   'apellido_materno': 'Dos',
                                   'celular': '900000000',
                                   'direccion': 'Nueva 1',
                                   'categoria': 'Herramientas'})
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['celular'] == '900000000'


def test_eliminar_proveedor(client, bd):
    respuesta = client.delete('/api/v1/admin/proveedores/2', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['eliminado'] is True
    assert 2 not in bd.proveedores


# ─────────────────────────────────────────────
# PEDIDOS A PROVEEDOR
# ─────────────────────────────────────────────
def test_listar_pedidos_compra(client, bd):
    respuesta = client.get('/api/v1/admin/pedidos-compra', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 2
    assert cuerpo['meta']['sin_asignar'] == 1
    assert len(cuerpo['meta']['grupos']) == 2


def test_listar_pedidos_compra_sin_asignar(client, bd):
    respuesta = client.get('/api/v1/admin/pedidos-compra?filtro=sin_asignar',
                           headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 1


def test_actualizar_cantidad_pedido(client, bd):
    respuesta = client.patch('/api/v1/admin/pedidos-compra/1',
                             headers=_headers(), json={'cantidad': 5})
    assert respuesta.status_code == 200
    assert bd.pedidos[0]['cantidad_pedido'] == 5


def test_actualizar_cantidad_invalida(client, bd):
    respuesta = client.patch('/api/v1/admin/pedidos-compra/1',
                             headers=_headers(), json={'cantidad': 0})
    assert respuesta.status_code == 422


def test_asignar_proveedor_pedido(client, bd):
    respuesta = client.post('/api/v1/admin/pedidos-compra/2/proveedor',
                            headers=_headers(), json={'proveedor_id': 2})
    assert respuesta.status_code == 200
    assert bd.pedidos[1]['proveedor_id'] == 2


def test_asignar_proveedor_otra_categoria(client, bd):
    respuesta = client.post('/api/v1/admin/pedidos-compra/2/proveedor',
                            headers=_headers(), json={'proveedor_id': 1})
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'CATEGORIA_DISTINTA'


def test_asignar_proveedor_inexistente(client, bd):
    respuesta = client.post('/api/v1/admin/pedidos-compra/2/proveedor',
                            headers=_headers(), json={'proveedor_id': 99})
    assert respuesta.status_code == 404


def test_quitar_proveedor_pedido(client, bd):
    respuesta = client.delete('/api/v1/admin/pedidos-compra/1/proveedor',
                              headers=_headers())
    assert respuesta.status_code == 200
    assert bd.pedidos[0]['proveedor_id'] is None


def test_marcar_enviado(client, bd):
    respuesta = client.post('/api/v1/admin/pedidos-compra/marcar-enviado',
                            headers=_headers(), json={'proveedor_id': 1})
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['enviados'] == 1
    assert bd.pedidos[0]['estado'] == 'enviado'


def test_marcar_enviado_proveedor_invalido(client, bd):
    respuesta = client.post('/api/v1/admin/pedidos-compra/marcar-enviado',
                            headers=_headers(), json={'proveedor_id': 0})
    assert respuesta.status_code == 422


# ─────────────────────────────────────────────
# INGRESOS
# ─────────────────────────────────────────────
def test_listar_ingresos_vacio(client, bd):
    respuesta = client.get('/api/v1/admin/ingresos', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['meta']['total'] == 0
    assert 'motivos' in respuesta.get_json()['meta']


def test_crear_ingreso_sube_stock(client, bd):
    respuesta = client.post('/api/v1/admin/ingresos', headers=_headers(),
                            json={'proveedor_id': 1, 'notas': 'Compra',
                                  'items': [{'producto_id': 1, 'cantidad': 3,
                                             'precio_compra': '8.00',
                                             'precio_venta': '12.50'}]})
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['total_cantidad'] == 3
    assert data['items'][0]['producto_nombre'] == 'Taladro'
    assert bd.productos[1]['stock'] == 8
    assert bd.productos[1]['precio'] == Decimal('12.50')


def test_crear_ingreso_sin_items(client, bd):
    respuesta = client.post('/api/v1/admin/ingresos', headers=_headers(),
                            json={'proveedor_id': 1, 'items': []})
    assert respuesta.status_code == 422


def test_crear_ingreso_producto_repetido(client, bd):
    respuesta = client.post('/api/v1/admin/ingresos', headers=_headers(),
                            json={'proveedor_id': 1, 'items': [
                                {'producto_id': 1, 'cantidad': 1,
                                 'precio_compra': '1.00', 'precio_venta': '2.00'},
                                {'producto_id': 1, 'cantidad': 1,
                                 'precio_compra': '1.00', 'precio_venta': '2.00'}]})
    assert respuesta.status_code == 422
    assert 'repetido' in respuesta.get_json()['error']['message']


def test_crear_ingreso_proveedor_inexistente(client, bd):
    respuesta = client.post('/api/v1/admin/ingresos', headers=_headers(),
                            json={'proveedor_id': 99, 'items': [
                                {'producto_id': 1, 'cantidad': 1,
                                 'precio_compra': '1.00', 'precio_venta': '2.00'}]})
    assert respuesta.status_code == 404


def test_ver_ingreso(client, bd):
    client.post('/api/v1/admin/ingresos', headers=_headers(),
                json={'proveedor_id': 1, 'items': [
                    {'producto_id': 1, 'cantidad': 2, 'precio_compra': '8.00',
                     'precio_venta': '12.50'}]})
    respuesta = client.get('/api/v1/admin/ingresos/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['items'][0]['cantidad'] == 2


def test_ver_ingreso_inexistente(client, bd):
    assert client.get('/api/v1/admin/ingresos/99',
                      headers=_headers()).status_code == 404


def test_anular_ingreso_revierte_stock(client, bd):
    client.post('/api/v1/admin/ingresos', headers=_headers(),
                json={'proveedor_id': 1, 'items': [
                    {'producto_id': 1, 'cantidad': 3, 'precio_compra': '8.00',
                     'precio_venta': '12.50'}]})
    assert bd.productos[1]['stock'] == 8
    respuesta = client.post('/api/v1/admin/ingresos/1/anular', headers=_headers(),
                            json={'motivo': 'duplicado'})
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'anulado'
    assert bd.productos[1]['stock'] == 5


def test_anular_ingreso_motivo_invalido(client, bd):
    client.post('/api/v1/admin/ingresos', headers=_headers(),
                json={'proveedor_id': 1, 'items': [
                    {'producto_id': 1, 'cantidad': 1, 'precio_compra': '1.00',
                     'precio_venta': '2.00'}]})
    respuesta = client.post('/api/v1/admin/ingresos/1/anular', headers=_headers(),
                            json={'motivo': 'nada'})
    assert respuesta.status_code == 422


def test_anular_ingreso_otro_sin_detalle(client, bd):
    client.post('/api/v1/admin/ingresos', headers=_headers(),
                json={'proveedor_id': 1, 'items': [
                    {'producto_id': 1, 'cantidad': 1, 'precio_compra': '1.00',
                     'precio_venta': '2.00'}]})
    respuesta = client.post('/api/v1/admin/ingresos/1/anular', headers=_headers(),
                            json={'motivo': 'otro'})
    assert respuesta.status_code == 422


def test_pdf_ingreso(client, bd):
    client.post('/api/v1/admin/ingresos', headers=_headers(),
                json={'proveedor_id': 1, 'items': [
                    {'producto_id': 1, 'cantidad': 1, 'precio_compra': '1.00',
                     'precio_venta': '2.00'}]})
    respuesta = client.get('/api/v1/admin/ingresos/1.pdf', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.mimetype == 'application/pdf'
    assert respuesta.data[:4] == b'%PDF'


def test_pdf_periodo_sin_datos(client, bd):
    respuesta = client.get('/api/v1/admin/ingresos.pdf', headers=_headers())
    assert respuesta.status_code == 404
    assert respuesta.get_json()['error']['code'] == 'SIN_DATOS'


def test_pdf_periodo_invalido(client, bd):
    respuesta = client.get('/api/v1/admin/ingresos.pdf?periodo=mes&mes=13',
                           headers=_headers())
    assert respuesta.status_code == 422


# ─────────────────────────────────────────────
# SALIDAS
# ─────────────────────────────────────────────
def test_listar_salidas_vacio(client, bd):
    respuesta = client.get('/api/v1/admin/salidas', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['meta']['total'] == 0


def test_crear_salida_descuenta_stock(client, bd):
    respuesta = client.post('/api/v1/admin/salidas', headers=_headers(),
                            json={'venta_id': 1, 'notas': 'Despacho',
                                  'items': [{'producto_id': 1, 'cantidad': 2}]})
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['total_items'] == 2
    assert bd.productos[1]['stock'] == 3


def test_crear_salida_stock_insuficiente(client, bd):
    respuesta = client.post('/api/v1/admin/salidas', headers=_headers(),
                            json={'items': [{'producto_id': 1, 'cantidad': 99}]})
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'STOCK_INSUFICIENTE'


def test_crear_salida_venta_inexistente(client, bd):
    respuesta = client.post('/api/v1/admin/salidas', headers=_headers(),
                            json={'venta_id': 99,
                                  'items': [{'producto_id': 1, 'cantidad': 1}]})
    assert respuesta.status_code == 404


def test_ver_salida(client, bd):
    client.post('/api/v1/admin/salidas', headers=_headers(),
                json={'items': [{'producto_id': 1, 'cantidad': 1}]})
    respuesta = client.get('/api/v1/admin/salidas/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['items'][0]['producto_nombre'] == 'Taladro'


def test_eliminar_salida_revierte_stock(client, bd):
    client.post('/api/v1/admin/salidas', headers=_headers(),
                json={'items': [{'producto_id': 1, 'cantidad': 2}]})
    assert bd.productos[1]['stock'] == 3
    respuesta = client.delete('/api/v1/admin/salidas/1', headers=_headers())
    assert respuesta.status_code == 200
    assert bd.productos[1]['stock'] == 5


def test_pdf_salida(client, bd):
    client.post('/api/v1/admin/salidas', headers=_headers(),
                json={'items': [{'producto_id': 1, 'cantidad': 1}]})
    respuesta = client.get('/api/v1/admin/salidas/1.pdf', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.mimetype == 'application/pdf'


# ─────────────────────────────────────────────
# ENTREGAS
# ─────────────────────────────────────────────
def test_listar_entregas_vacio(client, bd):
    respuesta = client.get('/api/v1/admin/entregas', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['meta']['total'] == 0


def test_crear_entrega(client, bd):
    respuesta = client.post('/api/v1/admin/entregas', headers=_headers(),
                            json={'venta_id': 1, 'direccion_envio': 'Av. Los Andes',
                                  'notas': 'Llamar'})
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['estado'] == 'pendiente'
    assert data['productos_venta'][0]['producto_id'] == 1


def test_crear_entrega_duplicada(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.post('/api/v1/admin/entregas', headers=_headers(),
                            json={'venta_id': 1})
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'ENTREGA_EXISTENTE'


def test_crear_entrega_venta_inexistente(client, bd):
    respuesta = client.post('/api/v1/admin/entregas', headers=_headers(),
                            json={'venta_id': 99})
    assert respuesta.status_code == 404


def test_ver_entrega(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.get('/api/v1/admin/entregas/1', headers=_headers())
    assert respuesta.status_code == 200


def test_patch_entrega_entregado(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.patch('/api/v1/admin/entregas/1', headers=_headers(),
                             json={'estado': 'entregado', 'notas': 'ok'})
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'entregado'
    assert bd.ventas[1]['estado'] == 'entregado'


def test_patch_entrega_cancelado_devuelve_stock(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.patch('/api/v1/admin/entregas/1', headers=_headers(),
                             json={'estado': 'cancelado'})
    assert respuesta.status_code == 200
    assert bd.ventas[1]['estado'] == 'cancelado'
    assert ('stock', 1) in bd.cancelaciones_registradas
    assert any(c[0] == 'motivo' for c in bd.cancelaciones_registradas)


def test_patch_entrega_estado_invalido(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.patch('/api/v1/admin/entregas/1', headers=_headers(),
                             json={'estado': 'volando'})
    assert respuesta.status_code == 422


def test_patch_entrega_cancelada_es_terminal(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    client.patch('/api/v1/admin/entregas/1', headers=_headers(),
                 json={'estado': 'cancelado'})
    respuesta = client.patch('/api/v1/admin/entregas/1', headers=_headers(),
                             json={'estado': 'entregado'})
    assert respuesta.status_code == 409


def test_eliminar_entrega(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.delete('/api/v1/admin/entregas/1', headers=_headers())
    assert respuesta.status_code == 200
    assert bd.seguimiento == []


def test_productos_entrega(client, bd):
    client.post('/api/v1/admin/entregas', headers=_headers(),
                json={'venta_id': 1})
    respuesta = client.post('/api/v1/admin/entregas/1/productos',
                            headers=_headers(),
                            json={'items': [{'producto_id': 3, 'cantidad': 2}]})
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['productos_entrega'][0]['cantidad'] == 2


def test_asignar_entrega(client, bd):
    respuesta = client.post('/api/v1/admin/ventas/1/entrega', headers=_headers(),
                            json={'direccion_envio': 'Jr. Puno 5',
                                  'items': [{'producto_id': 1, 'cantidad': 1}]})
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['accion'] == 'creada'
    respuesta2 = client.post('/api/v1/admin/ventas/1/entrega', headers=_headers(),
                             json={'direccion_envio': 'Jr. Puno 6'})
    assert respuesta2.get_json()['data']['accion'] == 'actualizada'


def test_asignar_entrega_venta_inexistente(client, bd):
    respuesta = client.post('/api/v1/admin/ventas/99/entrega', headers=_headers(),
                            json={})
    assert respuesta.status_code == 404


# ─────────────────────────────────────────────
# INVENTARIO
# ─────────────────────────────────────────────
def test_verificar_inventario(client, bd):
    respuesta = client.get('/api/v1/admin/inventario/verificacion',
                           headers=_headers())
    assert respuesta.status_code == 200
    meta = respuesta.get_json()['meta']
    assert meta['todos'] == 5
    assert meta['sin_stock'] == 1
    assert meta['critico'] == 1   # Taladro stock 5
    assert meta['bajo'] == 1      # Foco stock 20 (16..49)
    assert meta['normal'] == 1    # Martillo stock 60


def test_verificar_inventario_filtro(client, bd):
    respuesta = client.get('/api/v1/admin/inventario/verificacion?filtro=critico',
                           headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 1
    assert respuesta.get_json()['data'][0]['nivel'] == 'critico'


def test_verificar_inventario_inactivos(client, bd):
    respuesta = client.get(
        '/api/v1/admin/inventario/verificacion?filtro=inactivos',
        headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 1
    assert respuesta.get_json()['data'][0]['nivel'] == 'inactivo'
