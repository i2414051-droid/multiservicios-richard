"""Pruebas de contrato de checkout y pedidos sin MySQL real.

La BD de juguete interpreta las consultas que generan
`services.pedidos_service` y `services.tienda_service`. Se prueba el cableado
HTTP, la idempotencia, el descuento/reposicion de stock, la cancelacion y la
forma del JSON.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

import app as monolito
from api import security
from services import pedidos_service, tienda_service

USUARIO = {
    'id': 7,
    'correo': 'ana@example.com',
    'rol': 'cliente',
    'nombre': 'Ana',
    'apellido': 'Perez',
    'telefono': '999999999',
    'documento_tipo': 'dni',
    'documento_numero': '12345678',
    'estado': 'activo',
    'created_at': None,
    'token_version': 0,
}

CABECERA_IDEM = {'Idempotency-Key': 'clave-12345678'}


def _producto(id, nombre, precio, stock, categoria='Herramientas',
              imagen='imagenes/p.jpg', estado='activo'):
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
            1: _producto(1, 'Taladro', '120.50', 5, imagen='imagenes/taladro.jpg'),
            2: _producto(2, 'Cable', '10.00', 0, categoria='Electricos',
                         imagen='imagenes/cable.jpg'),
        }
        self.ajustes = [
            {'clave': 'envio_delivery', 'valor': '15'},
            {'clave': 'envio_gratis_desde', 'valor': '300'},
            {'clave': 'delivery_minimo', 'valor': '50'},
            {'clave': 'empresa_direccion', 'valor': 'Av. Tienda 123'},
            {'clave': 'empresa_lat', 'valor': '-12.07'},
            {'clave': 'empresa_lon', 'valor': '-75.19'},
        ]
        self.carrito = []
        self.ventas = []
        self.detalle = []
        self.seguimiento = []
        self.cancelaciones = []
        self.idempotencia = []
        self.direcciones = [
            {'id': 3, 'usuario_id': '7', 'direccion': 'Av. Los Andes 456',
             'distrito': 'Chilca', 'referencia': 'Porton azul',
             'predeterminada': 1},
        ]
        self._next_item = 1
        self._next_venta = 1
        self._next_detalle = 1
        self._next_seguimiento = 1
        self.connection = self

    def cursor(self):
        return CursorFalso(self)

    def commit(self):
        return None

    def rollback(self):
        return None

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

    def fila_item_pedido(self, linea):
        producto = self.productos.get(linea['producto_id'])
        if not producto:
            return None
        return {
            'producto_id': linea['producto_id'],
            'nombre': producto['nombre'],
            'descripcion': producto.get('descripcion'),
            'imagen': producto.get('imagen'),
            'cantidad': linea['cantidad'],
            'precio': linea['precio'],
        }

    def venta_json(self, venta, con_motivo=False):
        fila = dict(venta)
        if con_motivo:
            motivo = next((c for c in self.cancelaciones
                           if c['venta_id'] == venta['id']), None)
            fila['motivo_codigo'] = motivo['motivo_codigo'] if motivo else None
            fila['motivo_detalle'] = motivo['motivo_detalle'] if motivo else None
        return fila

    def ultimo_seguimiento(self, venta_id):
        filas = [s for s in self.seguimiento if s['venta_id'] == venta_id]
        return filas[-1] if filas else None


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

        if s.startswith('SELECT id, request_hash, respuesta'):
            usuario_id, clave = int(p[0]), str(p[1])
            fila = next((i for i in self.db.idempotencia
                         if i['usuario_id'] == usuario_id and i['clave'] == clave),
                        None)
            if fila:
                self._one = {'id': fila['id'],
                             'request_hash': fila['request_hash'],
                             'respuesta': fila['respuesta'],
                             'edad': 0}
            return
        if s.startswith('DELETE FROM api_idempotencia'):
            self.db.idempotencia = [
                i for i in self.db.idempotencia if i['id'] != int(p[0])
            ]
            self.rowcount = 1
            return
        if s.startswith('INSERT INTO api_idempotencia'):
            self.db.idempotencia.append({
                'id': len(self.db.idempotencia) + 1,
                'usuario_id': int(p[0]), 'clave': str(p[1]),
                'endpoint': str(p[2]), 'request_hash': str(p[3]),
                'respuesta': None,
            })
            self.lastrowid = len(self.db.idempotencia)
            self.rowcount = 1
            return
        if s.startswith('UPDATE api_idempotencia'):
            for i in self.db.idempotencia:
                if (i['usuario_id'] == int(p[1]) and i['clave'] == str(p[2])
                        and i['respuesta'] is None):
                    i['respuesta'] = p[0]
                    self.rowcount = 1
            return

        if 'FROM usuarios WHERE id=%s' in s and s.startswith('SELECT correo'):
            self._one = {
                'correo': USUARIO['correo'], 'nombre': USUARIO['nombre'],
                'apellido': USUARIO['apellido'], 'telefono': USUARIO['telefono'],
                'documento_tipo': USUARIO['documento_tipo'],
                'documento_numero': USUARIO['documento_numero'],
            }
            return
        if s.startswith('UPDATE usuarios SET'):
            self.rowcount = 1
            return

        if 'FROM ajustes' in s:
            claves = {str(x) for x in p}
            self._rows = [a for a in self.db.ajustes if a['clave'] in claves]
            return

        if 'FROM carrito c JOIN' in s:
            usuario_id = str(p[0])
            self._rows = [
                self.db.fila_carrito(c) for c in self.db.carrito
                if str(c['usuario_id']) == usuario_id
            ]
            self._rows = [r for r in self._rows if r]
            return

        if 'FROM direcciones WHERE usuario_id=%s' in s:
            usuario_id = str(p[0])
            self._rows = [d for d in self.db.direcciones
                          if str(d['usuario_id']) == usuario_id]
            return

        if s.startswith('INSERT INTO ventas'):
            (fecha, cliente_id, total, metodo_pago, direccion_envio, documento,
             doc_tipo, nombre, apellido, telefono, correo, notas, tipo_entrega,
             distrito, latitud, longitud, costo_envio) = p
            self.db.ventas.append({
                'id': self.db._next_venta,
                'cliente_id': int(cliente_id),
                'total': Decimal(str(total)),
                'documento': documento, 'doc_tipo': doc_tipo,
                'nombre': nombre, 'apellido': apellido,
                'telefono': telefono, 'correo': correo,
                'estado': 'en espera', 'fecha': fecha,
                'metodo_pago': metodo_pago,
                'direccion_envio': direccion_envio,
                'notas_entrega': notas, 'estado_pago': 'pendiente',
                'tipo_entrega': tipo_entrega, 'distrito': distrito,
                'latitud': latitud, 'longitud': longitud,
                'costo_envio': Decimal(str(costo_envio)),
                'oculta': 0,
            })
            self.lastrowid = self.db._next_venta
            self.db._next_venta += 1
            self.rowcount = 1
            return

        if s.startswith('INSERT INTO seguimiento_entregas'):
            (creado, venta_id, direccion, notas, tipo, distrito,
             lat, lon, costo) = p
            self.db.seguimiento.append({
                'id': self.db._next_seguimiento, 'venta_id': int(venta_id),
                'estado': 'pendiente', 'direccion_envio': direccion,
                'notas': notas, 'tipo_entrega': tipo, 'distrito': distrito,
                'latitud': lat, 'longitud': lon,
                'costo_envio': Decimal(str(costo)), 'created_at': creado,
                'fecha_estimada': None, 'fecha_entrega': None,
            })
            self.db._next_seguimiento += 1
            self.lastrowid = self.db._next_seguimiento - 1
            self.rowcount = 1
            return

        if s.startswith('INSERT INTO detalle_venta'):
            venta_id, producto_id, cantidad, precio = p
            self.db.detalle.append({
                'id': self.db._next_detalle, 'venta_id': int(venta_id),
                'producto_id': int(producto_id), 'cantidad': int(cantidad),
                'precio': Decimal(str(precio)),
            })
            self.db._next_detalle += 1
            self.rowcount = 1
            return

        if 'SET stock=stock-%s' in s:
            cantidad, producto_id, minimo = int(p[0]), int(p[1]), int(p[2])
            producto = self.db.productos.get(producto_id)
            if producto and int(producto['stock']) >= minimo:
                producto['stock'] = int(producto['stock']) - cantidad
                self.rowcount = 1
            return

        if 'SET stock=stock+%s' in s:
            cantidad, producto_id = int(p[0]), int(p[1])
            producto = self.db.productos.get(producto_id)
            if producto:
                producto['stock'] = int(producto['stock']) + cantidad
                self.rowcount = 1
            return

        if s.startswith('DELETE FROM carrito WHERE usuario_id=%s'):
            usuario_id = str(p[0])
            antes = len(self.db.carrito)
            self.db.carrito = [c for c in self.db.carrito
                               if str(c['usuario_id']) != usuario_id]
            self.rowcount = antes - len(self.db.carrito)
            return

        if s.startswith('INSERT IGNORE INTO cancelaciones'):
            venta_id, motivo, detalle, cancelado_por = p
            self.db.cancelaciones.append({
                'venta_id': int(venta_id), 'motivo_codigo': motivo,
                'motivo_detalle': detalle, 'cancelado_por': cancelado_por,
                'created_at': datetime(2026, 1, 1, 12, 0, 0),
            })
            self.rowcount = 1
            return

        if s.startswith('SELECT producto_id, cantidad FROM detalle_venta'):
            venta_id = int(p[0])
            self._rows = [{'producto_id': d['producto_id'],
                           'cantidad': d['cantidad']}
                          for d in self.db.detalle if d['venta_id'] == venta_id]
            return

        if s.startswith('SELECT * FROM ventas WHERE id=%s'):
            self._one = next((dict(v) for v in self.db.ventas
                              if v['id'] == int(p[0])), None)
            return

        if 'FROM cancelaciones WHERE venta_id=%s' in s:
            self._one = next((dict(c) for c in self.db.cancelaciones
                              if c['venta_id'] == int(p[0])), None)
            return

        if 'FROM detalle_venta d JOIN' in s:
            venta_id = int(p[0])
            self._rows = [
                self.db.fila_item_pedido(d) for d in self.db.detalle
                if d['venta_id'] == venta_id
            ]
            self._rows = [r for r in self._rows if r]
            return

        if 'FROM seguimiento_entregas WHERE venta_id=%s' in s:
            self._one = self.db.ultimo_seguimiento(int(p[0]))
            return

        if s.startswith('SELECT COUNT(*) AS total FROM ventas'):
            user_id = int(p[0])
            self._one = {'total': len([v for v in self.db.ventas
                                       if v['cliente_id'] == user_id
                                       and not v.get('oculta')])}
            return

        if 'FROM ventas v LEFT JOIN cancelaciones' in s:
            user_id = int(p[0])
            self._rows = [self.db.venta_json(v, con_motivo=True)
                          for v in self.db.ventas
                          if v['cliente_id'] == user_id and not v.get('oculta')]
            return

        if s.startswith("UPDATE ventas SET estado='cancelado'"):
            venta = next((v for v in self.db.ventas if v['id'] == int(p[0])), None)
            if venta:
                venta['estado'] = 'cancelado'
                self.rowcount = 1
            return
        if s.startswith("UPDATE seguimiento_entregas SET estado='cancelado'"):
            for sgs in self.db.seguimiento:
                if sgs['venta_id'] == int(p[0]):
                    sgs['estado'] = 'cancelado'
            self.rowcount = 1
            return
        if s.startswith('UPDATE ventas SET documento=%s, nombre=%s'):
            documento, nombre, venta_id = p
            venta = next((v for v in self.db.ventas if v['id'] == int(venta_id)), None)
            if venta:
                venta['documento'] = documento
                venta['nombre'] = nombre
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
    monkeypatch.setattr('services.auth_service.buscar_usuario_por_id',
                        lambda _cur, _id: USUARIO)
    monkeypatch.setattr(monolito, 'sincronizar_pedido_por_stock',
                        lambda _cur, _pid: None)
    monkeypatch.setattr(monolito, 'enviar_boleta_cliente',
                        lambda _correo, _vid: None)
    monkeypatch.setattr(monolito, 'generar_boleta_pdf',
                        lambda _vid: (b'%PDF-1.4 boleta', None))
    return db


def _headers(extra=None):
    with monolito.app.app_context():
        token = create_access_token(
            identity='7', additional_claims={'ver': 0, 'rol': 'cliente'})
    cabeceras = {'Authorization': 'Bearer %s' % token}
    if extra:
        cabeceras.update(extra)
    return cabeceras


def _agregar_carrito(bd, producto_id=1, cantidad=2, item_id=1):
    bd.carrito.append({
        'item_id': item_id, 'usuario_id': '7', 'producto_id': producto_id,
        'cantidad': cantidad, 'created_at': datetime(2026, 1, 1, 12, 0, 0),
    })
    bd._next_item = max(bd._next_item, item_id + 1)


def _pedido_valido(**cambios):
    datos = {
        'doc_tipo': 'dni',
        'doc_numero': '12345678',
        'nombres': 'Ana',
        'apellido_paterno': 'Perez',
        'apellido_materno': 'Gomez',
        'telefono': '999999999',
        'correo': 'ana@example.com',
        'metodo_pago': 'transferencia',
        'tipo_entrega': 'recojo',
    }
    datos.update(cambios)
    return datos


# ─────────────────────────────────────────────
# CHECKOUT
# ─────────────────────────────────────────────
def test_checkout_opciones(client, bd):
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.get('/api/v1/checkout/opciones', headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['carrito']['total'] == '241.00'
    assert data['usuario']['documento_numero'] == '12345678'
    assert data['direcciones'][0]['distrito'] == 'Chilca'
    metodos = {m['codigo']: m['disponible'] for m in data['metodos_pago']}
    assert metodos['transferencia'] is True
    assert metodos['contra_entrega'] is True
    assert metodos['tarjeta'] is False
    assert data['entrega']['distritos'] == list(tienda_service.DISTRITOS_DELIVERY)
    assert data['entrega']['elegible_delivery'] is True


def test_checkout_opciones_sin_token(client, bd):
    respuesta = client.get('/api/v1/checkout/opciones')
    assert respuesta.status_code == 401


def test_checkout_validar_recojo(client, bd):
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post('/api/v1/checkout/validar',
                            json=_pedido_valido(), headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['valido'] is True
    assert data['subtotal'] == '241.00'
    assert data['costo_envio'] == '0.00'
    assert data['total'] == '241.00'
    assert data['tipo_entrega'] == 'recojo'


def test_checkout_validar_delivery_minimo(client, bd):
    bd.productos[1]['precio'] = Decimal('10.00')
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post(
        '/api/v1/checkout/validar',
        json=_pedido_valido(tipo_entrega='delivery', direccion='Av. Los Andes 456'),
        headers=_headers(),
    )
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'DELIVERY_MINIMO'


def test_checkout_validar_pago_tarjeta(client, bd):
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post('/api/v1/checkout/validar',
                            json=_pedido_valido(metodo_pago='tarjeta'),
                            headers=_headers())
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'PAGO_NO_DISPONIBLE'


# ─────────────────────────────────────────────
# ALTA DE PEDIDO
# ─────────────────────────────────────────────
def test_crear_pedido_sin_idempotencia(client, bd):
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post('/api/v1/pedidos', json=_pedido_valido(),
                            headers=_headers())
    assert respuesta.status_code == 400
    assert respuesta.get_json()['error']['code'] == 'IDEMPOTENCIA_REQUERIDA'


def test_crear_pedido_ok(client, bd):
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post('/api/v1/pedidos', json=_pedido_valido(),
                            headers=_headers(CABECERA_IDEM))
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['estado'] == 'en espera'
    assert data['estado_pago'] == 'pendiente'
    assert data['metodo_pago'] == 'transferencia'
    assert data['total'] == '241.00'
    assert data['items'][0]['cantidad'] == 2
    assert data['seguimiento']['estado'] == 'pendiente'
    # Stock descontado y carrito vaciado.
    assert bd.productos[1]['stock'] == 3
    assert bd.carrito == []
    assert len(bd.ventas) == 1
    assert len(bd.detalle) == 1


def test_crear_pedido_replay(client, bd):
    _agregar_carrito(bd, cantidad=2)
    primera = client.post('/api/v1/pedidos', json=_pedido_valido(),
                          headers=_headers(CABECERA_IDEM))
    segunda = client.post('/api/v1/pedidos', json=_pedido_valido(),
                          headers=_headers(CABECERA_IDEM))
    assert primera.status_code == 201
    assert segunda.status_code == 201
    assert primera.get_json()['data']['id'] == segunda.get_json()['data']['id']
    assert len(bd.ventas) == 1


def test_crear_pedido_conflicto(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.post('/api/v1/pedidos',
                            json=_pedido_valido(telefono='988888888'),
                            headers=_headers(CABECERA_IDEM))
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'IDEMPOTENCIA_CONFLICTO'


def test_crear_pedido_sin_stock(client, bd):
    bd.productos[1]['stock'] = 1
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post('/api/v1/pedidos', json=_pedido_valido(),
                            headers=_headers(CABECERA_IDEM))
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'STOCK_INSUFICIENTE'
    assert bd.ventas == []
    assert len(bd.carrito) == 1


def test_crear_pedido_carrito_vacio(client, bd):
    respuesta = client.post('/api/v1/pedidos', json=_pedido_valido(),
                            headers=_headers(CABECERA_IDEM))
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'CARRITO_VACIO'


def test_crear_pedido_delivery(client, bd):
    _agregar_carrito(bd, cantidad=2)
    respuesta = client.post(
        '/api/v1/pedidos',
        json=_pedido_valido(tipo_entrega='delivery', direccion='Av. Los Andes 456',
                            lat=-12.10, lon=-75.20),
        headers=_headers(CABECERA_IDEM),
    )
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['entrega']['tipo'] == 'delivery'
    assert data['entrega']['distrito'] == 'Chilca'
    assert data['costo_envio'] == '15.00'
    assert data['total'] == '256.00'
    assert data['subtotal'] == '241.00'


# ─────────────────────────────────────────────
# HISTORIAL, DETALLE, CANCELACION
# ─────────────────────────────────────────────
def test_listar_pedidos(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.get('/api/v1/pedidos', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 1
    assert cuerpo['data'][0]['id'] == 1
    assert cuerpo['data'][0]['items'][0]['nombre'] == 'Taladro'


def test_detalle_pedido(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.get('/api/v1/pedidos/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['total'] == '241.00'


def test_detalle_pedido_ajeno(client, bd):
    bd.ventas.append({
        'id': 99, 'cliente_id': 999, 'total': Decimal('10.00'),
        'documento': '1', 'doc_tipo': 'dni', 'nombre': 'X', 'apellido': 'Y',
        'telefono': '999999999', 'correo': 'x@y.com', 'estado': 'en espera',
        'fecha': datetime(2026, 1, 1), 'metodo_pago': 'transferencia',
        'direccion_envio': '', 'notas_entrega': '', 'estado_pago': 'pendiente',
        'tipo_entrega': 'recojo', 'distrito': 'Chilca', 'latitud': None,
        'longitud': None, 'costo_envio': Decimal('0.00'), 'oculta': 0,
    })
    respuesta = client.get('/api/v1/pedidos/99', headers=_headers())
    assert respuesta.status_code == 404


def test_cancelar_pedido(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    assert bd.productos[1]['stock'] == 3
    respuesta = client.post('/api/v1/pedidos/1/cancelar',
                            json={'motivo': 'cambio_opinion'},
                            headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['estado'] == 'cancelado'
    assert data['cancelacion']['motivo'] == 'cambio_opinion'
    assert bd.productos[1]['stock'] == 5
    assert bd.ventas[0]['estado'] == 'cancelado'


def test_cancelar_pedido_motivo_invalido(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.post('/api/v1/pedidos/1/cancelar',
                            json={'motivo': 'no_existe'}, headers=_headers())
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'MOTIVO_INVALIDO'


def test_cancelar_pedido_entregado(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    bd.ventas[0]['estado'] = 'entregado'
    respuesta = client.post('/api/v1/pedidos/1/cancelar',
                            json={'motivo': 'cambio_opinion'},
                            headers=_headers())
    assert respuesta.status_code == 409
    assert respuesta.get_json()['error']['code'] == 'PEDIDO_ENTREGADO'


def test_seguimiento_pedido(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.get('/api/v1/pedidos/1/seguimiento', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['estado'] == 'pendiente'


# ─────────────────────────────────────────────
# BOLETA
# ─────────────────────────────────────────────
def test_boleta_json(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.get('/api/v1/pedidos/1/boleta', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['documento']['numero'] == '12345678'


def test_boleta_pdf(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.get('/api/v1/pedidos/1/boleta.pdf', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.mimetype == 'application/pdf'
    assert respuesta.data.startswith(b'%PDF')
    assert 'attachment' in respuesta.headers['Content-Disposition']


def test_guardar_boleta(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.post('/api/v1/pedidos/1/boleta',
                            json={'documento': '20123456789',
                                  'nombre': 'Multiservicios SAC'},
                            headers=_headers())
    assert respuesta.status_code == 200
    assert bd.ventas[0]['documento'] == '20123456789'


def test_guardar_boleta_documento_invalido(client, bd):
    _agregar_carrito(bd, cantidad=2)
    client.post('/api/v1/pedidos', json=_pedido_valido(),
                headers=_headers(CABECERA_IDEM))
    respuesta = client.post('/api/v1/pedidos/1/boleta',
                            json={'documento': 'abc', 'nombre': 'X SA'},
                            headers=_headers())
    assert respuesta.status_code == 422
