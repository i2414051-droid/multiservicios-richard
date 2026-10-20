"""
Registro en vivo del recorrido del cliente (Fase 1 del plan de KPIs).

Este modulo NO calcula nada: solo deja constancia de lo que hace el cliente
para que despues se pueda medir.

Por que hace falta: el sistema no registraba ninguna interaccion del cliente
salvo el carrito. Sin un rastro de los pasos, no se puede responder a
"en que paso se quedo la gente" ni a "que busca la gente y no tenemos".

Que registra:

  1. `kpi_sesiones_compra`: una fila por intencion de compra. Nace cuando el
     cliente arma el carrito y avanza por los pasos (carrito -> checkout ->
     formulario -> valido -> venta). Si el formulario falla, se guarda el
     MOTIVO del fallo, que es la parte accionable del indicador.

  2. `kpi_busquedas`: una fila por busqueda en la tienda, con cuantos
     resultados dio y si el cliente acabo comprando. Sirve para saber que
     busca la gente (y que no tenemos) y cuanto convierte el buscador.

Reglas del modulo:

  * Nunca rompe la peticion que se esta atendiendo. Si el registro falla, la
    compra o la busqueda siguen su curso: una metrica no puede tumbar la
    tienda. Por eso todas las escrituras van envueltas en try/except.
  * Las marcas de tiempo las pone MySQL (CURRENT_TIMESTAMP), no Python: el
    panel de KPIs compara los periodos contra el reloj de la base de datos.
  * Escribe en la conexion propia del modulo de KPIs, no en la de la app.
"""
import logging
import uuid

from flask import session

from . import repository

log = logging.getLogger(__name__)

# Pasos posibles del recorrido de compra, en orden.
PASO_CARRITO = 'carrito'
PASO_CHECKOUT = 'checkout'
PASO_FORMULARIO = 'formulario'
PASO_VALIDO = 'valido'
PASO_VENTA = 'venta'

ESTADO_ABIERTA = 'abierta'
ESTADO_CONVERTIDA = 'convertida'
ESTADO_ABANDONADA = 'abandonada'

# Motivos de fallo del checkout. Se guardan como codigo corto, no como el
# texto que ve el cliente: el texto cambia con cada redaccion y el motivo
# tiene que poder agruparse.
MOTIVO_CARRITO_VACIO = 'carrito_vacio'
MOTIVO_STOCK = 'stock_insuficiente'
MOTIVO_CAMPOS = 'campos_obligatorios'
MOTIVO_DOCUMENTO = 'documento_invalido'
MOTIVO_TELEFONO = 'telefono_invalido'
MOTIVO_CSRF = 'csrf_invalido'
MOTIVO_ERROR = 'error_sistema'


def _motivo_desde_mensaje(mensaje):
    """Deduce el codigo del motivo a partir del texto que ve el cliente."""
    texto = (mensaje or '').lower()
    if 'csrf' in texto:
        return MOTIVO_CSRF
    if 'stock insuficiente' in texto:
        return MOTIVO_STOCK
    if 'carrito está vacío' in texto or 'carrito esta vacio' in texto:
        return MOTIVO_CARRITO_VACIO
    if 'documento' in texto or 'dni' in texto or 'ruc' in texto:
        return MOTIVO_DOCUMENTO
    if 'celular' in texto or 'teléfono' in texto or 'telefono' in texto:
        return MOTIVO_TELEFONO
    if 'obligatorios' in texto:
        return MOTIVO_CAMPOS
    return MOTIVO_ERROR


def id_visita():
    """
    Identificador de la visita actual.

    Vive en la cookie de sesion, asi que cambia cuando el cliente abre el
    navegador de nuevo. Sirve para agrupar las busquedas y la sesion de
    compra que pertenecen a la misma visita.
    """
    try:
        visita = session.get('kpi_visita')
        if not visita:
            visita = uuid.uuid4().hex
            session['kpi_visita'] = visita
        return visita
    except Exception:
        return None


def _identidad():
    """(usuario_id, visitante) del cliente actual."""
    try:
        usuario_id = session.get('user_id')
        visitante = session.get('guest_id')
        if usuario_id:
            try:
                usuario_id = int(usuario_id)
            except (TypeError, ValueError):
                usuario_id = None
            visitante = None
        elif not visitante:
            visitante = uuid.uuid4().hex
            session['guest_id'] = visitante
        return usuario_id, visitante
    except Exception:
        return None, None


def _fila_abierta(usuario_id, visitante):
    """Sesion de compra abierta del cliente, si existe."""
    cur = repository.get_cursor()
    try:
        if usuario_id:
            cur.execute(
                """SELECT id FROM kpi_sesiones_compra
                   WHERE estado=%s AND usuario_id=%s
                   ORDER BY id DESC LIMIT 1""",
                (ESTADO_ABIERTA, usuario_id),
            )
        else:
            cur.execute(
                """SELECT id FROM kpi_sesiones_compra
                   WHERE estado=%s AND visitante=%s
                   ORDER BY id DESC LIMIT 1""",
                (ESTADO_ABIERTA, visitante),
            )
        fila = cur.fetchone()
        return fila['id'] if fila else None
    finally:
        cur.close()


def sesion_compra(paso, motivo_fallo=None, venta_id=None, estado=None):
    """
    Deja constancia de un paso del recorrido de compra.

    Crea la sesion si el cliente no tenia ninguna abierta y avanza la que ya
    tiene. Nunca lanza: si la escritura falla, la compra sigue adelante.
    """
    try:
        usuario_id, visitante = _identidad()
        if not usuario_id and not visitante:
            return None

        sesion_id = _fila_abierta(usuario_id, visitante)
        cur = repository.get_cursor()
        try:
            if sesion_id is None:
                cur.execute(
                    """INSERT INTO kpi_sesiones_compra
                       (usuario_id, visitante, visita_id, iniciada_at, estado,
                        paso_alcanzado, motivo_fallo, venta_id)
                       VALUES(%s,%s,%s,NOW(),%s,%s,%s,%s)""",
                    (usuario_id, visitante, id_visita(),
                     estado or ESTADO_ABIERTA, paso, motivo_fallo, venta_id),
                )
                return cur.lastrowid

            campos = ['paso_alcanzado=%s']
            params = [paso]
            if motivo_fallo is not None:
                campos.append('motivo_fallo=%s')
                params.append(motivo_fallo)
            if venta_id is not None:
                campos.append('venta_id=%s')
                params.append(venta_id)
            if estado is not None:
                campos.append('estado=%s')
                params.append(estado)
                if estado != ESTADO_ABIERTA:
                    campos.append('cerrada_at=NOW()')
            params.append(sesion_id)
            cur.execute(
                "UPDATE kpi_sesiones_compra SET %s WHERE id=%%s" % ', '.join(campos),
                tuple(params),
            )
            return sesion_id
        finally:
            cur.close()
    except Exception as exc:
        log.warning('KPI registro: no se pudo guardar el paso "%s": %s', paso, exc)
        return None


def fallo_checkout(mensaje):
    """Registra que el cliente se quedo en el paso del formulario."""
    return sesion_compra(PASO_FORMULARIO,
                          motivo_fallo=_motivo_desde_mensaje(mensaje))


def registrar_busqueda(texto, resultados):
    """
    Guarda una busqueda de la tienda.

    Solo se registran las que el cliente escribe: un filtro por categoria no
    es una busqueda y ensuciaria el indice de lo que pide la gente.
    """
    texto = (texto or '').strip()
    if not texto:
        return None
    try:
        usuario_id, visitante = _identidad()
        cur = repository.get_cursor()
        try:
            cur.execute(
                """INSERT INTO kpi_busquedas
                   (usuario_id, visitante, visita_id, texto, resultados, fecha)
                   VALUES(%s,%s,%s,%s,%s,NOW())""",
                (usuario_id, visitante, id_visita(), texto[:120], int(resultados or 0)),
            )
            return cur.lastrowid
        finally:
            cur.close()
    except Exception as exc:
        log.warning('KPI registro: no se pudo guardar la busqueda: %s', exc)
        return None


def marcar_producto_visto(producto_id):
    """
    Anota que el cliente acabo de ver un producto.

    Se guarda en la ultima busqueda de la visita: si buscaba y luego abrio un
    producto, esa busqueda es la que lo trajo.
    """
    try:
        visita = id_visita()
        if not visita:
            return
        cur = repository.get_cursor()
        try:
            cur.execute(
                """UPDATE kpi_busquedas SET producto_visto_id=%s
                   WHERE id = (SELECT id FROM (
                       SELECT id FROM kpi_busquedas
                       WHERE visita_id=%s AND producto_visto_id IS NULL
                       ORDER BY id DESC LIMIT 1) AS ult)""",
                (int(producto_id), visita),
            )
        finally:
            cur.close()
    except Exception as exc:
        log.warning('KPI registro: no se pudo marcar el producto visto: %s', exc)


def marcar_busqueda_convertida(usuario_id=None):
    """
    Marca las busquedas del cliente como convertidas en compra.

    Se llama cuando se crea la venta: asi el buscador se puede medir de
    verdad (busquedas -> compras) y no solo en teoria.
    """
    try:
        if usuario_id is None:
            usuario_id, _ = _identidad()
        if not usuario_id:
            return
        cur = repository.get_cursor()
        try:
            cur.execute(
                """UPDATE kpi_busquedas SET convertido=1
                   WHERE usuario_id=%s AND convertido=0
                     AND fecha > NOW() - INTERVAL 30 DAY""",
                (int(usuario_id),),
            )
        finally:
            cur.close()
    except Exception as exc:
        log.warning('KPI registro: no se pudo marcar la busqueda como convertida: %s', exc)
