"""Errores y respuestas uniformes de la API v1."""
from flask import current_app, jsonify
from flask_jwt_extended.exceptions import (
    FreshTokenRequired,
    InvalidHeaderError,
    InvalidQueryParamError,
    JWTDecodeError,
    NoAuthorizationError,
    RevokedTokenError,
    UserClaimsVerificationError,
    UserLookupError,
    WrongTokenError,
)
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWTError
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException


class ApiError(Exception):
    """Error de negocio o de validacion con codigo estable para el cliente."""

    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def ok(data=None, meta=None, status=200):
    cuerpo = {'ok': True}
    if data is not None:
        cuerpo['data'] = data
    if meta is not None:
        cuerpo['meta'] = meta
    return jsonify(cuerpo), status


def fail(code, message, status=400, details=None):
    error = {'code': code, 'message': message}
    if details is not None:
        error['details'] = details
    return jsonify({'ok': False, 'error': error}), status


def _detalles_validacion(exc):
    detalles = []
    for error in exc.errors():
        campo = '.'.join(str(parte) for parte in error.get('loc', ())) or 'body'
        detalles.append({'campo': campo, 'mensaje': error.get('msg', 'Valor invalido.')})
    return detalles


def validar_modelo(modelo, datos):
    """Valida un dict con Pydantic y lanza ApiError(422) si falla."""
    try:
        return modelo(**datos)
    except ValidationError as exc:
        raise ApiError('VALIDACION', 'Datos invalidos.', 422,
                       _detalles_validacion(exc)) from exc


def registrar_handlers(bp):
    @bp.errorhandler(ApiError)
    def _manejar_api_error(exc):
        return fail(exc.code, exc.message, exc.status, exc.details)

    @bp.errorhandler(Exception)
    def _manejar_error_no_controlado(exc):
        # Los errores de JWT se producen dentro de las vistas del blueprint.
        # El handler generico de Flask los interceptaria antes que el handler
        # especifico de Flask-JWT-Extended, asi que se traducen aqui.
        if isinstance(exc, NoAuthorizationError):
            return fail('NO_AUTENTICADO',
                        'Falta el token de acceso (Authorization: Bearer).', 401)
        if isinstance(exc, ExpiredSignatureError):
            return fail('TOKEN_EXPIRADO',
                        'El token expiro. Usa el refresh token para renovarlo.', 401)
        if isinstance(exc, RevokedTokenError):
            return fail('TOKEN_REVOCADO', 'El token fue revocado.', 401)
        if isinstance(exc, FreshTokenRequired):
            return fail('TOKEN_NO_FRESCO',
                        'Esta operacion exige un token de acceso reciente.', 401)
        if isinstance(exc, UserLookupError):
            return fail('USUARIO_NO_ENCONTRADO', 'La cuenta no existe.', 401)
        if isinstance(exc, (InvalidHeaderError, WrongTokenError, JWTDecodeError,
                            InvalidQueryParamError, InvalidTokenError,
                            UserClaimsVerificationError, PyJWTError)):
            return fail('TOKEN_INVALIDO', 'El token de acceso no es valido.', 401)
        if isinstance(exc, HTTPException):
            return fail('HTTP_%s' % (exc.code or 500),
                        exc.description or 'Error HTTP.', exc.code or 500)
        try:
            current_app.logger.error('API v1: error no controlado: %s', exc,
                                     exc_info=True)
        except Exception:
            pass
        return fail('ERROR_INTERNO', 'Ocurrio un error interno.', 500)
