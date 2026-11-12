"""Reglas de seguridad compartidas por la web y la API.

Este modulo concentra:
  * la politica de contrasenas,
  * la escalera de bloqueo por intentos fallidos,
  * la validacion de correo,
  * la verificacion de reCAPTCHA.

La web mantiene por ahora sus propias copias en app.py; la API usa estas.
Cuando se refactorice app.py, debera importar desde aqui y borrar las copias.
"""
import os
import re

import requests

# Dominios permitidos al crear una cuenta (misma regla que /registro).
DOMINIOS_CORREO = ('gmail.com', 'hotmail.com')

# Fallos acumulados antes de aplicar el primer bloqueo.
PRIMER_BLOQUEO_FALLO = 5
# Fallos minimos para que la cuenta aparezca en intentos_usuario.
UMBRAL_INTENTOS = 3
# Tope maximo de bloqueo: 24 horas.
BLOQUEO_TOPE_MIN = 1440

# Fallo -> minutos de castigo. Coincide con app.ESCALERA_BLOQUEO.
ESCALERA_BLOQUEO = {
    5: 15,
    6: 30,
    7: 60,
    8: 120,
    9: 240,
    10: 480,
    11: 960,
    12: 1440,
}

RECAPTCHA_VERIFY_URL = 'https://www.google.com/recaptcha/api/siteverify'

_RE_CORREO = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_RE_SOLO_LETRAS = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+$")


def minutos_bloqueo(intentos):
    """Minutos de castigo para un numero de fallo dado."""
    return min(ESCALERA_BLOQUEO.get(intentos, BLOQUEO_TOPE_MIN), BLOQUEO_TOPE_MIN)


def password_valida(password):
    """Reglas de contrasena. Devuelve (ok, mensaje)."""
    if len(password) < 8:
        return False, 'La contrasena debe tener al menos 8 caracteres.'
    if len(password.encode('utf-8')) > 72:
        return False, 'La contrasena no puede superar los 72 caracteres.'
    if not re.search(r'[A-Z]', password):
        return False, 'La contrasena debe tener al menos una letra mayuscula.'
    if not re.search(r'[a-z]', password):
        return False, 'La contrasena debe tener al menos una letra minuscula.'
    if not re.search(r'[0-9]', password):
        return False, 'La contrasena debe tener al menos un numero.'
    if not re.search(r'[^A-Za-z0-9]', password):
        return False, ('La contrasena debe tener al menos un caracter especial '
                       '(por ejemplo @, #, $, !).')
    return True, ''


def correo_valido(correo):
    """True si el correo tiene forma de correo."""
    return bool(_RE_CORREO.fullmatch(correo or ''))


def dominio_permitido(correo):
    """True si el dominio esta en la lista permitida."""
    if '@' not in (correo or ''):
        return False
    return correo.rsplit('@', 1)[-1].lower() in DOMINIOS_CORREO


def nombre_valido(nombre):
    """True si el nombre/apellido solo contiene letras y espacios."""
    return bool(_RE_SOLO_LETRAS.fullmatch(nombre or ''))


def recaptcha_configurado():
    return bool(os.environ.get('RECAPTCHA_SITE_KEY')
                and os.environ.get('RECAPTCHA_SECRET_KEY'))


def validar_recaptcha(token, ip=None):
    """Valida un token de reCAPTCHA v2. Devuelve (ok, mensaje).

    Si el captcha no esta configurado se desactiva solo, igual que en la web.
    """
    if not recaptcha_configurado():
        return True, None
    if not token:
        return False, 'Marca la casilla "No soy un robot" para continuar.'
    try:
        respuesta = requests.post(
            RECAPTCHA_VERIFY_URL,
            data={
                'secret': os.environ.get('RECAPTCHA_SECRET_KEY', ''),
                'response': token,
                'remoteip': ip or '',
            },
            timeout=int(os.environ.get('RECAPTCHA_TIMEOUT', '9')),
        )
        datos = respuesta.json()
    except Exception:
        return False, 'No se pudo verificar el captcha. Intenta de nuevo.'

    if not datos.get('success'):
        return False, 'El captcha no es valido o ya expiro. Intenta de nuevo.'

    # La app movil puede resolver el captcha en un WebView cuyo hostname no
    # coincide con el de la API. La comprobacion de hostname se deja opcional
    # para no bloquear integraciones legitimas; en produccion web conviene
    # activarla.
    if os.environ.get('RECAPTCHA_VALIDAR_HOST') == '1':
        host_esperado = os.environ.get('RECAPTCHA_HOST', '')
        if host_esperado and datos.get('hostname') != host_esperado:
            return False, 'El captcha fue resuelto en otro sitio.'
    return True, None
