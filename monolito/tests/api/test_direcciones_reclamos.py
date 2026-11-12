"""Pruebas de contrato de direcciones y reclamos sin MySQL real.

La BD de juguete interpreta las consultas que generan
`services.direcciones_service` y `services.reclamos_service`.
"""
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

import app as monolito
from api import security

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


class BDJuguete:
    def __init__(self):
        self.consultas = []
        self.direcciones = []
        self.reclamaciones = []
        self._next_direccion = 1
        self._next_reclamo = 1
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

    def execute(self, sql, params=None):
        self.db.consultas.append((sql, params))
        self._rows = []
        self._one = None
        self.rowcount = 0
        s = ' '.join(sql.split())
        p = tuple(params or ())

        # ── Direcciones ──
        if s.startswith('SELECT * FROM direcciones') and \
                'ORDER BY predeterminada DESC' in s:
            usuario_id = str(p[0])
            filas = [d for d in self.db.direcciones
                     if str(d['usuario_id']) == usuario_id]
            filas.sort(key=lambda d: (int(d.get('predeterminada') or 0),
                                      d.get('id') or 0), reverse=True)
            self._rows = [dict(d) for d in filas]
            return
        if s.startswith('SELECT * FROM direcciones WHERE id=%s AND usuario_id=%s'):
            direccion_id, usuario_id = int(p[0]), str(p[1])
            self._one = next(
                (dict(d) for d in self.db.direcciones
                 if d['id'] == direccion_id
                 and str(d['usuario_id']) == usuario_id), None)
            return
        if s.startswith('UPDATE direcciones SET predeterminada=0'):
            usuario_id = str(p[0])
            for d in self.db.direcciones:
                if str(d['usuario_id']) == usuario_id:
                    d['predeterminada'] = 0
            self.rowcount = 1
            return
        if s.startswith('UPDATE direcciones SET predeterminada=1'):
            direccion_id, usuario_id = int(p[0]), str(p[1])
            for d in self.db.direcciones:
                if d['id'] == direccion_id and str(d['usuario_id']) == usuario_id:
                    d['predeterminada'] = 1
                    self.rowcount = 1
            return
        if s.startswith('UPDATE direcciones SET direccion=%s'):
            (direccion, distrito, referencia, predeterminada,
             direccion_id, usuario_id) = (p[0], p[1], p[2], p[3],
                                          int(p[4]), str(p[5]))
            for d in self.db.direcciones:
                if d['id'] == direccion_id and str(d['usuario_id']) == usuario_id:
                    d.update({'direccion': direccion, 'distrito': distrito,
                              'referencia': referencia,
                              'predeterminada': predeterminada})
                    self.rowcount = 1
            return
        if s.startswith('INSERT INTO direcciones'):
            usuario_id, direccion, distrito, referencia, predeterminada = p
            self.db.direcciones.append({
                'id': self.db._next_direccion,
                'usuario_id': str(usuario_id),
                'direccion': direccion,
                'distrito': distrito,
                'referencia': referencia,
                'predeterminada': int(predeterminada),
                'created_at': datetime(2026, 1, 1, 12, 0, 0),
            })
            self.lastrowid = self.db._next_direccion
            self.db._next_direccion += 1
            self.rowcount = 1
            return
        if s.startswith('DELETE FROM direcciones WHERE id=%s AND usuario_id=%s'):
            direccion_id, usuario_id = int(p[0]), str(p[1])
            antes = len(self.db.direcciones)
            self.db.direcciones = [
                d for d in self.db.direcciones
                if not (d['id'] == direccion_id
                        and str(d['usuario_id']) == usuario_id)
            ]
            self.rowcount = antes - len(self.db.direcciones)
            return

        # ── Reclamos ──
        if s.startswith('INSERT INTO reclamaciones'):
            (token, fecha, tipo, nombre, doc_tipo, doc, domicilio, telefono,
             correo, bien_tipo, bien_desc, monto, detalle, pedido,
             fecha_limite) = p
            self.db.reclamaciones.append({
                'id': self.db._next_reclamo,
                'codigo': None,
                'token': token,
                'fecha': fecha,
                'tipo': tipo,
                'consumidor_nombre': nombre,
                'consumidor_documento_tipo': doc_tipo,
                'consumidor_documento': doc,
                'consumidor_domicilio': domicilio,
                'consumidor_telefono': telefono,
                'consumidor_correo': correo,
                'bien_tipo': bien_tipo,
                'bien_descripcion': bien_desc,
                'bien_monto': monto,
                'detalle': detalle,
                'pedido': pedido,
                'estado': 'pendiente',
                'respuesta': None,
                'fecha_respuesta': None,
                'respondido_por': None,
                'fecha_limite': fecha_limite,
                'created_at': fecha,
            })
            self.lastrowid = self.db._next_reclamo
            self.db._next_reclamo += 1
            self.rowcount = 1
            return
        if s.startswith('UPDATE reclamaciones SET codigo=%s WHERE id=%s'):
            codigo, reclamo_id = p[0], int(p[1])
            for r in self.db.reclamaciones:
                if r['id'] == reclamo_id:
                    r['codigo'] = codigo
                    self.rowcount = 1
            return
        if s.startswith('SELECT COUNT(*) AS total FROM reclamaciones'):
            correo = str(p[0]).lower()
            self._one = {'total': len(
                [r for r in self.db.reclamaciones
                 if (r.get('consumidor_correo') or '').lower() == correo])}
            return
        if s.startswith('SELECT * FROM reclamaciones') and \
                'LOWER(consumidor_correo)' in s:
            correo = str(p[0]).lower()
            filas = [dict(r) for r in self.db.reclamaciones
                     if (r.get('consumidor_correo') or '').lower() == correo]
            filas.sort(key=lambda r: r['id'], reverse=True)
            self._rows = filas
            return
        if s.startswith('SELECT * FROM reclamaciones WHERE id=%s'):
            reclamo_id = int(p[0])
            self._one = next(
                (dict(r) for r in self.db.reclamaciones if r['id'] == reclamo_id),
                None)
            return
        if s.startswith('SELECT * FROM reclamaciones WHERE token=%s'):
            token = p[0]
            self._one = next(
                (dict(r) for r in self.db.reclamaciones if r['token'] == token),
                None)
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
    return db


def _headers():
    with monolito.app.app_context():
        token = create_access_token(
            identity='7', additional_claims={'ver': 0, 'rol': 'cliente'})
    return {'Authorization': 'Bearer %s' % token}


def _direccion(bd, direccion='Av. Los Andes 456, Chilca', distrito='Chilca',
               predeterminada=0, usuario_id='7'):
    bd.direcciones.append({
        'id': bd._next_direccion, 'usuario_id': str(usuario_id),
        'direccion': direccion, 'distrito': distrito,
        'referencia': 'Porton azul', 'predeterminada': predeterminada,
        'created_at': datetime(2026, 1, 1, 12, 0, 0),
    })
    bd._next_direccion += 1


def _reclamo_valido(**cambios):
    datos = {
        'tipo': 'reclamo',
        'consumidor_nombre': 'Ana Perez Gomez',
        'consumidor_documento_tipo': 'DNI',
        'consumidor_documento': '12345678',
        'consumidor_domicilio': 'Av. Los Andes 456, Chilca',
        'consumidor_telefono': '999999999',
        'consumidor_correo': 'ana@example.com',
        'bien_descripcion': 'Taladro con falla',
        'bien_monto': '120.50',
        'detalle': 'El taladro dejo de funcionar al segundo uso.',
        'pedido': 'Solicito el cambio del producto.',
    }
    datos.update(cambios)
    return datos


# ─────────────────────────────────────────────
# DIRECCIONES
# ─────────────────────────────────────────────
def test_direcciones_sin_token(client, bd):
    respuesta = client.get('/api/v1/direcciones')
    assert respuesta.status_code == 401


def test_listar_direcciones_vacio(client, bd):
    respuesta = client.get('/api/v1/direcciones', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data'] == []


def test_listar_direcciones_predeterminada_primero(client, bd):
    _direccion(bd, direccion='Calle A 111', predeterminada=0)
    _direccion(bd, direccion='Calle B 222', predeterminada=1)
    respuesta = client.get('/api/v1/direcciones', headers=_headers())
    data = respuesta.get_json()['data']
    assert data[0]['direccion'] == 'Calle B 222'
    assert data[0]['predeterminada'] is True
    assert data[1]['created_at'] is not None


def test_crear_direccion(client, bd):
    respuesta = client.post('/api/v1/direcciones',
                            json={'direccion': 'Av. Peru 1234, El Tambo',
                                  'distrito': 'El Tambo',
                                  'referencia': 'Frente al parque',
                                  'predeterminada': True},
                            headers=_headers())
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['id'] == 1
    assert data['distrito'] == 'El Tambo'
    assert data['predeterminada'] is True


def test_crear_direccion_invalida(client, bd):
    respuesta = client.post('/api/v1/direcciones',
                            json={'direccion': 'ab'}, headers=_headers())
    assert respuesta.status_code == 422
    assert respuesta.get_json()['error']['code'] == 'DIRECCION_INVALIDA'


def test_crear_predeterminada_desmarca_la_anterior(client, bd):
    _direccion(bd, direccion='Calle A 111', predeterminada=1)
    client.post('/api/v1/direcciones',
                json={'direccion': 'Calle B 222', 'predeterminada': True},
                headers=_headers())
    predeterminadas = [d for d in bd.direcciones if d['predeterminada']]
    assert len(predeterminadas) == 1
    assert predeterminadas[0]['direccion'] == 'Calle B 222'


def test_ver_direccion_ajena(client, bd):
    _direccion(bd, usuario_id='999')
    respuesta = client.get('/api/v1/direcciones/1', headers=_headers())
    assert respuesta.status_code == 404


def test_actualizar_direccion_parcial(client, bd):
    _direccion(bd, direccion='Calle A 111', distrito='Chilca')
    respuesta = client.patch('/api/v1/direcciones/1',
                             json={'distrito': 'Huancayo'}, headers=_headers())
    assert respuesta.status_code == 200
    data = respuesta.get_json()['data']
    assert data['distrito'] == 'Huancayo'
    assert data['direccion'] == 'Calle A 111'


def test_eliminar_direccion(client, bd):
    _direccion(bd)
    respuesta = client.delete('/api/v1/direcciones/1', headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['eliminado'] is True
    assert bd.direcciones == []


def test_predeterminar_direccion(client, bd):
    _direccion(bd, direccion='Calle A 111', predeterminada=1)
    _direccion(bd, direccion='Calle B 222', predeterminada=0)
    respuesta = client.post('/api/v1/direcciones/2/predeterminada',
                            headers=_headers())
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['predeterminada'] is True
    assert bd.direcciones[0]['predeterminada'] == 0


# ─────────────────────────────────────────────
# RECLAMOS
# ─────────────────────────────────────────────
def test_crear_reclamo_publico(client, bd):
    respuesta = client.post('/api/v1/reclamos', json=_reclamo_valido())
    assert respuesta.status_code == 201
    data = respuesta.get_json()['data']
    assert data['codigo'].startswith('LR-')
    assert data['token']
    assert data['estado'] == 'pendiente'
    assert data['vencido'] is False
    assert data['bien']['monto'] == '120.50'
    assert len(bd.reclamaciones) == 1


def test_crear_reclamo_invalido(client, bd):
    respuesta = client.post('/api/v1/reclamos',
                            json=_reclamo_valido(consumidor_documento='123'))
    assert respuesta.status_code == 422
    error = respuesta.get_json()['error']
    assert error['code'] == 'VALIDACION'
    campos = {detalle['campo'] for detalle in error['details']}
    assert 'consumidor_documento' in campos


def test_listar_reclamos_propios(client, bd):
    client.post('/api/v1/reclamos', json=_reclamo_valido())
    respuesta = client.get('/api/v1/reclamos', headers=_headers())
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo['meta']['total'] == 1
    assert cuerpo['data'][0]['consumidor']['correo'] == 'ana@example.com'


def test_listar_reclamos_otro_correo(client, bd):
    client.post('/api/v1/reclamos',
                json=_reclamo_valido(consumidor_correo='otro@example.com'))
    respuesta = client.get('/api/v1/reclamos', headers=_headers())
    assert respuesta.get_json()['meta']['total'] == 0


def test_ver_reclamo_ajeno(client, bd):
    client.post('/api/v1/reclamos',
                json=_reclamo_valido(consumidor_correo='otro@example.com'))
    respuesta = client.get('/api/v1/reclamos/1', headers=_headers())
    assert respuesta.status_code == 404


def test_constancia_con_token(client, bd):
    creado = client.post('/api/v1/reclamos', json=_reclamo_valido())
    token = creado.get_json()['data']['token']
    respuesta = client.get('/api/v1/reclamos/1/constancia?token=%s' % token)
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['codigo'] == creado.get_json()['data']['codigo']


def test_constancia_token_incorrecto(client, bd):
    client.post('/api/v1/reclamos', json=_reclamo_valido())
    respuesta = client.get('/api/v1/reclamos/1/constancia?token=malo')
    assert respuesta.status_code == 403
    assert respuesta.get_json()['error']['code'] == 'CONSTANCIA_NO_AUTORIZADA'


def test_constancia_sin_token(client, bd):
    client.post('/api/v1/reclamos', json=_reclamo_valido())
    respuesta = client.get('/api/v1/reclamos/1/constancia')
    assert respuesta.status_code == 403


def test_constancia_con_sesion_del_dueno(client, bd):
    client.post('/api/v1/reclamos', json=_reclamo_valido())
    respuesta = client.get('/api/v1/reclamos/1/constancia', headers=_headers())
    assert respuesta.status_code == 200


def test_reclamo_vencido(client, bd):
    client.post('/api/v1/reclamos', json=_reclamo_valido())
    bd.reclamaciones[0]['fecha_limite'] = datetime.now() - timedelta(days=1)
    respuesta = client.get('/api/v1/reclamos', headers=_headers())
    fila = respuesta.get_json()['data'][0]
    assert fila['vencido'] is True
    assert fila['dias_restantes'] < 0
