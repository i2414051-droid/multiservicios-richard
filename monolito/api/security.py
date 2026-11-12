"""JWT de la API v1: access token, refresh rotativo y guards por rol.

La web sigue usando cookie de sesion y CSRF. Este modulo solo se aplica a
`/api/v1/*`, que queda exento del CSRF global.
"""
import os
import threading
from datetime import datetime, timedelta
from functools import wraps

from flask import current_app, g, has_request_context, request
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)

from services import auth_service

from .deps import get_mysql
from .errors import ApiError, fail

jwt = JWTManager()

_schema_lock = threading.Lock()
_schema_listo = False

_ADMIN_ROLES = ('admin', 'administrador', 'superadmin')


def _entero_env(nombre, por_defecto):
    try:
        return int(os.environ.get(nombre, por_defecto))
    except (TypeError, ValueError):
        return por_defecto


def init_jwt(app):
    app.config.setdefault(
        'JWT_SECRET_KEY',
        os.environ.get('JWT_SECRET_KEY') or app.secret_key)
    app.config.setdefault(
        'JWT_ACCESS_TOKEN_EXPIRES',
        timedelta(minutes=_entero_env('JWT_ACCESS_MINUTOS', 30)))
    app.config.setdefault(
        'JWT_REFRESH_TOKEN_EXPIRES',
        timedelta(days=_entero_env('JWT_REFRESH_DIAS', 30)))
    app.config.setdefault('JWT_TOKEN_LOCATION', ['headers'])
    app.config.setdefault('JWT_HEADER_TYPE', 'Bearer')
    app.config.setdefault('JWT_HEADER_NAME', 'Authorization')

    jwt.init_app(app)

    @jwt.unauthorized_loader
    def _sin_token(_motivo):
        return fail('NO_AUTENTICADO',
                    'Falta el token de acceso (Authorization: Bearer).', 401)

    @jwt.invalid_token_loader
    def _token_invalido(_motivo):
        return fail('TOKEN_INVALIDO', 'El token de acceso no es valido.', 401)

    @jwt.expired_token_loader
    def _token_expirado(_cabecera, _payload):
        return fail('TOKEN_EXPIRADO',
                    'El token expiro. Usa el refresh token para renovarlo.', 401)

    @jwt.revoked_token_loader
    def _token_revocado(_cabecera, _payload):
        return fail('TOKEN_REVOCADO', 'El token fue revocado.', 401)

    @jwt.needs_fresh_token_loader
    def _token_no_fresco(_cabecera, _payload):
        return fail('TOKEN_NO_FRESCO',
                    'Esta operacion exige un token de acceso reciente.', 401)


def asegurar_esquema():
    """Crea las tablas propias de la API la primera vez que se necesitan.

    Es idempotente y tolera que la columna `token_version` ya exista. Si la
    base no responde, lanza ApiError 503 para que el cliente reintente.
    """
    global _schema_listo
    if _schema_listo:
        return
    with _schema_lock:
        if _schema_listo:
            return
        mysql = get_mysql()
        try:
            cur = mysql.connection.cursor()
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        usuario_id  INT NOT NULL,
                        jti         VARCHAR(64) NOT NULL,
                        token_hash  CHAR(64) NOT NULL,
                        device_id   VARCHAR(120) NULL,
                        user_agent  VARCHAR(255) NULL,
                        ip          VARCHAR(45) NULL,
                        creado_en   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expira_en   DATETIME NOT NULL,
                        revocado    TINYINT DEFAULT 0,
                        revocado_en DATETIME NULL,
                        UNIQUE KEY uq_refresh_jti (jti),
                        INDEX ix_refresh_usuario (usuario_id),
                        INDEX ix_refresh_expira (expira_en)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                try:
                    cur.execute(
                        "ALTER TABLE usuarios ADD COLUMN token_version INT DEFAULT 0")
                except Exception:
                    # La columna ya existe o el usuario no puede alterar:
                    # si ya existe, todo sigue; si no, la consulta posterior
                    # fallara y se reportara como 503.
                    pass
                try:
                    # Acelera el carrito y ayuda a serializar agregados del
                    # mismo producto. Si ya existe, MySQL devuelve 1061.
                    cur.execute(
                        "ALTER TABLE carrito ADD INDEX "
                        "ix_carrito_usuario_producto (usuario_id, producto_id)")
                except Exception:
                    pass
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS api_idempotencia (
                        id           INT AUTO_INCREMENT PRIMARY KEY,
                        usuario_id   INT NOT NULL,
                        clave        VARCHAR(120) NOT NULL,
                        endpoint     VARCHAR(120) NOT NULL,
                        request_hash CHAR(64) NOT NULL,
                        respuesta    MEDIUMTEXT NULL,
                        creado_en    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_idem_usuario_clave (usuario_id, clave)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                mysql.connection.commit()
            finally:
                cur.close()
        except ApiError:
            raise
        except Exception as exc:
            try:
                current_app.logger.error(
                    'API v1: no se pudo preparar el esquema: %s', exc)
            except Exception:
                pass
            raise ApiError('BD_NO_DISPONIBLE',
                           'No se pudo preparar la base de datos. Intenta de nuevo.',
                           503) from exc
        _schema_listo = True


def emitir_tokens(usuario, device_id=None):
    """Crea access + refresh y guarda el refresh hasheado en BD."""
    asegurar_esquema()
    identidad = str(usuario['id'])
    claims = {
        'rol': (usuario.get('rol') or 'cliente').lower(),
        'nombre': usuario.get('nombre') or '',
        'ver': int(usuario.get('token_version') or 0),
    }
    access = create_access_token(identity=identidad, additional_claims=claims)
    refresh = create_refresh_token(identity=identidad, additional_claims=claims)
    datos = decode_token(refresh)

    expira_en = datetime.fromtimestamp(datos['exp'])
    user_agent = None
    ip = None
    if has_request_context():
        user_agent = request.user_agent.string if request.user_agent else None
        ip = request.remote_addr

    mysql = get_mysql()
    cur = mysql.connection.cursor()
    try:
        # Limpieza oportunista: no deja crecer la tabla con tokens vencidos.
        auth_service.limpiar_refresh_expirados(cur, usuario['id'])
        auth_service.crear_registro_refresh(
            cur,
            usuario['id'],
            datos['jti'],
            auth_service.hash_token(refresh),
            expira_en,
            device_id=device_id,
            user_agent=user_agent,
            ip=ip,
        )
        mysql.connection.commit()
    finally:
        cur.close()
    return access, refresh, datos


def _cargar_usuario():
    """Carga el usuario del token y valida estado y version."""
    identidad = get_jwt_identity()
    claims = get_jwt()
    if identidad is None:
        raise ApiError('NO_AUTENTICADO', 'Falta el token de acceso.', 401)

    mysql = get_mysql()
    cur = mysql.connection.cursor()
    try:
        usuario = auth_service.buscar_usuario_por_id(cur, identidad)
    except Exception as exc:
        try:
            current_app.logger.error('API v1: error cargando usuario: %s', exc)
        except Exception:
            pass
        raise ApiError('BD_NO_DISPONIBLE',
                       'No se pudo consultar la cuenta. Intenta de nuevo.', 503) from exc
    finally:
        cur.close()

    if not usuario:
        raise ApiError('USUARIO_NO_ENCONTRADO', 'La cuenta no existe.', 401)

    estado = (usuario.get('estado') or 'activo').lower()
    if estado in ('bloqueado', 'inactivo'):
        raise ApiError('CUENTA_NO_ACTIVA',
                       'Tu cuenta esta %s. Contacta al administrador.' % estado, 403)

    if int(claims.get('ver', 0)) != int(usuario.get('token_version') or 0):
        raise ApiError('TOKEN_REVOCADO',
                       'La sesion fue revocada. Inicia sesion de nuevo.', 401)
    return usuario


def requiere_cliente(fn):
    """Exige access token valido y deja el usuario en g.usuario."""
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        asegurar_esquema()
        g.usuario = _cargar_usuario()
        return fn(*args, **kwargs)
    return wrapper


def requiere_admin(fn):
    """Exige access token valido y rol administrador."""
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        asegurar_esquema()
        usuario = _cargar_usuario()
        if (usuario.get('rol') or '').lower() not in _ADMIN_ROLES:
            raise ApiError('SIN_PERMISO',
                           'Se requiere rol administrador.', 403)
        g.usuario = usuario
        return fn(*args, **kwargs)
    return wrapper


def cliente_opcional(fn):
    """Permite entrar sin token; si hay token valido, deja el usuario en g."""
    @wraps(fn)
    @jwt_required(optional=True)
    def wrapper(*args, **kwargs):
        identidad = get_jwt_identity()
        if identidad is None:
            g.usuario = None
        else:
            asegurar_esquema()
            g.usuario = _cargar_usuario()
        return fn(*args, **kwargs)
    return wrapper
