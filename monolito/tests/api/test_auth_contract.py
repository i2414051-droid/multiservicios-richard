"""Pruebas de contrato de la API v1 que no requieren MySQL.

Las pruebas de integracion reales (login, carrito, pedidos) se agregan cuando
el .env tenga credenciales validas. Estas cubren:
  * envelope de exito/error,
  * OpenAPI y Swagger,
  * guards JWT (sin token, token invalido, expirado, revocado),
  * validacion de entrada.
"""
import hashlib
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    decode_token,
)

import app as monolito
from api import security


@pytest.fixture()
def client():
    monolito.app.config.update(TESTING=True)
    with monolito.app.test_client() as cliente:
        yield cliente


def _token(claims=None, expires_delta=None):
    with monolito.app.app_context():
        return create_access_token(
            identity='7',
            additional_claims=claims or {'ver': 0, 'rol': 'cliente'},
            expires_delta=expires_delta,
        )


class _CursorFalso:
    def close(self):
        return None


class _ConexionFalsa:
    def cursor(self):
        return _CursorFalso()


class _MySQLFalso:
    connection = _ConexionFalsa()


def _sin_bd(monkeypatch):
    """Evita tocar MySQL en los guards."""
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, 'get_mysql', lambda: _MySQLFalso())


def test_openapi(client):
    respuesta = client.get('/api/v1/openapi.json')
    assert respuesta.status_code == 200
    datos = respuesta.get_json()
    assert datos['openapi'].startswith('3.')
    assert '/api/v1/auth/login' in datos['paths']
    assert '/api/v1/auth/refresh' in datos['paths']


def test_docs(client):
    respuesta = client.get('/api/v1/docs')
    assert respuesta.status_code == 200
    assert b'swagger' in respuesta.data.lower()


def test_health(client):
    respuesta = client.get('/api/v1/health')
    assert respuesta.status_code in (200, 503)
    datos = respuesta.get_json()
    assert datos['ok'] is True
    assert datos['data']['base_datos'] in ('ok', 'error')


def test_me_sin_token(client):
    respuesta = client.get('/api/v1/auth/me')
    assert respuesta.status_code == 401
    error = respuesta.get_json()['error']
    assert error['code'] == 'NO_AUTENTICADO'


def test_token_invalido(client):
    respuesta = client.get(
        '/api/v1/auth/me',
        headers={'Authorization': 'Bearer esto-no-es-un-jwt'},
    )
    assert respuesta.status_code == 401
    assert respuesta.get_json()['error']['code'] == 'TOKEN_INVALIDO'


def test_token_expirado(client):
    token = _token(expires_delta=timedelta(seconds=-5))
    respuesta = client.get(
        '/api/v1/auth/me',
        headers={'Authorization': 'Bearer %s' % token},
    )
    assert respuesta.status_code == 401
    assert respuesta.get_json()['error']['code'] == 'TOKEN_EXPIRADO'


def test_login_sin_json(client):
    respuesta = client.post(
        '/api/v1/auth/login',
        data='no-es-json',
        content_type='text/plain',
    )
    assert respuesta.status_code == 400
    assert respuesta.get_json()['error']['code'] == 'JSON_INVALIDO'


def test_login_validacion(client):
    respuesta = client.post('/api/v1/auth/login', json={})
    assert respuesta.status_code == 422
    error = respuesta.get_json()['error']
    assert error['code'] == 'VALIDACION'
    assert len(error['details']) == 2


def test_refresh_sin_token(client):
    respuesta = client.post('/api/v1/auth/refresh')
    assert respuesta.status_code == 401
    assert respuesta.get_json()['error']['code'] == 'NO_AUTENTICADO'


def test_ruta_inexistente(client):
    respuesta = client.get('/api/v1/no-existe')
    assert respuesta.status_code == 404
    assert respuesta.get_json()['error']['code'] == 'NO_ENCONTRADO'


def test_metodo_no_permitido(client):
    respuesta = client.delete('/api/v1/auth/login')
    assert respuesta.status_code == 405
    assert respuesta.get_json()['error']['code'] == 'METODO_NO_PERMITIDO'


def test_emitir_tokens_guarda_refresh_hasheado(monkeypatch):
    ejecutados = []

    class Cursor:
        def execute(self, sql, params=None):
            ejecutados.append((sql, params))

        def close(self):
            return None

    class Conexion:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

    class MySQL:
        connection = Conexion()

    monkeypatch.setattr(security, 'get_mysql', lambda: MySQL())
    monkeypatch.setattr(security, '_schema_listo', True)

    with monolito.app.app_context():
        access, refresh, datos = security.emitir_tokens(
            {'id': 7, 'rol': 'cliente', 'nombre': 'Ana', 'token_version': 0},
            'dispositivo-1',
        )
        payload = decode_token(access)

    assert payload['sub'] == '7'
    assert payload['rol'] == 'cliente'
    assert payload['ver'] == 0
    assert datos['jti']

    inserts = [(sql, params) for sql, params in ejecutados
               if 'INSERT INTO auth_refresh_tokens' in sql]
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == 7
    assert params[1] == datos['jti']
    assert params[2] == hashlib.sha256(refresh.encode('utf-8')).hexdigest()
    assert params[3] == 'dispositivo-1'


def _mysql_simulado(usuario, ejecutados):
    class Cursor:
        def __init__(self):
            self._row = None
            self.lastrowid = usuario['id']

        def execute(self, sql, params=None):
            ejecutados.append((sql, params))
            if 'FROM usuarios' in sql:
                self._row = usuario
            else:
                self._row = None

        def fetchone(self):
            return self._row

        def close(self):
            return None

    class Conexion:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

    class MySQL:
        connection = Conexion()

    return MySQL()


def test_login_ok_con_bd_simulada(client, monkeypatch):
    password = 'Clave123!'
    with monolito.app.app_context():
        hash_pw = monolito.bcrypt.generate_password_hash(password).decode('utf-8')
    usuario = {
        'id': 7, 'correo': 'cliente@example.com', 'password': hash_pw,
        'rol': 'cliente', 'nombre': 'Ana', 'apellido': 'Perez',
        'telefono': '', 'documento_tipo': '', 'documento_numero': '',
        'estado': 'activo', 'created_at': None, 'token_version': 0,
    }
    ejecutados = []
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr('api.v1.auth.asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, '_schema_listo', True)
    simulado = _mysql_simulado(usuario, ejecutados)
    monkeypatch.setattr(security, 'get_mysql', lambda: simulado)
    monkeypatch.setattr('api.v1.auth.get_mysql', lambda: simulado)

    respuesta = client.post('/api/v1/auth/login', json={
        'correo': 'cliente@example.com',
        'password': password,
    })
    assert respuesta.status_code == 200
    datos = respuesta.get_json()['data']
    assert datos['token_type'] == 'Bearer'
    assert datos['usuario']['correo'] == 'cliente@example.com'
    assert any('INSERT INTO auth_refresh_tokens' in sql
               for sql, _ in ejecutados)


def test_login_falla_con_bd_simulada(client, monkeypatch):
    password = 'Clave123!'
    with monolito.app.app_context():
        hash_pw = monolito.bcrypt.generate_password_hash(password).decode('utf-8')
    usuario = {
        'id': 7, 'correo': 'cliente@example.com', 'password': hash_pw,
        'rol': 'cliente', 'nombre': 'Ana', 'apellido': 'Perez',
        'telefono': '', 'documento_tipo': '', 'documento_numero': '',
        'estado': 'activo', 'created_at': None, 'token_version': 0,
    }
    ejecutados = []
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr('api.v1.auth.asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, '_schema_listo', True)
    simulado = _mysql_simulado(usuario, ejecutados)
    monkeypatch.setattr(security, 'get_mysql', lambda: simulado)
    monkeypatch.setattr('api.v1.auth.get_mysql', lambda: simulado)

    respuesta = client.post('/api/v1/auth/login', json={
        'correo': 'cliente@example.com',
        'password': 'incorrecta',
    })
    assert respuesta.status_code == 401
    error = respuesta.get_json()['error']
    assert error['code'] == 'CREDENCIALES_INVALIDAS'
    assert error['details']['intentos_restantes'] == 4
    assert any('INSERT INTO intentos_usuario' in sql
               for sql, _ in ejecutados)


def test_refresh_rota_el_token(client, monkeypatch):
    usuario = {
        'id': 7, 'correo': 'cliente@example.com', 'rol': 'cliente',
        'nombre': 'Ana', 'apellido': 'Perez', 'telefono': '',
        'documento_tipo': '', 'documento_numero': '', 'estado': 'activo',
        'created_at': None, 'token_version': 0,
    }
    with monolito.app.app_context():
        refresh = create_refresh_token(
            identity='7',
            additional_claims={'ver': 0, 'rol': 'cliente'},
        )
        payload = decode_token(refresh)

    fila_refresh = {
        'id': 1,
        'usuario_id': 7,
        'jti': payload['jti'],
        'token_hash': 'x' * 64,
        'device_id': 'dispositivo-1',
        'expira_en': datetime.now() + timedelta(days=1),
        'revocado': 0,
    }
    ejecutados = []

    class Cursor:
        def __init__(self):
            self._row = None
            self.lastrowid = 1

        def execute(self, sql, params=None):
            ejecutados.append((sql, params))
            if 'FROM auth_refresh_tokens WHERE jti' in sql:
                self._row = fila_refresh
            elif 'FROM usuarios' in sql:
                self._row = usuario
            else:
                self._row = None

        def fetchone(self):
            return self._row

        def close(self):
            return None

    class Conexion:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

    class MySQL:
        connection = Conexion()

    simulado = MySQL()
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr('api.v1.auth.asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, '_schema_listo', True)
    monkeypatch.setattr(security, 'get_mysql', lambda: simulado)
    monkeypatch.setattr('api.v1.auth.get_mysql', lambda: simulado)

    respuesta = client.post(
        '/api/v1/auth/refresh',
        headers={'Authorization': 'Bearer %s' % refresh},
    )
    assert respuesta.status_code == 200
    datos = respuesta.get_json()['data']
    assert datos['access_token']
    assert datos['refresh_token'] != refresh
    assert any('UPDATE auth_refresh_tokens SET revocado=1' in sql
               for sql, _ in ejecutados)
    assert any('INSERT INTO auth_refresh_tokens' in sql
               for sql, _ in ejecutados)


def test_actualizar_perfil_genera_sql_correcto(client, monkeypatch):
    usuario = {
        'id': 7, 'correo': 'cliente@example.com', 'rol': 'cliente',
        'nombre': 'Ana', 'apellido': 'Perez', 'telefono': '',
        'documento_tipo': '', 'documento_numero': '', 'estado': 'activo',
        'created_at': None, 'token_version': 0,
    }
    ejecutados = []

    class Cursor:
        def __init__(self):
            self._row = None

        def execute(self, sql, params=None):
            ejecutados.append((sql, params))
            self._row = usuario if 'FROM usuarios' in sql else None

        def fetchone(self):
            return self._row

        def close(self):
            return None

    class Conexion:
        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

    class MySQL:
        connection = Conexion()

    simulado = MySQL()
    monkeypatch.setattr(security, 'asegurar_esquema', lambda: None)
    monkeypatch.setattr(security, '_schema_listo', True)
    monkeypatch.setattr(security, 'get_mysql', lambda: simulado)
    monkeypatch.setattr('api.v1.auth.get_mysql', lambda: simulado)
    monkeypatch.setattr(
        'services.auth_service.buscar_usuario_por_id',
        lambda _cur, _id: usuario,
    )

    respuesta = client.patch(
        '/api/v1/auth/me',
        json={'nombre': 'Ana Maria'},
        headers={'Authorization': 'Bearer %s' % _token()},
    )
    assert respuesta.status_code == 200

    updates = [(sql, params) for sql, params in ejecutados
               if 'UPDATE usuarios SET' in sql]
    assert len(updates) == 1
    sql, params = updates[0]
    assert 'nombre=%s' in sql
    assert 'WHERE id=%s' in sql
    assert params == ('Ana Maria', 7)


def test_guard_usuario_activo(client, monkeypatch):
    _sin_bd(monkeypatch)
    usuario = {
        'id': 7,
        'correo': 'cliente@example.com',
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
    monkeypatch.setattr(
        'services.auth_service.buscar_usuario_por_id',
        lambda _cur, _id: usuario,
    )
    respuesta = client.get(
        '/api/v1/auth/me',
        headers={'Authorization': 'Bearer %s' % _token()},
    )
    assert respuesta.status_code == 200
    assert respuesta.get_json()['data']['correo'] == 'cliente@example.com'
    assert 'password' not in respuesta.get_json()['data']


def test_guard_token_revocado(client, monkeypatch):
    _sin_bd(monkeypatch)
    usuario = {
        'id': 7, 'correo': 'cliente@example.com', 'rol': 'cliente',
        'nombre': 'Ana', 'apellido': 'Perez', 'telefono': '',
        'documento_tipo': '', 'documento_numero': '', 'estado': 'activo',
        'created_at': None, 'token_version': 1,
    }
    monkeypatch.setattr(
        'services.auth_service.buscar_usuario_por_id',
        lambda _cur, _id: usuario,
    )
    respuesta = client.get(
        '/api/v1/auth/me',
        headers={'Authorization': 'Bearer %s' % _token({'ver': 0})},
    )
    assert respuesta.status_code == 401
    assert respuesta.get_json()['error']['code'] == 'TOKEN_REVOCADO'


def test_guard_cuenta_bloqueada(client, monkeypatch):
    _sin_bd(monkeypatch)
    usuario = {
        'id': 7, 'correo': 'cliente@example.com', 'rol': 'cliente',
        'nombre': 'Ana', 'apellido': 'Perez', 'telefono': '',
        'documento_tipo': '', 'documento_numero': '', 'estado': 'bloqueado',
        'created_at': None, 'token_version': 0,
    }
    monkeypatch.setattr(
        'services.auth_service.buscar_usuario_por_id',
        lambda _cur, _id: usuario,
    )
    respuesta = client.get(
        '/api/v1/auth/me',
        headers={'Authorization': 'Bearer %s' % _token()},
    )
    assert respuesta.status_code == 403
    assert respuesta.get_json()['error']['code'] == 'CUENTA_NO_ACTIVA'
