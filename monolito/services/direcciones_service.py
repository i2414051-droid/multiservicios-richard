"""Direcciones de entrega del cliente (API v1).

Mismas tablas y reglas que la web: `direcciones` pertenece al `usuario_id`
(guardado como texto), solo una direccion puede quedar como predeterminada, y
el listado prioriza la predeterminada. No se agregan topes que la web no tenga.
"""
from .tienda_service import TiendaError


def _texto(valor, limite):
    return ' '.join(str(valor or '').split())[:limite]


def _entero(valor, por_defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto


def _usuario(usuario_id):
    return str(usuario_id)


def listar(cur, usuario_id):
    """Direcciones del usuario, con la predeterminada primero."""
    cur.execute(
        "SELECT * FROM direcciones WHERE usuario_id=%s "
        "ORDER BY predeterminada DESC, created_at DESC, id DESC",
        (_usuario(usuario_id),),
    )
    return cur.fetchall()


def obtener(cur, usuario_id, direccion_id):
    cur.execute(
        "SELECT * FROM direcciones WHERE id=%s AND usuario_id=%s",
        (_entero(direccion_id), _usuario(usuario_id)),
    )
    return cur.fetchone()


def _validar(datos):
    direccion = _texto(datos.get('direccion'), 300)
    if len(direccion) < 5:
        raise TiendaError(
            'DIRECCION_INVALIDA',
            'La direccion debe tener al menos 5 caracteres.',
            422,
            {'campo': 'direccion'},
        )
    return {
        'direccion': direccion,
        'distrito': _texto(datos.get('distrito'), 150),
        'referencia': _texto(datos.get('referencia'), 300),
        'predeterminada': 1 if datos.get('predeterminada') else 0,
    }


def _quitar_predeterminada(cur, usuario_id):
    cur.execute(
        "UPDATE direcciones SET predeterminada=0 WHERE usuario_id=%s",
        (_usuario(usuario_id),),
    )


def crear(cur, usuario_id, datos):
    valores = _validar(datos)
    if valores['predeterminada']:
        _quitar_predeterminada(cur, usuario_id)
    cur.execute(
        "INSERT INTO direcciones "
        "(usuario_id, direccion, distrito, referencia, predeterminada) "
        "VALUES (%s,%s,%s,%s,%s)",
        (_usuario(usuario_id), valores['direccion'], valores['distrito'],
         valores['referencia'], valores['predeterminada']),
    )
    return cur.lastrowid


def actualizar(cur, usuario_id, direccion_id, datos):
    if not obtener(cur, usuario_id, direccion_id):
        raise TiendaError('DIRECCION_NO_ENCONTRADA',
                          'La direccion no existe.', 404)
    valores = _validar(datos)
    if valores['predeterminada']:
        _quitar_predeterminada(cur, usuario_id)
    cur.execute(
        "UPDATE direcciones SET direccion=%s, distrito=%s, referencia=%s, "
        "predeterminada=%s WHERE id=%s AND usuario_id=%s",
        (valores['direccion'], valores['distrito'], valores['referencia'],
         valores['predeterminada'], _entero(direccion_id),
         _usuario(usuario_id)),
    )
    return obtener(cur, usuario_id, direccion_id)


def eliminar(cur, usuario_id, direccion_id):
    cur.execute(
        "DELETE FROM direcciones WHERE id=%s AND usuario_id=%s",
        (_entero(direccion_id), _usuario(usuario_id)),
    )
    return _entero(getattr(cur, 'rowcount', 0))


def marcar_predeterminada(cur, usuario_id, direccion_id):
    if not obtener(cur, usuario_id, direccion_id):
        raise TiendaError('DIRECCION_NO_ENCONTRADA',
                          'La direccion no existe.', 404)
    _quitar_predeterminada(cur, usuario_id)
    cur.execute(
        "UPDATE direcciones SET predeterminada=1 WHERE id=%s AND usuario_id=%s",
        (_entero(direccion_id), _usuario(usuario_id)),
    )
    return obtener(cur, usuario_id, direccion_id)
