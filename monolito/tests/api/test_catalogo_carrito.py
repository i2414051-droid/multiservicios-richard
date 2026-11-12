"""Pruebas de contrato de catalogo y carrito sin MySQL real.

Se usa una base de datos de juguete que interpreta las consultas que genera
`services.tienda_service`. Asi se prueba el cableado HTTP, los serializers,
los guards JWT, las validaciones de stock y la forma del JSON.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

import app as monolito
from api import security
from services import tienda_service

USUARIO = {
    'id': 7,
    'correo': 'cliente@example.com',
    'rol': 'cliente',
    'nombre': 'Ana',
    'apellido': 'Perez',
    'telefono': '',
    'documento_tipo': '',
    'documento_numero': '',
    'estado': 'activo',
    'created_at': None,
    'token_version': 0,
}


def _producto(id, nombre, precio, stock, categoria, imagen, estado='activo'):
    return {
        'id': id,
        'nombre': nombre,
        'descripcion': 'Producto de prueba',
        'precio': Decimal(precio),
        'stock': stock,
        'categoria': categoria,
        'imagen': imagen,
        'estado': estado,
        'created_at': datetime(2026, 1, 1, 12, 0, 0),
    }


class BDJuguete:
    def __init__(self):
        self.consultas = []
        self.productos = {
            1: _producto(1, 'Taladro', '120.50', 5, 'Herramientas',
                         'imagenes/taladro.jpg'),
            2: _producto(2, 'Cable', '10.00', 0, 'Electricos',
                         'imagenes/cable.jpg'),
        }
        self.imagenes = [
            {'producto_id': 1, 'imagen': 'imagenes/taladro-2.jpg'},
            {'producto_id': 1, 'imagen': 'imagenes/taladro-3.jpg'},
        ]
        self.ajustes = [
            {'clave': 'envio_delivery', 'valor': '15'},
            {'clave': 'envio_gratis_desde', 'valor': '300'},
            {'clave': 'delivery_minimo', 'valor': '50'},
            {'clave': 'empresa_direccion', 'valor': 'Av. Test 123'},
            {'clave': 'empresa_lat', 'valor': '-12.07'},
            {'clave': 'empresa_lon', 'valor': '-75.19'},
        ]
        self.carrito = []
        self._next_item = 1
        # Flask-MySQLdb expone mysql.connection.cursor()
        self.connection = self

    def cursor(self):
        return CursorFalso(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def productos_activos(self):
        return [
            p for p in self.productos.values()
            if (p.get('estado') or 'activo') == 'activo'
        ]

    def categorias(self):
        mapa = {}
        for producto in self.productos_activos():
            nombre = producto.get('categoria')
            if not nombre:
                continue
            fila = mapa.setdefault(nombre, {
                'categoria': nombre,
                'total_productos': 0,
                'disponibles': 0,
            })
            fila['total_productos'] += 1
            if int(producto.get('stock') or 0) > 0:
                fila['disponibles'] += 1
        return list(mapa.values())

    def fila_carrito(self, item):
        producto = self.productos.get(item['producto_id'])
        if not producto:
            return None
        return {
            'item_id': item['item_id'],
            'producto_id': item['producto_id'],
            'cantidad': item['cantidad'],
            'agregado_en': item.get('created_at'),
            'nombre': producto['nombre'],
            'descripcion': producto.get('descripcion'),
            'precio': producto['precio'],
            'stock': producto['stock'],
            'categoria': producto.get('categoria'),
            'imagen': producto.get('imagen'),
            'estado': producto.get('estado', 'activo'),
        }


class CursorFalso:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self._one = None
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self.db.consultas.append((sql, params))
        self._rows = []
        self._one = None
        self.rowcount = 0
        s = ' '.join(sql.split())
        p = tuple(params or ())

        if 'productos_imagenes' in s:
            ids = {int(x) for x in p}
            self._rows = [
                i for i in self.db.imagenes
                if int(i['producto_id']) in ids
            ]
            return
        if 'GROUP BY categoria' in s:
            self._rows = self.db.categorias()
            return
        if 'FROM ajustes' in s:
            claves = {str(x) for x in p}
            self._rows = [
                a for a in self.db.ajustes if a['clave'] in claves
            ]
            return
        if 'FROM carrito c JOIN' in s:
            if 'AND c.producto_id=%s' in s:
                usuario_id, producto_id = str(p[0]), int(p[1])
                self._rows = [
                    self.db.fila_carrito(c) for c in self.db.carrito
                    if str(c['usuario_id']) == usuario_id
                    and int(c['producto_id']) == producto_id
                ]
            else:
                usuario_id = str(p[0])
                self._rows = [
                    self.db.fila_carrito(c) for c in self.db.carrito
                    if str(c['usuario_id']) == usuario_id
                ]
            self._rows = [r for r in self._rows if r]
            return
        if s.startswith('SELECT COUNT(*) AS total FROM') and 'productos' in s:
            self._one = {'total': len(self.db.productos_activos())}
            return
        if s.startswith('SELECT id, nombre, descripcion') and 'productos' in s:
            if 'WHERE id=%s' in s:
                self._one = self.db.productos.get(int(p[0]))
            else:
                self._rows = self.db.productos_activos()
            return
        if s.startswith('INSERT INTO carrito'):
            usuario_id, producto_id, cantidad = str(p[0]), int(p[1]), int(p[2])
            self.db.carrito.append({
                'item_id': self.db._next_item,
                'usuario_id': usuario_id,
                'producto_id': producto_id,
                'cantidad': cantidad,
                'created_at': datetime(2026, 1, 1, 12, 0, 0),
            })
            self.lastrowid = self.db._next_item
            self.db._next_item += 1
            self.rowcount = 1
            return
        if s.startswith('UPDATE carrito SET cantidad=%s WHERE id=%s'):
            cantidad, item_id = int(p[0]), int(p[1])
            for item in self.db.carrito:
                if int(item['item_id']) == item_id:
                    item['cantidad'] = cantidad
                    self.rowcount += 1
            return
        if s.startswith(
                'UPDATE carrito SET cantidad=%s '
                'WHERE usuario_id=%s AND producto_id=%s'):
            cantidad, usuario_id, producto_id = int(p[0]), str(p[1]), int(p[2])
            for item in self.db.carrito:
                if (str(item['usuario_id']) == usuario_id
                        and int(item['producto_id']) == producto_id):
                    item['cantidad'] = cantidad
                    self.rowcount += 1
            return
        if s.startswith('DELETE FROM carrito WHERE id IN'):
            ids = {int(x) for x in p}
            antes = len(self.db.carrito)
            self.db.carrito = [
                c for c in self.db.carrito if int(c['item_id']) not in ids
            ]
            self.rowcount = antes - len(self.db.carrito)
            return
        if s.startswith(
                'DELETE FROM carrito WHERE usuario_id=%s '
                'AND producto_id=%s'):
            usuario_id, producto_id = str(p[0]), int(p[1])
            antes = len(self.db.carrito)
            self.db.carrito = [
                c for c in self.db.carrito
                if not (str(c['usuario_id']) == usuario_id
                        and int(c['producto_id']) == producto_id)
            ]
            self.rowcount = antes - len(self.db.carrito)
            return
        if s.startswith('DELETE FROM carrito WHERE usuario_id=%s'):
            usuario_id = str(p[0])
            antes = len(self.db.carrito)
            self.db.carrito = [
                c for c in self.db.carrito
                if str(c['usuario_id']) != usuario_id
            ]
            self.rowcount = antes - len(self.db.carrito)
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
def client():
    monolito.app.config.update(TESTING=True)
    with monolito.app.test_client() as cliente:
        yield cliente


@pytest.fixture()
def bd(monkeypatch):
    db = BDJuguete()
    monkeypatch.setattr('api.deps.get_mysql', lambda: db)
    monkeypatch.setattr(security, 'get_mysql', lambda: db)
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, '_schema_listo', True)
    monkeypatch.setattr(
        'services.auth_service.buscar_usuario_por_id',
        lambda _cur, _id: USUARIO,
    )
    return db


def _headers():
    with monolito.app.app_context():
        token = create_access_token(
            identity='7',
            additional_claims={'ver': 0, 'rol': 'cliente'},
        )
    return {'Authorization': 'Bearer %s' % token}


# ─────────────────────────────────────────────
# CATALOGO
# ─────────────────────────────────────────────
def test_categorias(client, bd):
    respuesta = client.get('/api/v1/categorias')
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    nombres = [c['nombre'] for c in data]
    assert nombres[:5] == tienda_service.CATEGORIAS

    herramientas = data[0]
    assert herramientas['nombre'] == 'Herramientas'
    assert herramientas['total_productos'] == 1
    assert herramientas['disponibles'] == 1

    electricos = next(c for c in data if c['nombre'] == 'Electricos')
    assert electricos['total_productos'] == 1
    assert electricos['disponibles'] == 0


def test_productos(client, bd):
    respuesta = client.get(
        '/api/v1/productos?buscar=taladro&categoria=Herramientas'
        '&page=1&per_page=10'
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 2
    assert cuerpo['meta']['page'] == 1
    assert cuerpo['meta']['per_page'] == 10
    assert cuerpo['meta']['buscar'] == 'taladro'
    assert cuerpo['meta']['categoria'] == 'Herramientas'

    taladro = next(p for p in cuerpo['data'] if p['id'] == 1)
    assert taladro['precio'] == '120.50'
    assert taladro['precio_centimos'] == 12050
    assert taladro['disponible'] is True
    assert taladro['imagen'].endswith(
        '/static/imagenes/thumbs/taladro.jpg')
    assert taladro['imagen_original'].endswith(
        '/static/imagenes/taladro.jpg')
    assert len(taladro['imagenes']) == 3
    assert len(taladro['imagenes_thumb']) == 3
    assert taladro['imagenes_extra'][0].endswith(
        '/static/imagenes/taladro-2.jpg')

    cable = next(p for p in cuerpo['data'] if p['id'] == 2)
    assert cable['disponible'] is False

    consultas = ' '.join(q for q, _ in bd.consultas)
    assert 'nombre LIKE %s' in consultas
    assert 'categoria=%s' in consultas


def test_producto_detalle(client, bd):
    respuesta = client.get('/api/v1/productos/1')
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['nombre'] == 'Taladro'
    assert len(data['imagenes']) == 3
    assert data['imagenes_thumb'][0].endswith('/thumbs/taladro.jpg')
    assert data['created_at'] == '2026-01-01T12:00:00'


def test_producto_sin_imagen(client, bd):
    bd.productos[1]['imagen'] = None
    bd.imagenes = []
    respuesta = client.get('/api/v1/productos/1')
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['imagen'] is None
    assert data['imagenes'] == []


def test_producto_inexistente(client, bd):
    respuesta = client.get('/api/v1/productos/999')
    assert respuesta.status_code == 404
    assert respuesta.get_json()['error']['code'] == 'PRODUCTO_NO_ENCONTRADO'


def test_imagenes_producto(client, bd):
    respuesta = client.get('/api/v1/productos/1/imagenes')
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['producto_id'] == 1
    assert len(data['imagenes']) == 3
    assert len(data['imagenes_thumb']) == 3


def test_orden_invalido(client, bd):
    respuesta = client.get('/api/v1/productos?orden=caro')
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'ORDEN_INVALIDO'


def test_config_tienda(client, bd):
    respuesta = client.get('/api/v1/config/tienda')
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['moneda'] == 'PEN'
    assert data['empresa']['direccion'] == 'Av. Test 123'
    assert data['empresa']['lat'] == -12.07
    assert data['empresa']['lon'] == -75.19
    assert data['envio']['costo'] == '15.00'
    assert data['envio']['gratis_desde'] == '300.00'
    assert data['envio']['minimo_delivery'] == '50.00'
    assert data['envio']['distritos'] == [
        'Chilca', 'El Tambo', 'Huancayo']
    assert data['categorias'] == tienda_service.CATEGORIAS


# ─────────────────────────────────────────────
# CARRITO
# ─────────────────────────────────────────────
def test_carrito_sin_token(client, bd):
    respuesta = client.get('/api/v1/carrito')
    assert respuesta.status_code == 401
    assert respuesta.get_json()['error']['code'] == 'NO_AUTENTICADO'


def test_carrito_vacio(client, bd):
    respuesta = client.get('/api/v1/carrito', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['items'] == []
    assert data['cantidad_items'] == 0
    assert data['lineas'] == 0
    assert data['total'] == '0.00'
    assert data['envio']['elegible_delivery'] is False
    assert data['envio']['costo_estimado'] == '0.00'


def test_agregar_item(client, bd):
    respuesta = client.post(
        '/api/v1/carrito/items',
        json={'producto_id': 1, 'cantidad': 2},
        headers=_headers(),
    )
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['item']['producto_id'] == 1
    assert data['item']['cantidad'] == 2
    assert data['item']['subtotal'] == '241.00'
    assert data['item']['puede_comprar'] is True
    assert data['carrito']['cantidad_items'] == 2
    assert data['carrito']['lineas'] == 1
    assert data['carrito']['total'] == '241.00'
    assert data['carrito']['total_con_envio'] == '256.00'
    assert len(bd.carrito) == 1
    assert bd.carrito[0]['cantidad'] == 2


def test_agregar_item_acumula(client, bd):
    headers = _headers()
    client.post('/api/v1/carrito/items',
                json={'producto_id': 1, 'cantidad': 2},
                headers=headers)
    respuesta = client.post('/api/v1/carrito/items',
                            json={'producto_id': 1, 'cantidad': 3},
                            headers=headers)
    assert respuesta.status_code == 201
    assert respuesta.get_json()['data']['item']['cantidad'] == 5
    assert len(bd.carrito) == 1


def test_agregar_item_stock_insuficiente(client, bd):
    bd.productos[1]['stock'] = 4
    respuesta = client.post(
        '/api/v1/carrito/items',
        json={'producto_id': 1, 'cantidad': 5},
        headers=_headers(),
    )
    assert respuesta.status_code == 409
    error = respuesta.get_json()['error']
    assert error['code'] == 'STOCK_INSUFICIENTE'
    assert error['details']['stock'] == 4
    assert bd.carrito == []


def test_agregar_item_sin_stock(client, bd):
    bd.productos[1]['stock'] = 0
    respuesta = client.post(
        '/api/v1/carrito/items',
        json={'producto_id': 1, 'cantidad': 1},
        headers=_headers(),
    )
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'SIN_STOCK'


def test_agregar_item_inexistente(client, bd):
    respuesta = client.post(
        '/api/v1/carrito/items',
        json={'producto_id': 999, 'cantidad': 1},
        headers=_headers(),
    )
    assert respuesta.status_code == 404
    assert respuesta.get_json()['error']['code'] == 'PRODUCTO_NO_ENCONTRADO'


def test_agregar_item_inactivo(client, bd):
    bd.productos[1]['estado'] = 'inactivo'
    respuesta = client.post(
        '/api/v1/carrito/items',
        json={'producto_id': 1, 'cantidad': 1},
        headers=_headers(),
    )
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'PRODUCTO_NO_DISPONIBLE'


def test_cantidad_fuera_de_rango(client, bd):
    for cantidad in (0, 501):
        respuesta = client.post(
            '/api/v1/carrito/items',
            json={'producto_id': 1, 'cantidad': cantidad},
            headers=_headers(),
        )
        assert respuesta.status_code == 422
        assert respuesta.get_json()['error']['code'] == 'VALIDACION'


def test_tope_de_unidades(client, bd):
    bd.productos[1]['stock'] = 600
    bd.carrito = [{
        'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
        'cantidad': 500, 'created_at': None,
    }]
    bd._next_item = 2
    respuesta = client.post(
        '/api/v1/carrito/items',
        json={'producto_id': 1, 'cantidad': 1},
        headers=_headers(),
    )
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'CANTIDAD_MAXIMA'


def test_actualizar_item(client, bd):
    bd.carrito = [{
        'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
        'cantidad': 2, 'created_at': None,
    }]
    bd._next_item = 2
    respuesta = client.patch(
        '/api/v1/carrito/items/1',
        json={'cantidad': 3},
        headers=_headers(),
    )
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['eliminado'] is False
    assert data['item']['cantidad'] == 3
    assert bd.carrito[0]['cantidad'] == 3


def test_actualizar_item_a_cero(client, bd):
    bd.carrito = [{
        'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
        'cantidad': 2, 'created_at': None,
    }]
    bd._next_item = 2
    respuesta = client.patch(
        '/api/v1/carrito/items/1',
        json={'cantidad': 0},
        headers=_headers(),
    )
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['eliminado'] is True
    assert data['item'] is None
    assert bd.carrito == []


def test_actualizar_item_inexistente(client, bd):
    respuesta = client.patch(
        '/api/v1/carrito/items/1',
        json={'cantidad': 1},
        headers=_headers(),
    )
    assert respuesta.status_code == 404
    assert respuesta.get_json()['error']['code'] == 'ITEM_NO_ENCONTRADO'


def test_actualizar_item_stock_insuficiente(client, bd):
    bd.carrito = [{
        'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
        'cantidad': 2, 'created_at': None,
    }]
    bd.productos[1]['stock'] = 2
    respuesta = client.patch(
        '/api/v1/carrito/items/1',
        json={'cantidad': 3},
        headers=_headers(),
    )
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'STOCK_INSUFICIENTE'


def test_eliminar_item(client, bd):
    bd.carrito = [{
        'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
        'cantidad': 2, 'created_at': None,
    }]
    bd._next_item = 2
    respuesta = client.delete(
        '/api/v1/carrito/items/1', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['eliminado'] is True
    assert bd.carrito == []


def test_eliminar_item_idempotente(client, bd):
    respuesta = client.delete(
        '/api/v1/carrito/items/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['eliminado'] is False


def test_vaciar_carrito(client, bd):
    bd.carrito = [
        {'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
         'cantidad': 2, 'created_at': None},
        {'item_id': 2, 'usuario_id': '7', 'producto_id': 2,
         'cantidad': 1, 'created_at': None},
    ]
    bd._next_item = 3
    respuesta = client.delete('/api/v1/carrito', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['eliminados'] == 2
    assert data['carrito']['items'] == []
    assert bd.carrito == []


def test_resumen_carrito(client, bd):
    client.post('/api/v1/carrito/items',
                json={'producto_id': 1, 'cantidad': 2},
                headers=_headers())
    respuesta = client.get('/api/v1/carrito/resumen', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert 'items' not in data
    assert data['cantidad_items'] == 2
    assert data['total'] == '241.00'
    assert data['total_centimos'] == 24100
    assert data['envio']['costo'] == '15.00'
    assert data['total_con_envio'] == '256.00'


def test_carrito_agrupa_duplicados(client, bd):
    bd.carrito = [
        {'item_id': 1, 'usuario_id': '7', 'producto_id': 1,
         'cantidad': 2, 'created_at': None},
        {'item_id': 2, 'usuario_id': '7', 'producto_id': 1,
         'cantidad': 1, 'created_at': None},
    ]
    bd._next_item = 3
    respuesta = client.get('/api/v1/carrito', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['lineas'] == 1
    assert data['cantidad_items'] == 3
    assert data['total'] == '361.50'
    assert data['items'][0]['cantidad'] == 3
