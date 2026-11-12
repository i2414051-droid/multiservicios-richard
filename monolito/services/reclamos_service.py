"""Libro de Reclamaciones (API v1).

Replica la validacion y las consultas de la web:
  * la hoja se guarda en `reclamaciones` y queda identificada por un `token`
    aleatorio (para la constancia) y un `codigo` legible `LR-<anio>-<id>`,
  * el plazo de respuesta por defecto es 15 dias (`DIAS_RESPUESTA_RECLAMO`),
  * el cliente ve sus hojas por correo, no por `usuario_id`.
"""
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from .tienda_service import TiendaError

DIAS_RESPUESTA_RECLAMO = int(os.environ.get('DIAS_RESPUESTA_RECLAMO', '15'))

TIPOS_RECLAMO = ('reclamo', 'queja')
TIPOS_DOCUMENTO = ('DNI', 'CE', 'PASAPORTE')

_RE_NOMBRE_PERSONA = re.compile(
    r'^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*$'
)
_RE_CORREO = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_RE_DOC_CE = re.compile(r'[A-Za-z0-9]{6,12}')


def _texto(valor, limite=None):
    limpio = ' '.join(str(valor if valor is not None else '').split())
    return limpio[:limite] if limite else limpio


def _entero(valor, por_defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto


def dias_para_limite(fecha_limite):
    """Dias que faltan para el plazo de respuesta (negativo si ya vencio)."""
    if not fecha_limite:
        return None
    try:
        return (fecha_limite.date() - datetime.now().date()).days
    except AttributeError:
        return None


# ─────────────────────────────────────────────
# VALIDACION
# ─────────────────────────────────────────────
def _validar(datos):
    """Normaliza y valida la hoja. Lanza TiendaError(422) con la lista de errores."""
    normalizado = {
        'tipo': _texto(datos.get('tipo') or 'reclamo').lower(),
        'consumidor_nombre': _texto(datos.get('consumidor_nombre'), 200),
        'consumidor_documento_tipo': _texto(
            datos.get('consumidor_documento_tipo') or 'DNI').upper(),
        'consumidor_documento': _texto(datos.get('consumidor_documento'), 20),
        'consumidor_domicilio': _texto(datos.get('consumidor_domicilio'), 300),
        'consumidor_telefono': _texto(datos.get('consumidor_telefono'), 20),
        'consumidor_correo': _texto(datos.get('consumidor_correo'), 200),
        'bien_tipo': 'producto',
        'bien_descripcion': _texto(datos.get('bien_descripcion'), 500),
        'detalle': _texto(datos.get('detalle'), 2000),
        'pedido': _texto(datos.get('pedido'), 1000),
    }
    errores = []

    def _error(campo, mensaje):
        errores.append({'campo': campo, 'mensaje': mensaje})

    if normalizado['tipo'] not in TIPOS_RECLAMO:
        _error('tipo', 'Selecciona si tu caso es un reclamo o una queja.')

    nombre = normalizado['consumidor_nombre']
    if (not nombre or not _RE_NOMBRE_PERSONA.match(nombre)
            or sum(c.isalpha() for c in nombre) < 4):
        _error('consumidor_nombre',
               'El nombre completo debe tener al menos 4 letras '
               '(solo letras y espacios).')

    tipo_doc = normalizado['consumidor_documento_tipo']
    doc = normalizado['consumidor_documento']
    if tipo_doc not in TIPOS_DOCUMENTO:
        _error('consumidor_documento_tipo',
               'Selecciona un tipo de documento valido.')
    elif tipo_doc == 'DNI':
        if not re.fullmatch(r'\d{8}', doc):
            _error('consumidor_documento',
                   'El DNI debe tener exactamente 8 numeros.')
    elif not _RE_DOC_CE.fullmatch(doc):
        _error('consumidor_documento',
               'El carne o pasaporte debe tener entre 6 y 12 letras o numeros.')

    if len(normalizado['consumidor_domicilio']) < 5:
        _error('consumidor_domicilio',
               'Ingresa tu domicilio completo (minimo 5 caracteres).')

    telefono = normalizado['consumidor_telefono']
    if telefono and not re.fullmatch(r'\d{9}', telefono):
        _error('consumidor_telefono',
               'El telefono / celular debe tener exactamente 9 digitos.')

    correo = normalizado['consumidor_correo']
    if not correo:
        _error('consumidor_correo',
               'El correo electronico es obligatorio (ahi se enviara la respuesta).')
    elif not _RE_CORREO.match(correo):
        _error('consumidor_correo',
               'El correo electronico debe incluir @ y un dominio valido.')

    if len(normalizado['bien_descripcion']) < 3:
        _error('bien_descripcion',
               'Describe el producto comprado (minimo 3 caracteres).')

    monto = None
    crudo_monto = datos.get('bien_monto')
    if crudo_monto not in (None, ''):
        try:
            monto = round(float(str(crudo_monto).replace(',', '.')), 2)
            if monto < 0:
                raise ValueError
        except (TypeError, ValueError):
            _error('bien_monto', 'El monto debe ser un numero mayor o igual a 0.')
            monto = None
    normalizado['bien_monto'] = None if monto is None else str(monto)

    if len(normalizado['detalle']) < 15:
        _error('detalle', 'Cuentanos con detalle lo ocurrido (minimo 15 caracteres).')
    if len(normalizado['pedido']) < 5:
        _error('pedido',
               'Indica que solucion o pedido concreto esperas (minimo 5 caracteres).')

    if errores:
        raise TiendaError('VALIDACION', 'Datos invalidos.', 422, errores)
    return normalizado


# ─────────────────────────────────────────────
# ESCRITURA
# ─────────────────────────────────────────────
def crear(cur, datos):
    """Inserta la hoja y devuelve (id, token, codigo)."""
    valores = _validar(datos)
    token = secrets.token_urlsafe(24)
    fecha = datetime.now()
    fecha_limite = fecha + timedelta(days=DIAS_RESPUESTA_RECLAMO)
    cur.execute(
        """INSERT INTO reclamaciones
           (codigo, token, fecha, tipo, consumidor_nombre,
            consumidor_documento_tipo, consumidor_documento,
            consumidor_domicilio, consumidor_telefono, consumidor_correo,
            bien_tipo, bien_descripcion, bien_monto, detalle, pedido,
            estado, fecha_limite)
           VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   'pendiente',%s)""",
        (token, fecha, valores['tipo'], valores['consumidor_nombre'],
         valores['consumidor_documento_tipo'], valores['consumidor_documento'],
         valores['consumidor_domicilio'], valores['consumidor_telefono'],
         valores['consumidor_correo'], valores['bien_tipo'],
         valores['bien_descripcion'], valores['bien_monto'],
         valores['detalle'], valores['pedido'], fecha_limite),
    )
    reclamo_id = cur.lastrowid
    codigo = 'LR-%d-%06d' % (fecha.year, _entero(reclamo_id))
    cur.execute("UPDATE reclamaciones SET codigo=%s WHERE id=%s",
                (codigo, reclamo_id))
    return reclamo_id, token, codigo


# ─────────────────────────────────────────────
# LECTURA
# ─────────────────────────────────────────────
def listar(cur, correo, page=1, per_page=10):
    cur.execute(
        "SELECT COUNT(*) AS total FROM reclamaciones "
        "WHERE LOWER(consumidor_correo)=LOWER(%s)",
        (correo,),
    )
    fila = cur.fetchone() or {}
    total = _entero(fila.get('total'))
    offset = max(0, (page - 1) * per_page)
    cur.execute(
        "SELECT * FROM reclamaciones "
        "WHERE LOWER(consumidor_correo)=LOWER(%s) "
        "ORDER BY fecha DESC, id DESC LIMIT %s OFFSET %s",
        (correo, per_page, offset),
    )
    return cur.fetchall(), total


def obtener(cur, reclamo_id):
    cur.execute("SELECT * FROM reclamaciones WHERE id=%s", (_entero(reclamo_id),))
    return cur.fetchone()


def obtener_por_token(cur, token):
    cur.execute("SELECT * FROM reclamaciones WHERE token=%s", (token,))
    return cur.fetchone()


def autorizado(reclamo, token=None, correo=None):
    """True si el token de la constancia coincide o si el correo es el dueno."""
    if not reclamo:
        return False
    token_reclamo = str(reclamo.get('token') or '')
    if token and token_reclamo and hmac.compare_digest(token_reclamo, str(token)):
        return True
    correo_reclamo = (reclamo.get('consumidor_correo') or '').strip().lower()
    if correo and correo_reclamo and correo_reclamo == correo.strip().lower():
        return True
    return False
