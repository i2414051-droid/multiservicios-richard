"""Dependencias compartidas por las vistas de la API v1."""
from flask import current_app, request

from .errors import ApiError


def get_mysql():
    """Instancia de Flask-MySQLdb registrada por app.py."""
    return current_app.extensions['api_mysql']


def get_bcrypt():
    """Instancia de Flask-Bcrypt registrada por app.py."""
    return current_app.extensions['api_bcrypt']


def cuerpo_json():
    """Devuelve el cuerpo JSON como dict o lanza 400."""
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        raise ApiError('JSON_INVALIDO',
                       'El cuerpo de la peticion debe ser un objeto JSON.', 400)
    return datos


def paginacion(por_defecto=20, maximo=100):
    """Normaliza ?page y ?per_page."""
    try:
        page = int(request.args.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get('per_page', por_defecto))
    except (TypeError, ValueError):
        per_page = por_defecto
    page = max(1, page)
    per_page = min(max(1, per_page), maximo)
    return page, per_page


def abrir_cursor():
    """Abre un cursor de la conexion MySQL de la app.

    Devuelve `(mysql, cursor)` para que la vista pueda hacer commit/rollback.
    Si la conexion no esta disponible lanza 503 con el envelope de la API.
    """
    mysql = get_mysql()
    try:
        return mysql, mysql.connection.cursor()
    except Exception as exc:
        raise ApiError('BD_NO_DISPONIBLE',
                       'No se pudo conectar con la base de datos.', 503) from exc


def error_bd(exc, mensaje='No se pudo completar la operacion en la base de datos.'):
    """Registra el error real y devuelve un ApiError 503 para el cliente."""
    try:
        current_app.logger.error('API v1: %s', exc, exc_info=True)
    except Exception:
        pass
    return ApiError('BD_NO_DISPONIBLE', mensaje, 503)
