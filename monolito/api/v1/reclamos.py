"""Libro de Reclamaciones (API v1).

`POST /reclamos` es publico (como el formulario web); el resto exige sesion.
La constancia se protege con el token del envio o con la sesion del dueno.
"""
from flask import g, request
from pydantic import BaseModel, Field

from services import reclamos_service
from services.tienda_service import TiendaError

from ..deps import abrir_cursor, cuerpo_json, error_bd, paginacion
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import cliente_opcional, requiere_cliente
from ..serializers import reclamo_a_json
from . import bp


class ReclamoIn(BaseModel):
    tipo: str = Field(default='reclamo', max_length=20)
    consumidor_nombre: str = Field(min_length=1, max_length=200)
    consumidor_documento_tipo: str = Field(default='DNI', max_length=20)
    consumidor_documento: str = Field(default='', max_length=20)
    consumidor_domicilio: str = Field(default='', max_length=300)
    consumidor_telefono: str | None = Field(default=None, max_length=20)
    consumidor_correo: str = Field(default='', max_length=200)
    bien_descripcion: str = Field(default='', max_length=500)
    bien_monto: str | float | None = None
    detalle: str = Field(default='', max_length=2000)
    pedido: str = Field(default='', max_length=1000)


def _correo_actual():
    usuario = getattr(g, 'usuario', None) or {}
    return (usuario.get('correo') or '').strip()


def _api_error(exc):
    return ApiError(exc.code, exc.message, exc.status, exc.details)


def _rollback(mysql):
    try:
        mysql.connection.rollback()
    except Exception:
        pass


@bp.get('/reclamos')
@doc(
    summary='Listar mis reclamos',
    tags=['Reclamos'],
    auth=True,
    response_model='ReclamoLista',
    parameters=[
        {'name': 'page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
        {'name': 'per_page', 'in': 'query',
         'schema': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                    'default': 10}},
    ],
    responses={200: 'Hojas registradas con el correo de la cuenta',
               401: 'Token invalido'},
)
@requiere_cliente
def listar_reclamos():
    page, per_page = paginacion(10, 50)
    correo = _correo_actual()
    if not correo:
        raise ApiError('CORREO_REQUERIDO',
                       'La cuenta no tiene correo registrado.', 409)

    mysql, cur = abrir_cursor()
    try:
        filas, total = reclamos_service.listar(cur, correo, page, per_page)
        pages = (total + per_page - 1) // per_page if total else 0
        return ok([reclamo_a_json(fila) for fila in filas],
                  meta={'page': page, 'per_page': per_page,
                        'total': total, 'pages': pages})
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudieron cargar los reclamos.') from exc
    finally:
        cur.close()


@bp.post('/reclamos')
@doc(
    summary='Registrar una hoja de reclamacion',
    tags=['Reclamos'],
    request='ReclamoIn',
    response_model='Reclamo',
    success_status=201,
    responses={201: 'Hoja registrada; incluye el token de la constancia',
               422: 'Datos invalidos'},
)
@cliente_opcional
def crear_reclamo():
    datos = validar_modelo(ReclamoIn, cuerpo_json())
    mysql, cur = abrir_cursor()
    try:
        reclamo_id, _token, _codigo = reclamos_service.crear(
            cur, datos.model_dump())
        mysql.connection.commit()
        fila = reclamos_service.obtener(cur, reclamo_id)
        return ok(reclamo_a_json(fila), status=201)
    except TiendaError as exc:
        _rollback(mysql)
        raise _api_error(exc) from exc
    except Exception as exc:
        _rollback(mysql)
        raise error_bd(exc, 'No se pudo registrar la reclamacion.') from exc
    finally:
        cur.close()


@bp.get('/reclamos/<int:reclamo_id>')
@doc(
    summary='Ver una hoja de reclamacion',
    tags=['Reclamos'],
    auth=True,
    response_model='Reclamo',
    responses={200: 'Hoja del cliente', 401: 'Token invalido',
               404: 'No existe o no pertenece al correo de la cuenta'},
)
@requiere_cliente
def ver_reclamo(reclamo_id):
    correo = _correo_actual()
    mysql, cur = abrir_cursor()
    try:
        fila = reclamos_service.obtener(cur, reclamo_id)
        if not fila or not reclamos_service.autorizado(fila, correo=correo):
            raise ApiError('RECLAMO_NO_ENCONTRADO',
                           'La hoja de reclamacion no existe.', 404)
        return ok(reclamo_a_json(fila))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la reclamacion.') from exc
    finally:
        cur.close()


@bp.get('/reclamos/<int:reclamo_id>/constancia')
@doc(
    summary='Constancia de una hoja de reclamacion',
    tags=['Reclamos'],
    parameters=[
        {'name': 'token', 'in': 'query', 'required': False,
         'schema': {'type': 'string'},
         'description': 'Token devuelto al registrar la hoja. Tambien vale la '
                        'sesion del dueno.'},
    ],
    response_model='Reclamo',
    responses={200: 'Copia para el consumidor', 403: 'Token incorrecto',
               404: 'No existe'},
)
@cliente_opcional
def constancia_reclamo(reclamo_id):
    token = (request.args.get('token') or '').strip()
    correo = _correo_actual() or None
    mysql, cur = abrir_cursor()
    try:
        fila = reclamos_service.obtener(cur, reclamo_id)
        if not fila:
            raise ApiError('RECLAMO_NO_ENCONTRADO',
                           'La hoja de reclamacion no existe.', 404)
        if not reclamos_service.autorizado(fila, token=token, correo=correo):
            raise ApiError('CONSTANCIA_NO_AUTORIZADA',
                           'Falta el token correcto de la constancia.', 403)
        return ok(reclamo_a_json(fila))
    except ApiError:
        raise
    except Exception as exc:
        raise error_bd(exc, 'No se pudo cargar la constancia.') from exc
    finally:
        cur.close()
