"""Clientes y permisos (admin) de la API v1 — Fase 4.

Reutiliza las mismas reglas que `/clientes` y `/permisos` de la web:
estados de cliente, historial de cambios, prohibicion de auto-bloquearse y la
regla que reserva al administrador principal (`admin@mail.com`) el cambio de
estado de otros administradores y el cambio de roles.
"""
from .tienda_service import TiendaError

# Mismo catalogo que app.ESTADOS_CLIENTE.
ESTADOS_CLIENTE = {
    'activo': {'nombre': 'Activo', 'color': '#28a745'},
    'inactivo': {'nombre': 'Inactivo', 'color': '#6c757d'},
    'bloqueado': {'nombre': 'Bloqueado', 'color': '#dc3545'},
    'pendiente_verificacion': {'nombre': 'Pendiente verificacion',
                               'color': '#ffc107'},
    'moroso': {'nombre': 'Moroso', 'color': '#fd7e14'},
}
ROLES_ADMIN = ('admin', 'administrador')
ROLES_ASIGNABLES = ('admin', 'cliente')


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def estado_valido(codigo):
    return codigo in ESTADOS_CLIENTE


# ─────────────────────────────────────────────
# LECTURA
# ─────────────────────────────────────────────
def listar(cur, buscar='', filtro='todos', page=1, per_page=20):
    """Clientes paginados con el numero y monto de compras no canceladas."""
    where = ("WHERE (u.correo LIKE %s OR COALESCE(u.nombre,'') LIKE %s "
             "OR COALESCE(u.apellido,'') LIKE %s)")
    params = [f'%{buscar}%'] * 3
    if estado_valido(filtro):
        where += " AND COALESCE(u.estado,'activo')=%s"
        params.append(filtro)

    cur.execute(f"SELECT COUNT(*) AS total FROM usuarios u {where}",
                tuple(params))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"""SELECT u.id, u.correo, u.nombre, u.apellido, u.telefono, u.rol,
                   COALESCE(u.estado,'activo') AS estado, u.created_at,
                   (SELECT COUNT(*) FROM ventas v WHERE v.cliente_id=u.id
                      AND COALESCE(v.estado,'') <> 'cancelado') AS num_compras,
                   (SELECT COALESCE(SUM(v2.total),0) FROM ventas v2
                      WHERE v2.cliente_id=u.id
                      AND COALESCE(v2.estado,'') <> 'cancelado') AS total_compras
            FROM usuarios u {where}
            ORDER BY u.created_at DESC LIMIT %s OFFSET %s""",
        tuple(params) + (per_page, offset),
    )
    filas = cur.fetchall()

    conteos = {}
    try:
        cur.execute(
            "SELECT COALESCE(estado,'activo') AS e, COUNT(*) AS n "
            "FROM usuarios GROUP BY COALESCE(estado,'activo')")
        for fila in cur.fetchall():
            conteos[fila['e']] = fila['n']
    except Exception:
        pass
    return filas, total, conteos


def obtener(cur, user_id):
    cur.execute(
        """SELECT u.id, u.correo, u.nombre, u.apellido, u.telefono, u.rol,
                  u.documento_tipo, u.documento_numero,
                  COALESCE(u.estado,'activo') AS estado, u.created_at,
                  (SELECT COUNT(*) FROM ventas v WHERE v.cliente_id=u.id
                     AND COALESCE(v.estado,'') <> 'cancelado') AS num_compras,
                  (SELECT COALESCE(SUM(v2.total),0) FROM ventas v2
                     WHERE v2.cliente_id=u.id
                     AND COALESCE(v2.estado,'') <> 'cancelado') AS total_compras
           FROM usuarios u WHERE u.id=%s""",
        (_entero(user_id),),
    )
    return cur.fetchone()


def historial(cur, user_id):
    try:
        cur.execute(
            """SELECT h.id, h.estado_anterior, h.estado_nuevo, h.motivo,
                      h.created_at, a.correo AS admin_correo
               FROM historial_estado_cliente h
               LEFT JOIN usuarios a ON h.admin_id=a.id
               WHERE h.usuario_id=%s ORDER BY h.created_at DESC LIMIT 50""",
            (_entero(user_id),),
        )
        return cur.fetchall()
    except Exception:
        return []


def listar_usuarios(cur, buscar='', page=1, per_page=20):
    cur.execute("SELECT COUNT(*) AS total FROM usuarios WHERE correo LIKE %s",
                (f'%{buscar}%',))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        """SELECT id, correo, rol, COALESCE(estado,'activo') AS estado
           FROM usuarios WHERE correo LIKE %s
           ORDER BY CASE rol WHEN 'admin' THEN 0 WHEN 'administrador' THEN 1
                             ELSE 2 END, correo ASC
           LIMIT %s OFFSET %s""",
        (f'%{buscar}%', per_page, offset),
    )
    return cur.fetchall(), total


# ─────────────────────────────────────────────
# ESCRITURA
# ─────────────────────────────────────────────
def cambiar_estado(cur, user_id, nuevo, motivo, admin_id, es_superadmin,
                   es_mismo_usuario):
    if not estado_valido(nuevo):
        raise TiendaError('ESTADO_INVALIDO', 'Estado no valido.', 422,
                          {'campo': 'estado'})
    if es_mismo_usuario and nuevo in ('bloqueado', 'inactivo'):
        raise TiendaError('AUTO_BLOQUEO', 'No puedes bloquearte a ti mismo.',
                          409)

    uid = _entero(user_id)
    cur.execute(
        "SELECT id, correo, rol, COALESCE(estado,'activo') AS estado "
        "FROM usuarios WHERE id=%s", (uid,))
    objetivo = cur.fetchone()
    if not objetivo:
        raise TiendaError('CLIENTE_NO_ENCONTRADO', 'Cliente no encontrado.', 404)

    if (objetivo.get('rol') or '').lower() in ROLES_ADMIN and not es_superadmin:
        raise TiendaError(
            'SIN_PERMISO',
            'Solo el administrador principal puede cambiar el estado de otro '
            'administrador.', 403)

    anterior = objetivo.get('estado') or 'activo'
    if anterior == nuevo:
        return {'cambiado': False, 'anterior': anterior, 'estado': nuevo,
                'correo': objetivo.get('correo') or ''}

    cur.execute("UPDATE usuarios SET estado=%s WHERE id=%s", (nuevo, uid))
    try:
        cur.execute(
            "INSERT INTO historial_estado_cliente "
            "(usuario_id, estado_anterior, estado_nuevo, motivo, admin_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            (uid, anterior, nuevo, (motivo or None), _entero(admin_id)),
        )
    except Exception:
        pass
    return {'cambiado': True, 'anterior': anterior, 'estado': nuevo,
            'correo': objetivo.get('correo') or ''}


def cambiar_rol(cur, user_id, rol, es_superadmin):
    if not es_superadmin:
        raise TiendaError(
            'SIN_PERMISO',
            'Solo el administrador principal puede cambiar roles.', 403)
    rol = (rol or '').strip().lower()
    if rol not in ROLES_ASIGNABLES:
        raise TiendaError('ROL_INVALIDO',
                          "El rol debe ser 'admin' o 'cliente'.", 422,
                          {'campo': 'rol'})

    uid = _entero(user_id)
    cur.execute("SELECT id FROM usuarios WHERE id=%s", (uid,))
    if not cur.fetchone():
        raise TiendaError('USUARIO_NO_ENCONTRADO', 'El usuario no existe.', 404)
    cur.execute("UPDATE usuarios SET rol=%s WHERE id=%s", (rol, uid))
    return rol
