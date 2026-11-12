"""Punto de montaje de la API v1 dentro del monolito Flask.

La web sigue registrada en app.py con cookie de sesion y CSRF. Aqui solo se
agrega el arbol `/api/v1`, que usa JWT y queda exento del CSRF global.
"""
from flask import request

from .errors import fail, registrar_handlers
from .openapi import init_openapi
from .security import init_jwt
from .v1 import bp as v1_bp


def create_api(app, mysql, bcrypt):
    """Registra la API v1 en una app Flask ya configurada.

    `mysql` y `bcrypt` se inyectan para no importar app.py desde el paquete
    (evita el ciclo app -> api -> app).
    """
    app.extensions['api_mysql'] = mysql
    app.extensions['api_bcrypt'] = bcrypt

    init_jwt(app)
    registrar_handlers(v1_bp)
    app.register_blueprint(v1_bp)
    init_openapi(app)

    @app.errorhandler(404)
    def _api_no_encontrado(exc):
        if request.path.startswith('/api/v1'):
            return fail('NO_ENCONTRADO', 'La ruta solicitada no existe.', 404)
        return exc

    @app.errorhandler(405)
    def _api_metodo_no_permitido(exc):
        if request.path.startswith('/api/v1'):
            return fail('METODO_NO_PERMITIDO',
                        'El metodo HTTP no esta permitido para esta ruta.', 405)
        return exc

    return app
