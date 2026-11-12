"""Operaciones de autenticacion contra MySQL.

Las funciones reciben un cursor ya abierto para que la vista controle la
transaccion. No abren conexiones propias salvo que se indique.
"""
import hashlib
from datetime import datetime

# Columnas publicas del usuario. Nunca se selecciona `password` para responder.
_COLS_USUARIO = """
    id, correo, rol, nombre, apellido, telefono, documento_tipo,
    documento_numero, estado, created_at,
    COALESCE(token_version, 0) AS token_version
"""


def hash_token(token):
    """Hash estable para guardar refresh tokens sin el texto plano."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def buscar_usuario_por_correo(cur, correo):
    cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
    return cur.fetchone()


def buscar_usuario_por_id(cur, usuario_id):
    cur.execute(
        f"SELECT {_COLS_USUARIO} FROM usuarios WHERE id=%s",
        (usuario_id,),
    )
    return cur.fetchone()


def crear_usuario(cur, bcrypt, *, correo, password, nombres, apellidos,
                  documento_tipo='dni', documento_numero='', telefono=''):
    """Inserta un cliente nuevo y devuelve su id.

    Repite la regla de /registro: el carrito de una cuenta recien creada no
    debe heredar filas antiguas cuyo `usuario_id` coincida con el id nuevo.
    """
    hash_password = bcrypt.generate_password_hash(password).decode('utf-8')
    cur.execute(
        "INSERT INTO usuarios "
        "(correo, password, rol, documento_tipo, documento_numero, nombre, apellido, telefono) "
        "VALUES (%s,%s,'cliente',%s,%s,%s,%s,%s)",
        (correo, hash_password, documento_tipo, documento_numero,
         nombres, apellidos, telefono),
    )
    nuevo_id = cur.lastrowid
    cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (str(nuevo_id),))
    return nuevo_id


def actualizar_password(cur, bcrypt, usuario_id, nueva_password):
    """Cambia la clave, invalida tokens y limpia la recuperacion pendiente."""
    hash_password = bcrypt.generate_password_hash(nueva_password).decode('utf-8')
    cur.execute(
        "UPDATE usuarios SET password=%s, "
        "token_version=COALESCE(token_version,0)+1, "
        "recuperacion_token=NULL, recuperacion_expira=NULL "
        "WHERE id=%s",
        (hash_password, usuario_id),
    )


def crear_registro_refresh(cur, usuario_id, jti, token_hash, expira_en,
                           device_id=None, user_agent=None, ip=None):
    cur.execute(
        "INSERT INTO auth_refresh_tokens "
        "(usuario_id, jti, token_hash, device_id, user_agent, ip, expira_en) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (usuario_id, jti, token_hash, (device_id or '')[:120],
         (user_agent or '')[:255], (ip or '')[:45], expira_en),
    )


def buscar_refresh_por_jti(cur, jti):
    cur.execute(
        "SELECT id, usuario_id, jti, token_hash, device_id, expira_en, revocado "
        "FROM auth_refresh_tokens WHERE jti=%s",
        (jti,),
    )
    return cur.fetchone()


def revocar_refresh(cur, jti):
    cur.execute(
        "UPDATE auth_refresh_tokens SET revocado=1, revocado_en=NOW() "
        "WHERE jti=%s AND revocado=0",
        (jti,),
    )


def revocar_todos_los_refresh(cur, usuario_id):
    cur.execute(
        "UPDATE auth_refresh_tokens SET revocado=1, revocado_en=NOW() "
        "WHERE usuario_id=%s AND revocado=0",
        (usuario_id,),
    )


def limpiar_refresh_expirados(cur, usuario_id=None):
    """Borra tokens vencidos o revocados hace mas de 7 dias."""
    sql = ("DELETE FROM auth_refresh_tokens "
           "WHERE (expira_en < NOW() - INTERVAL 7 DAY "
           "OR (revocado=1 AND revocado_en < NOW() - INTERVAL 7 DAY))")
    if usuario_id is not None:
        sql += " AND usuario_id=%s"
        cur.execute(sql, (usuario_id,))
    else:
        cur.execute(sql)


def refresh_vigente(fila, ahora=None):
    """True si la fila de refresh existe, no esta revocada y no expiro."""
    if not fila:
        return False
    if int(fila.get('revocado') or 0):
        return False
    expira = fila.get('expira_en')
    if not expira:
        return False
    return expira > (ahora or datetime.now())
