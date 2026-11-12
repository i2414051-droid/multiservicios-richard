"""Pruebas de contrato de la administracion (Fase 4) sin MySQL real.

La BD de juguete interpreta las consultas que generan
`services.admin_productos_service`, `services.admin_ventas_service` y
`services.admin_clientes_service`. Se prueba el cableado HTTP, los permisos,
las reglas de negocio y la forma del JSON.
"""
from datetime import datetime
from decimal import Decimal
from io import BytesIO

import pytest
from flask_jwt_extended import create_access_token

import app as monolito
from api import security

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 12

ADMIN = {
    'id': 1, 'correo': 'admin@mail.com', 'rol': 'admin', 'nombre': 'Admin',
    'apellido': 'Root', 'telefono': '999999999', 'documento_tipo': 'dni',
    'documento_numero': '00000000', 'estado': 'activo', 'created_at': None,
    'token_version': 0,
}
CLIENTE = {
    'id': 7, 'correo': 'ana@example.com', 'rol': 'cliente', 'nombre': 'Ana',
    'apellido': 'Perez', 'telefono': '999999999', 'documento_tipo': 'dni',
    'documento_numero': '12345678', 'estado': 'activo', 'created_at': None,
    'token_version': 0,
}


def _producto(id, nombre, precio, stock, categoria='Herramientas',
              estado='activo', imagen=None):
    return {
        'id': id, 'nombre': nombre, 'descripcion': 'Producto de prueba',
        'precio': Decimal(precio), 'stock': stock, 'categoria': categoria,
        'imagen': imagen, 'imagen_datos': None, 'imagen_mime': None,
        'estado': estado, 'created_at': datetime(2026, 1, 1, 12, 0, 0),
    }


def _venta(id, cliente_id=7, correo='ana@example.com', total='120.50',
           estado='en espera', metodo_pago='transferencia', oculta=0,
           tipo_entrega='recojo', distrito='Chilca'):
    return {
        'id': id, 'cliente_id': cliente_id, 'correo': correo,
        'total': Decimal(total), 'fecha': datetime(2026, 2, 1, 10, 0, 0),
        'documento': '12345678', 'doc_tipo': 'dni', 'nombre': 'Ana',
        'apellido': 'Perez', 'telefono': '999999999', 'estado': estado,
        'estado_pago': 'pendiente', 'metodo_pago': metodo_pago,
        'tipo_entrega': tipo_entrega, 'distrito': distrito,
        'direccion_envio': 'Av. Los Andes 456', 'costo_envio': Decimal('0.00'),
        'oculta': oculta,
    }


def _usuario(id, correo, rol='cliente', estado='activo', nombre='', apellido=''):
    return {
        'id': id, 'correo': correo, 'rol': rol, 'estado': estado,
        'nombre': nombre, 'apellido': apellido, 'telefono': '999999999',
        'documento_tipo': 'dni', 'documento_numero': '12345678',
        'created_at': datetime(2026, 1, 1, 12, 0, 0),
    }


class BDJuguete:
    def __init__(self):
        self.productos = {
            1: _producto(1, 'Taladro', '120.50', 5, imagen='imagenes/t.jpg'),
            2: _producto(2, 'Cable', '10.00', 0, categoria='Electricos',
                         estado='inactivo'),
        }
        self.imagenes = [
            {'id': 1, 'producto_id': 1, 'imagen': 'imagenes/g1.jpg',
             'imagen_datos': b'x', 'imagen_mime': 'image/jpeg', 'orden': 1},
        ]
        self.ventas = [
            _venta(1, estado='en espera'),
            _venta(2, total='200.00', estado='entregado', oculta=1),
        ]
        self.detalle = [
            {'id': 1, 'venta_id': 1, 'producto_id': 1, 'cantidad': 1,
             'precio': Decimal('120.50')},
        ]
        self.seguimiento = [
            {'id': 1, 'venta_id': 1, 'estado': 'pendiente',
             'fecha_entrega': None},
        ]
        self.cancelaciones = []
        self.usuarios = [
            _usuario(1, 'admin@mail.com', rol='admin', nombre='Admin'),
            _usuario(7, 'ana@example.com', rol='cliente', nombre='Ana',
                     apellido='Perez'),
        ]
        self.historial = []
        self.usuario = dict(ADMIN)
        self._next_producto = 3
        self._next_imagen = 2
        self._next_historial = 1
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

    # ── helpers ──
    def _filtrar_ventas(self, s, params):
        filas = [dict(v) for v in self.db.ventas]
        p = list(params or ())
        if 'COALESCE(v.oculta,0)=0' in s:
            filas = [f for f in filas if not f.get('oculta')]
        buscar = str(p[0]).strip('%') if p else ''
        if buscar:
            filas = [f for f in filas if buscar in (f.get('correo') or '')]
        estados = {'en espera', 'entregado', 'cancelado'}
        if 'v.estado=%s' in s:
            valor = next((x for x in p[1:] if x in estados), None)
            if valor:
                filas = [f for f in filas if f.get('estado') == valor]
        return filas

    def execute(self, sql, params=None):
        self.db.consultas = getattr(self.db, 'consultas', [])
        self.db.consultas.append((sql, params))
        self._rows = []
        self._one = None
        self.rowcount = 0
        s = ' '.join(sql.split())
        p = tuple(params or ())

        # ── Productos: lectura ──
        if s.startswith('SELECT COUNT(*) AS total FROM') and '.productos p' in s:
            buscar = str(p[0]).strip('%') if p else ''
            self._one = {'total': len([
                x for x in self.db.productos.values()
                if buscar.lower() in (x['nombre'] or '').lower()])}
            return
        if 'productos_para_pedir' in s and s.startswith('SELECT COUNT(*) AS total'):
            self._one = {'total': 0}
            return
        if '.proveedores' in s and s.startswith('SELECT COUNT(*) AS total'):
            self._one = {'total': 3}
            return
        if s.startswith('SELECT p.* FROM') and '.productos p' in s:
            buscar = str(p[0]).strip('%') if p else ''
            filas = [dict(x) for x in self.db.productos.values()
                     if buscar.lower() in (x['nombre'] or '').lower()]
            filas.sort(key=lambda x: x['id'], reverse=True)
            self._rows = filas
            return
        if (s.startswith('SELECT id, nombre, descripcion, precio, stock, '
                         'categoria, imagen, estado, created_at FROM')):
            prod = self.db.productos.get(int(p[0]))
            self._one = dict(prod) if prod else None
            return

        # ── Productos: galeria ──
        if s.startswith('SELECT id, imagen, orden FROM'):
            self._rows = [
                dict(i) for i in self.db.imagenes
                if i['producto_id'] == int(p[0])
            ]
            return
        if s.startswith('SELECT imagen_datos, imagen_mime FROM'):
            imagen = next((i for i in self.db.imagenes
                           if i['id'] == int(p[0])
                           and i['producto_id'] == int(p[1])), None)
            self._one = {'imagen_datos': imagen.get('imagen_datos'),
                         'imagen_mime': imagen.get('imagen_mime')} if imagen \
                else None
            return

        # ── Productos: escritura ──
        if s.startswith('INSERT INTO') and '.productos ' in s and 'imagen' not in s:
            nombre, descripcion, precio, stock, categoria, estado = p
            nuevo = _producto(self.db._next_producto, nombre, precio, stock,
                              categoria, estado)
            nuevo['descripcion'] = descripcion
            self.db.productos[self.db._next_producto] = nuevo
            self.lastrowid = self.db._next_producto
            self.db._next_producto += 1
            self.rowcount = 1
            return
        if s.startswith('INSERT INTO') and '.productos_imagenes' in s:
            producto_id, imagen, datos, mime, orden = p
            self.db.imagenes.append({
                'id': self.db._next_imagen, 'producto_id': int(producto_id),
                'imagen': imagen, 'imagen_datos': datos, 'imagen_mime': mime,
                'orden': int(orden)})
            self.db._next_imagen += 1
            self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos SET nombre=' in s:
            (nombre, descripcion, precio, stock, categoria, estado,
             producto_id) = p
            prod = self.db.productos.get(int(producto_id))
            if prod:
                prod.update({'nombre': nombre, 'descripcion': descripcion,
                             'precio': Decimal(str(precio)), 'stock': int(stock),
                             'categoria': categoria, 'estado': estado})
                self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos SET estado=%s' in s:
            prod = self.db.productos.get(int(p[1]))
            if prod:
                prod['estado'] = p[0]
                self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos SET imagen=%s' in s:
            prod = self.db.productos.get(int(p[3]))
            if prod:
                prod.update({'imagen': p[0], 'imagen_datos': p[1],
                             'imagen_mime': p[2]})
                self.rowcount = 1
            return
        if s.startswith('UPDATE') and '.productos SET imagen=NULL' in s:
            prod = self.db.productos.get(int(p[0]))
            if prod:
                prod.update({'imagen': None, 'imagen_datos': None,
                             'imagen_mime': None})
                self.rowcount = 1
            return
        if s.startswith('DELETE FROM') and '.productos_imagenes WHERE producto_id=%s' in s:
            pid = int(p[0])
            antes = len(self.db.imagenes)
            self.db.imagenes = [i for i in self.db.imagenes
                                if i['producto_id'] != pid]
            self.rowcount = antes - len(self.db.imagenes)
            return
        if s.startswith('DELETE FROM') and '.productos_imagenes WHERE id=%s' in s:
            iid, pid = int(p[0]), int(p[1])
            antes = len(self.db.imagenes)
            self.db.imagenes = [i for i in self.db.imagenes
                                if not (i['id'] == iid and i['producto_id'] == pid)]
            self.rowcount = antes - len(self.db.imagenes)
            return
        if s.startswith('DELETE FROM') and '.productos WHERE id=%s' in s:
            self.rowcount = 1 if self.db.productos.pop(int(p[0]), None) else 0
            return

        # ── Ventas ──
        if 'SUM(total) AS gran_total' in s:
            total = sum(v['total'] for v in self.db.ventas
                        if not v.get('oculta') and v['estado'] == 'entregado')
            self._one = {'gran_total': total or None}
            return
        if 'SUM(total) AS monto_espera' in s:
            total = sum(v['total'] for v in self.db.ventas
                        if not v.get('oculta') and v['estado'] == 'en espera')
            self._one = {'monto_espera': total or None}
            return
        if s.startswith('SELECT COUNT(*) AS n FROM ventas WHERE oculta=1'):
            self._one = {'n': len([v for v in self.db.ventas if v.get('oculta')])}
            return
        if (s.startswith('SELECT COUNT(*) AS total FROM ventas v JOIN usuarios u')
                or s.startswith('SELECT COUNT(*) AS total FROM ventas v JOIN')):
            self._one = {'total': len(self._filtrar_ventas(s, p))}
            return
        if s.startswith('SELECT v.id, u.correo, u.id AS cliente_id'):
            filas = self._filtrar_ventas(s, p)
            filas.sort(key=lambda x: x['id'], reverse=True)
            self._rows = filas
            return
        if s.startswith('SELECT v.id, u.correo, v.total, v.fecha, v.documento'):
            filas = self._filtrar_ventas(s, p)
            filas.sort(key=lambda x: x['id'], reverse=True)
            self._rows = filas
            return
        if s.startswith('SELECT v.*, u.correo'):
            venta = next((v for v in self.db.ventas if v['id'] == int(p[0])), None)
            if venta:
                fila = dict(venta)
                motivo = next((c for c in self.db.cancelaciones
                               if c['venta_id'] == venta['id']), None)
                fila['motivo_codigo'] = motivo['motivo_codigo'] if motivo else None
                fila['motivo_detalle'] = motivo['motivo_detalle'] if motivo else None
                fila['cancelado_por'] = motivo['cancelado_por'] if motivo else None
                self._one = fila
            return
        if s.startswith('SELECT p.id AS producto_id') and 'FROM detalle_venta d JOIN' in s:
            venta_id = int(p[0])
            resultado = []
            for linea in self.db.detalle:
                if linea['venta_id'] != venta_id:
                    continue
                prod = self.db.productos.get(linea['producto_id'], {})
                resultado.append({
                    'producto_id': linea['producto_id'],
                    'nombre': prod.get('nombre'), 'descripcion': None,
                    'imagen': prod.get('imagen'), 'cantidad': linea['cantidad'],
                    'precio': linea['precio']})
            self._rows = resultado
            return
        if s.startswith('SELECT id, estado FROM ventas WHERE id=%s'):
            venta = next((v for v in self.db.ventas if v['id'] == int(p[0])), None)
            self._one = {'id': venta['id'], 'estado': venta['estado']} if venta else None
            return
        if s.startswith('SELECT id FROM ventas WHERE id=%s'):
            venta = next((v for v in self.db.ventas if v['id'] == int(p[0])), None)
            self._one = {'id': venta['id']} if venta else None
            return
        if s.startswith('UPDATE ventas SET estado=%s'):
            venta = next((v for v in self.db.ventas if v['id'] == int(p[1])), None)
            if venta:
                venta['estado'] = p[0]
                self.rowcount = 1
            return
        if s.startswith('UPDATE ventas SET oculta=%s'):
            venta = next((v for v in self.db.ventas if v['id'] == int(p[1])), None)
            if venta:
                venta['oculta'] = int(p[0])
                self.rowcount = 1
            return
        if s.startswith('UPDATE seguimiento_entregas'):
            for seg in self.db.seguimiento:
                if seg['venta_id'] == int(p[-1]):
                    seg['estado'] = 'entregado' if 'fecha_entrega' in s else p[0]
            self.rowcount = 1
            return
        if s.startswith('SELECT producto_id, cantidad FROM detalle_venta'):
            self._rows = [{'producto_id': d['producto_id'],
                           'cantidad': d['cantidad']}
                          for d in self.db.detalle if d['venta_id'] == int(p[0])]
            return
        if s.startswith('INSERT IGNORE INTO cancelaciones'):
            self.db.cancelaciones.append({
                'venta_id': int(p[0]), 'motivo_codigo': p[1],
                'motivo_detalle': p[2], 'cancelado_por': p[3]})
            self.rowcount = 1
            return
        if s.startswith('DELETE FROM detalle_venta'):
            self.rowcount = len(self.db.detalle)
            self.db.detalle = []
            return
        if s.startswith('DELETE FROM ventas'):
            self.rowcount = len(self.db.ventas)
            self.db.ventas = []
            return
        if s.startswith('UPDATE') and '.productos SET stock=stock+%s' in s:
            prod = self.db.productos.get(int(p[1]))
            if prod:
                prod['stock'] = int(prod['stock']) + int(p[0])
                self.rowcount = 1
            return

        # ── Clientes ──
        if s.startswith('SELECT COUNT(*) AS total FROM usuarios u'):
            self._one = {'total': len(self.db.usuarios)}
            return
        if 'GROUP BY COALESCE(estado' in s:
            conteos = {}
            for u in self.db.usuarios:
                conteos[u['estado']] = conteos.get(u['estado'], 0) + 1
            self._rows = [{'e': k, 'n': v} for k, v in conteos.items()]
            return
        if s.startswith('SELECT COUNT(*) AS total FROM usuarios WHERE correo LIKE'):
            buscar = str(p[0]).strip('%')
            self._one = {'total': len([u for u in self.db.usuarios
                                       if buscar in u['correo']])}
            return
        if s.startswith('SELECT id, correo, rol, COALESCE(estado') and \
                'WHERE correo LIKE' in s:
            buscar = str(p[0]).strip('%')
            self._rows = [dict(u, estado=u['estado']) for u in self.db.usuarios
                          if buscar in u['correo']]
            return
        if s.startswith('SELECT id, correo, rol, COALESCE(estado') and \
                'WHERE id=%s' in s:
            u = next((x for x in self.db.usuarios if x['id'] == int(p[0])), None)
            self._one = {'id': u['id'], 'correo': u['correo'], 'rol': u['rol'],
                         'estado': u['estado']} if u else None
            return
        if s.startswith('SELECT u.id, u.correo') and 'FROM usuarios u WHERE u.id=%s' in s:
            u = next((x for x in self.db.usuarios if x['id'] == int(p[0])), None)
            if u:
                self._one = dict(u)
                self._one['num_compras'] = len(
                    [v for v in self.db.ventas
                     if v['cliente_id'] == u['id'] and v['estado'] != 'cancelado'])
                self._one['total_compras'] = Decimal('0')
            return
        if s.startswith('SELECT u.id, u.correo') and 'num_compras' in s:
            filas = [dict(u) for u in self.db.usuarios]
            filas.sort(key=lambda x: x['id'], reverse=True)
            for f in filas:
                f['num_compras'] = 0
                f['total_compras'] = Decimal('0')
            self._rows = filas
            return
        if s.startswith('UPDATE usuarios SET estado=%s'):
            u = next((x for x in self.db.usuarios if x['id'] == int(p[1])), None)
            if u:
                u['estado'] = p[0]
                self.rowcount = 1
            return
        if s.startswith('INSERT INTO historial_estado_cliente'):
            usuario_id, anterior, nuevo, motivo, admin_id = p
            self.db.historial.append({
                'id': self.db._next_historial,
                'usuario_id': int(usuario_id), 'estado_anterior': anterior,
                'estado_nuevo': nuevo, 'motivo': motivo,
                'created_at': datetime(2026, 3, 1, 9, 0, 0),
                'admin_id': admin_id})
            self.db._next_historial += 1
            self.rowcount = 1
            return
        if s.startswith('SELECT h.id, h.estado_anterior'):
            self._rows = [dict(h) for h in self.db.historial
                          if h['usuario_id'] == int(p[0])]
            return
        if s.startswith('SELECT id FROM usuarios WHERE id=%s'):
            u = next((x for x in self.db.usuarios if x['id'] == int(p[0])), None)
            self._one = {'id': u['id']} if u else None
            return
        if s.startswith('UPDATE usuarios SET rol=%s'):
            u = next((x for x in self.db.usuarios if x['id'] == int(p[1])), None)
            if u:
                u['rol'] = p[0]
                self.rowcount = 1
            return

    def fetchone(self):
        if self._one is not None:
            return self._one
        return self._rows[0] if self._rows else None

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
    return db


def _headers(rol='admin'):
    with monolito.app.app_context():
        token = create_access_token(
            identity='1', additional_claims={'ver': 0, 'rol': rol})
    return {'Authorization': 'Bearer %s' % token}


# ─────────────────────────────────────────────
# PERMISOS
# ─────────────────────────────────────────────
def test_admin_productos_requiere_admin(client, bd):
    bd.usuario = dict(CLIENTE)
    respuesta = client.get('/api/v1/admin/productos', headers=_headers('cliente'))
    assert respuesta.status_code == 403
    assert respuesta.get_json()['error']['code'] == 'SIN_PERMISO'


def test_admin_sin_token(client, bd):
    respuesta = client.get('/api/v1/admin/ventas')
    assert respuesta.status_code == 401


# ─────────────────────────────────────────────
# PRODUCTOS
# ─────────────────────────────────────────────
def test_admin_listar_productos(client, bd):
    respuesta = client.get('/api/v1/admin/productos', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 2
    assert cuerpo['data'][0]['id'] == 2
    assert cuerpo['data'][0]['estado'] == 'inactivo'
    assert cuerpo['data'][1]['imagenes_detalle'][0]['id'] == 1


def test_admin_listar_productos_buscar(client, bd):
    respuesta = client.get('/api/v1/admin/productos?buscar=Taladro',
                           headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 1


def test_admin_crear_producto(client, bd):
    respuesta = client.post('/api/v1/admin/productos',
                            json={'nombre': 'Martillo', 'precio': '25.00',
                                  'stock': 10, 'categoria': 'Herramientas'},
                            headers=_headers())
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['id'] == 3
    assert data['precio'] == '25.00'
    assert data['estado'] == 'activo'
    assert bd.productos[3]['nombre'] == 'Martillo'


def test_admin_crear_producto_precio_negativo(client, bd):
    respuesta = client.post('/api/v1/admin/productos',
                            json={'nombre': 'X', 'precio': '-1', 'stock': 1},
                            headers=_headers())
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'VALIDACION'


def test_admin_ver_producto(client, bd):
    respuesta = client.get('/api/v1/admin/productos/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['nombre'] == 'Taladro'


def test_admin_ver_producto_inexistente(client, bd):
    respuesta = client.get('/api/v1/admin/productos/99', headers=_headers())
    assert respuesta.status_code == 404


def test_admin_actualizar_producto(client, bd):
    respuesta = client.patch('/api/v1/admin/productos/1',
                             json={'stock': 20}, headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['stock'] == 20
    assert bd.productos[1]['stock'] == 20
    assert bd.productos[1]['nombre'] == 'Taladro'


def test_admin_desactivar_producto(client, bd):
    respuesta = client.delete('/api/v1/admin/productos/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'inactivo'
    assert bd.productos[1]['estado'] == 'inactivo'


def test_admin_activar_producto(client, bd):
    respuesta = client.post('/api/v1/admin/productos/2/activar',
                            headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'activo'


def test_admin_eliminar_definitivo(client, bd):
    respuesta = client.delete('/api/v1/admin/productos/1?definitivo=true',
                              headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['definitivo'] is True
    assert 1 not in bd.productos
    assert all(i['producto_id'] != 1 for i in bd.imagenes)


# ─────────────────────────────────────────────
# IMAGENES
# ─────────────────────────────────────────────
def test_admin_subir_imagen(client, bd):
    respuesta = client.post(
        '/api/v1/admin/productos/2/imagen',
        data={'imagenes': (BytesIO(PNG), 'foto.png')},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 200
    galeria = [i for i in bd.imagenes if i['producto_id'] == 2]
    assert len(galeria) == 1
    assert galeria[0]['imagen'].startswith('uploads/')


def test_admin_subir_imagen_principal(client, bd):
    respuesta = client.post(
        '/api/v1/admin/productos/1/imagen',
        data={'imagenes': (BytesIO(PNG), 'nueva.png'), 'principal': 'true'},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 200
    # La principal cambio y la galeria existente sigue.
    assert bd.productos[1]['imagen'].startswith('uploads/')
    assert len([i for i in bd.imagenes if i['producto_id'] == 1]) == 1


def test_admin_subir_imagen_invalida(client, bd):
    respuesta = client.post(
        '/api/v1/admin/productos/2/imagen',
        data={'imagenes': (BytesIO(b'no soy imagen'), 'x.txt')},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'IMAGEN_INVALIDA'


def test_admin_subir_imagen_maximo(client, bd):
    # 1 principal + 3 galeria = 4: una mas debe fallar.
    for i in range(2, 5):
        bd.imagenes.append({'id': 10 + i, 'producto_id': 1,
                            'imagen': 'imagenes/g%d.jpg' % i,
                            'imagen_datos': b'x', 'imagen_mime': 'image/png',
                            'orden': i})
    respuesta = client.post(
        '/api/v1/admin/productos/1/imagen',
        data={'imagenes': (BytesIO(PNG), 'extra.png')},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'IMAGENES_MAXIMO'


def test_admin_subir_imagen_principal_no_infla_el_tope(client, bd):
    # Producto 1: principal + 1 galeria = 2. Reemplazar la principal y sumar 2
    # a la galeria deja exactamente 4: debe entrar.
    respuesta = client.post(
        '/api/v1/admin/productos/1/imagen',
        data={'imagenes': [(BytesIO(PNG), 'a.png'),
                           (BytesIO(PNG), 'b.png'),
                           (BytesIO(PNG), 'c.png')],
              'principal': 'true'},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 200
    assert len([i for i in bd.imagenes if i['producto_id'] == 1]) == 3


def test_admin_subir_imagen_principal_sin_previa_valida_tope(client, bd):
    # Producto 2 no tiene principal ni galeria: 5 archivos como principal
    # quedarian en 5 y debe rechazarse.
    respuesta = client.post(
        '/api/v1/admin/productos/2/imagen',
        data={'imagenes': (BytesIO(PNG), 'a.png'),
              'principal': 'true'},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 200
    # Ahora tiene principal + nada de galeria; 4 mas harian 5.
    bd.imagenes.append({'id': 90, 'producto_id': 2, 'imagen': 'imagenes/x.jpg',
                        'imagen_datos': b'x', 'imagen_mime': 'image/png',
                        'orden': 1})
    bd.imagenes.append({'id': 91, 'producto_id': 2, 'imagen': 'imagenes/y.jpg',
                        'imagen_datos': b'x', 'imagen_mime': 'image/png',
                        'orden': 2})
    bd.imagenes.append({'id': 92, 'producto_id': 2, 'imagen': 'imagenes/z.jpg',
                        'imagen_datos': b'x', 'imagen_mime': 'image/png',
                        'orden': 3})
    respuesta = client.post(
        '/api/v1/admin/productos/2/imagen',
        data={'imagenes': [(BytesIO(PNG), 'w1.png'),
                           (BytesIO(PNG), 'w2.png')]},
        headers=_headers(), content_type='multipart/form-data')
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'IMAGENES_MAXIMO'


def test_admin_eliminar_imagen(client, bd):
    respuesta = client.delete('/api/v1/admin/productos/1/imagenes/1',
                              headers=_headers())
    assert respuesta.status_code == 200
    assert bd.imagenes == []


def test_admin_quitar_principal_promueve(client, bd):
    respuesta = client.delete('/api/v1/admin/productos/1/imagen',
                              headers=_headers())
    assert respuesta.status_code == 200
    # La galeria paso a ser la principal y quedo vacia.
    assert bd.productos[1]['imagen'] == 'imagenes/g1.jpg'
    assert bd.imagenes == []


# ─────────────────────────────────────────────
# VENTAS
# ─────────────────────────────────────────────
def test_admin_listar_ventas(client, bd):
    respuesta = client.get('/api/v1/admin/ventas', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    # Por defecto las ocultas no aparecen.
    assert cuerpo['meta']['total'] == 1
    assert cuerpo['meta']['monto_en_espera'] == '120.50'
    assert cuerpo['meta']['ocultas'] == 1
    assert cuerpo['data'][0]['cliente']['correo'] == 'ana@example.com'


def test_admin_listar_ventas_ocultas(client, bd):
    respuesta = client.get('/api/v1/admin/ventas?ocultas=1', headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 2


def test_admin_listar_ventas_filtro_estado(client, bd):
    respuesta = client.get('/api/v1/admin/ventas?estado=entregado',
                           headers=_headers())
    # La unica entregada esta oculta, asi que no sale.
    assert respuesta.get_json()['meta']['total'] == 0


def test_admin_ver_venta(client, bd):
    respuesta = client.get('/api/v1/admin/ventas/1', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['items'][0]['nombre'] == 'Taladro'


def test_admin_estado_venta_entregado(client, bd):
    respuesta = client.patch('/api/v1/admin/ventas/1/estado',
                             json={'estado': 'entregado'}, headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'entregado'
    assert bd.ventas[0]['estado'] == 'entregado'


def test_admin_estado_venta_cancelado_devuelve_stock(client, bd):
    respuesta = client.patch('/api/v1/admin/ventas/1/estado',
                             json={'estado': 'cancelado'}, headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'cancelado'
    assert bd.productos[1]['stock'] == 6
    assert bd.cancelaciones[0]['motivo_codigo'] == 'otro'


def test_admin_estado_venta_invalido(client, bd):
    respuesta = client.patch('/api/v1/admin/ventas/1/estado',
                             json={'estado': 'volando'}, headers=_headers())
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'ESTADO_INVALIDO'


def test_admin_estado_venta_ya_cancelada(client, bd):
    bd.ventas[0]['estado'] = 'cancelado'
    respuesta = client.patch('/api/v1/admin/ventas/1/estado',
                             json={'estado': 'entregado'}, headers=_headers())
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'VENTA_CANCELADA'


def test_admin_visibilidad_venta(client, bd):
    respuesta = client.patch('/api/v1/admin/ventas/1/visibilidad',
                             json={'oculta': True}, headers=_headers())
    assert respuesta.status_code == 200
    assert bd.ventas[0]['oculta'] == 1


def test_admin_export_csv(client, bd):
    respuesta = client.get('/api/v1/admin/ventas/export.csv', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.mimetype == 'text/csv'
    assert 'ana@example.com' in respuesta.get_data(as_text=True)
    assert 'attachment' in respuesta.headers['Content-Disposition']


def test_admin_export_pdf(client, bd):
    respuesta = client.get('/api/v1/admin/ventas/export.pdf', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.mimetype == 'application/pdf'
    assert respuesta.data.startswith(b'%PDF')


def test_admin_limpiar_ventas(client, bd):
    respuesta = client.delete('/api/v1/admin/ventas/ocultas', headers=_headers())
    assert respuesta.status_code == 200
    assert bd.ventas == []
    assert bd.detalle == []


# ─────────────────────────────────────────────
# CLIENTES Y PERMISOS
# ─────────────────────────────────────────────
def test_admin_listar_clientes(client, bd):
    respuesta = client.get('/api/v1/admin/clientes', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 2
    assert cuerpo['meta']['conteos']['activo'] == 2
    assert cuerpo['data'][0]['estado_detalle']['nombre']


def test_admin_ver_cliente(client, bd):
    respuesta = client.get('/api/v1/admin/clientes/7', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['correo'] == 'ana@example.com'


def test_admin_estado_cliente(client, bd):
    respuesta = client.patch('/api/v1/admin/clientes/7/estado',
                             json={'estado': 'moroso', 'motivo': 'Debe cuota'},
                             headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['cambiado'] is True
    assert data['estado'] == 'moroso'
    assert bd.usuarios[1]['estado'] == 'moroso'
    assert bd.historial[0]['estado_nuevo'] == 'moroso'


def test_admin_estado_invalido(client, bd):
    respuesta = client.patch('/api/v1/admin/clientes/7/estado',
                             json={'estado': 'desconocido'}, headers=_headers())
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'ESTADO_INVALIDO'


def test_admin_no_puede_autobloquearse(client, bd):
    respuesta = client.patch('/api/v1/admin/clientes/1/estado',
                             json={'estado': 'bloqueado'}, headers=_headers())
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'AUTO_BLOQUEO'


def test_admin_no_superadmin_no_cambia_estado_admin(client, bd):
    bd.usuario = dict(ADMIN, id=2, correo='jefe@example.com')
    bd.usuarios.append(_usuario(2, 'jefe@example.com', rol='admin'))
    respuesta = client.patch('/api/v1/admin/clientes/1/estado',
                             json={'estado': 'bloqueado'}, headers=_headers())
    assert respuesta.status_code == 403
    assert respuesta.get_json()['error']['code'] == 'SIN_PERMISO'


def test_admin_historial_cliente(client, bd):
    bd.historial.append({'id': 1, 'usuario_id': 7, 'estado_anterior': 'activo',
                         'estado_nuevo': 'moroso', 'motivo': 'Debe',
                         'created_at': datetime(2026, 3, 1, 9, 0, 0),
                         'admin_id': 1})
    respuesta = client.get('/api/v1/admin/clientes/7/historial',
                           headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data'][0]['nuevo'] == 'moroso'


def test_admin_listar_usuarios(client, bd):
    respuesta = client.get('/api/v1/admin/usuarios', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 2
    assert cuerpo['meta']['es_superadmin'] is True
    roles = {u['correo']: u['rol'] for u in cuerpo['data']}
    assert roles['admin@mail.com'] == 'admin'


def test_admin_cambiar_rol_superadmin(client, bd):
    respuesta = client.patch('/api/v1/admin/usuarios/7/rol',
                             json={'rol': 'admin'}, headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['rol'] == 'admin'
    assert bd.usuarios[1]['rol'] == 'admin'


def test_admin_cambiar_rol_no_superadmin(client, bd):
    bd.usuario = dict(ADMIN, id=2, correo='jefe@example.com')
    respuesta = client.patch('/api/v1/admin/usuarios/7/rol',
                             json={'rol': 'admin'}, headers=_headers())
    assert respuesta.status_code == 403


def test_admin_cambiar_rol_invalido(client, bd):
    respuesta = client.patch('/api/v1/admin/usuarios/7/rol',
                             json={'rol': 'superadmin'}, headers=_headers())
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'ROL_INVALIDO'
