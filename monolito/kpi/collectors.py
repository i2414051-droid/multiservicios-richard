"""
Instrumentacion minima del modulo de KPIs.

El panel tecnico (rendimiento, disponibilidad, estabilidad y seguridad) se
retiro junto con su blueprint, asi que aqui ya no queda nada que cronometre
peticiones ni que mida la latencia de la base de datos.

Solo sobrevive `persist_error`, que app.py invoca desde su manejador global de
excepciones para dejar una traza diagnostica de los errores no controlados.
"""
from flask import request

from . import repository


def _current_route() -> str:
    """
    Nombre del endpoint en vez de la ruta literal.

    request.endpoint agrupa /producto/1 y /producto/2 bajo 'producto', lo que
    mantiene baja la cardinalidad de la tabla de errores.
    """
    endpoint = request.endpoint
    if endpoint and not endpoint.startswith('static'):
        return endpoint
    return request.path[:120]


def persist_error(error, origen='servidor'):
    """
    Guarda una excepcion no controlada para su diagnostico posterior.

    No se registra como errorhandler propio a proposito: Flask resuelve el
    manejador de una excepcion con el primero que encuentra, y app.py ya define
    uno global. Si este modulo declarara el suyo, el suyo no se ejecutaria
    nunca. Por eso app.py llama a esta funcion desde dentro de su manejador
    existente, que es ademas el punto correcto: se registra el error antes de
    decidir que mensaje se le devuelve al usuario.

    Nunca propaga excepciones: registrar una metrica no puede romper la
    atencion de la peticion que estaba fallando.
    """
    try:
        route = _current_route()
    except Exception:
        route = None
    try:
        repository.insert_error_event(
            ruta=route,
            tipo_error=type(error).__name__,
            mensaje=str(error),
            origen=origen,
        )
    except Exception:
        pass
