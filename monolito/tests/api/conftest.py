"""Configuracion comun de las pruebas de contrato de la API v1."""
import pytest

import app as monolito


@pytest.fixture(autouse=True)
def _reiniciar_limites_de_tasa():
    """Limpia el contador global de rate limit antes y despues de cada prueba.

    `app._limitar_peticiones` cuenta TODAS las peticiones no seguras por IP en
    una ventana de 60 s (`todo:<ip>`). Como la suite entera corre en segundos,
    sin este reseteo los POST de una prueba contarian para la siguiente y
    aparecerian 429 que no tienen que ver con el codigo bajo prueba.
    """
    monolito._RATE.clear()
    yield
    monolito._RATE.clear()
