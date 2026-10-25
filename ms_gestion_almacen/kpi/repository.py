"""
Capa de acceso a datos de los KPIs de negocio.

Aqui vive TODO el SQL del modulo. Dos reglas que se cumplen en todas las
funciones:

1. Las columnas DECIMAL de MySQL llegan como decimal.Decimal. Mezclar un
   Decimal con un float lanza TypeError y jsonify no sabe serializarlo, asi
   que todo el SQL convierte a float/int en la frontera con `_f` e `_i`.
2. Las marcas de tiempo las escribe SIEMPRE Python, nunca CURRENT_TIMESTAMP.
   El calculo de periodos compara contra datetime.now(); con dos relojes las
   filas mas recientes se cairian fuera de la ventana 'hasta'.

La regla de negocio transversal: una venta cancelada NO es una venta. Todo
lo que cuenta dinero o pedidos usa `COALESCE(estado,'') <> 'cancelado'`.
"""
from datetime import datetime
import threading

import MySQLdb
from MySQLdb import cursors as mysql_cursors
from flask import current_app

from . import settings


def _f(valor):
    """Convierte un valor numerico de MySQL a float."""
    return float(valor) if valor is not None else 0.0


def _i(valor):
    """Convierte un valor numerico de MySQL a int."""
    return int(valor) if valor is not None else 0


# ─────────────────────────────────────────────
# CONEXION
# ─────────────────────────────────────────────
def _abrir_conexion(config):
    """
    Abre una conexion propia con la configuracion de MySQL de la app.

    Se leen las mismas claves que usa flask_mysqldb, de modo que el modulo
    habla con el mismo servidor y las mismas bases sin que app.py tenga que
    pasarle nada.
    """
    argumentos = {
        'host': config.get('MYSQL_HOST') or 'localhost',
        'user': config.get('MYSQL_USER'),
        'passwd': config.get('MYSQL_PASSWORD'),
        # BD por defecto del modulo: la de VENTAS (principal). Se puede forzar
        # con KPI_VENTAS_DB para que el modulo viva en un micro cuya conexion
        # de flask_mysqldb apunte a otra base (p.ej. el micro de almacen).
        'db': config.get('KPI_VENTAS_DB') or config.get('MYSQL_DB'),
        'charset': config.get('MYSQL_CHARSET') or 'utf8mb4',
        'use_unicode': config.get('MYSQL_USE_UNICODE', True),
        'connect_timeout': int(config.get('MYSQL_CONNECT_TIMEOUT') or 10),
    }

    # Con socket unix no se pueden enviar host ni puerto a la vez.
    socket_unix = config.get('MYSQL_UNIX_SOCKET')
    if socket_unix:
        argumentos.pop('host')
        argumentos['unix_socket'] = socket_unix
    else:
        argumentos['port'] = int(config.get('MYSQL_PORT') or 3306)

    modo_sql = config.get('MYSQL_SQL_MODE')
    if modo_sql:
        argumentos['sql_mode'] = modo_sql

    # app.py fija 'DictCursor'; sin esto las filas volverian como tuplas y
    # todas las lecturas de este modulo, que accede por nombre de columna,
    # darian TypeError.
    nombre_cursor = config.get('MYSQL_CURSORCLASS')
    if nombre_cursor:
        argumentos['cursorclass'] = getattr(mysql_cursors, nombre_cursor)

    return MySQLdb.connect(**argumentos, autocommit=True)


# Las conexiones se cachean por hilo y por app. Por hilo porque el
# errorhandler puede escribir desde fuera de toda peticion; por app porque el
# mismo proceso puede alojar mas de una en pruebas.
_conexiones = threading.local()


def get_connection():
    """Devuelve la conexion de este hilo contra la app en curso."""
    app = current_app._get_current_object()
    cache = getattr(_conexiones, 'cache', None)
    if cache is None:
        cache = _conexiones.cache = {}

    clave = id(app)
    conexion = cache.get(clave)
    if conexion is None:
        conexion = cache[clave] = _abrir_conexion(app.config)
    return conexion


def get_cursor():
    """Devuelve un cursor sobre la conexion propia del modulo."""
    return get_connection().cursor()


def commit():
    """Confirma la transaccion en curso."""
    get_connection().commit()


def rollback():
    """Deshace la transaccion en curso tras un error."""
    try:
        get_connection().rollback()
    except Exception:
        # Si ni siquiera se puede deshacer, la conexion esta muerta: se tira.
        pass
    descartar_conexion()


def descartar_conexion():
    """
    Cierra y olvida la conexion cacheada de este hilo.

    La MySQL de produccion corta las conexiones oidas (wait_timeout) y
    reinicia el contenedor. mysqlclient sigue entregando el socket hasta que
    se le escribe, y entonces falla con 'server has gone away' para siempre:
    sin esto, un solo corte dejaria el modulo de KPIs muerto hasta el
    siguiente reinicio del proceso, sin error visible en ninguna parte.
    """
    app = current_app._get_current_object()
    cache = getattr(_conexiones, 'cache', None)
    conexion = cache.pop(id(app), None) if cache else None
    if conexion is not None:
        try:
            conexion.close()
        except Exception:
            pass


def ejecutar_escritura(operacion):
    """
    Envuelve una escritura en una transaccion con reintento de conexion.

    Si la conexion estaba caduca, la primera llamada falla, se tira la
    conexion y se reintenta una vez contra una nueva.
    """
    for intento in (1, 2):
        cur = get_cursor()
        try:
            resultado = operacion(cur)
            commit()
            return resultado
        except Exception:
            rollback()
            if intento == 2:
                raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
    return None


def almacen_db():
    """
    Nombre de la base de datos de gestion de almacen.

    Lo lee de la configuracion de la app en vez de de una constante del
    modulo: app.py es quien la decide, y duplicar esa regla aqui seria una
    fuente de errores.
    """
    return (current_app.config.get('KPI_ALMACEN_DB')
            or current_app.config.get('ALMACEN_DB')
            or current_app.config.get('MYSQL_DB'))


# ─────────────────────────────────────────────
# 1. VENTAS Y FACTURACION
# ─────────────────────────────────────────────
def fetch_ventas_periodo(desde, hasta):
    """
    Pedidos e importe del periodo, separando validas de canceladas.

    Devuelve el desglose por estado para que el servicio reparta pedidos,
    ingresos y cancelaciones sin repetir la consulta.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COALESCE(estado,'sin estado') AS estado,
                      COUNT(*) AS n,
                      COALESCE(SUM(total),0) AS monto
               FROM ventas
               WHERE fecha >= %s AND fecha < %s
               GROUP BY estado""",
            (desde, hasta),
        )
        validas = {'n': 0, 'monto': 0.0}
        canceladas = {'n': 0, 'monto': 0.0}
        for row in cur.fetchall():
            estado = (row['estado'] or '').lower()
            n, monto = _i(row['n']), round(_f(row['monto']), 2)
            if estado == 'cancelado':
                canceladas['n'] += n
                canceladas['monto'] += monto
            else:
                validas['n'] += n
                validas['monto'] += monto
        return {
            'validas': validas,
            'canceladas': canceladas,
            'total_n': validas['n'] + canceladas['n'],
        }
    finally:
        cur.close()


def fetch_cancelaciones_por_motivo(desde, hasta):
    """
    Motivos de las ventas canceladas en el periodo (KPI 3 y 4).

    El motivo no vive en `ventas`: lo escribe el flujo de cancelacion en la
    tabla `cancelaciones` (una fila por venta, UNIQUE por venta_id). Se lee con
    LEFT JOIN para que una venta cancelada sin motivo registrado no desaparezca
    del indice: sale como 'sin_motivo'.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COALESCE(c.motivo_codigo,'sin_motivo') AS motivo,
                      COUNT(*) AS n
               FROM ventas v
               LEFT JOIN cancelaciones c ON c.venta_id = v.id
               WHERE v.fecha >= %s AND v.fecha < %s
                 AND COALESCE(v.estado,'') = 'cancelado'
               GROUP BY motivo
               ORDER BY n DESC""",
            (desde, hasta),
        )
        return [{'motivo': row['motivo'], 'n': _i(row['n'])}
                for row in cur.fetchall()]
    finally:
        cur.close()


def fetch_compras_por_cliente(desde, hasta):
    """
    Fechas de compra (validas) de cada cliente en el periodo.

    Se devuelven las filas crudas ordenadas para que el servicio calcule los
    intervalos entre compras: esa aritmetica no se puede hacer en SQL sin
    variables de sesion fragiles segun la version de MySQL.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT cliente_id, fecha
               FROM ventas
               WHERE fecha >= %s AND fecha < %s
                 AND COALESCE(estado,'') <> 'cancelado'
               ORDER BY cliente_id, fecha""",
            (desde, hasta),
        )
        return [{'cliente_id': row['cliente_id'], 'fecha': row['fecha']}
                for row in cur.fetchall()]
    finally:
        cur.close()


# ─────────────────────────────────────────────
# 2. GESTION DE ALMACEN E INVENTARIO
# ─────────────────────────────────────────────
_UNIDADES_VENDIDAS_SUB = """
    SELECT dv.producto_id AS pid, SUM(dv.cantidad) AS unidades
    FROM detalle_venta dv
    JOIN ventas v ON v.id = dv.venta_id
    WHERE v.fecha >= %s AND v.fecha < %s
      AND COALESCE(v.estado,'') <> 'cancelado'
    GROUP BY dv.producto_id
"""


def fetch_repuestos_demanda(desde, hasta):
    """
    Repuestos activos con su stock actual y sus unidades vendidas del periodo.

    El cruce de unidades se hace en una subconsulta y no con un LEFT JOIN
    directo: asi un repuesto sin ventas aparece con 0 y no multiplica filas.
    """
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.id AS id, p.nombre AS nombre,
                       COALESCE(p.stock,0) AS stock,
                       COALESCE(s.unidades,0) AS unidades
                FROM {almacen}.productos p
                LEFT JOIN ({_UNIDADES_VENDIDAS_SUB}) s ON s.pid = p.id
                WHERE p.categoria = %s
                  AND COALESCE(p.estado,'activo') = 'activo'""",
            (desde, hasta, settings.CATEGORIA_REPUESTOS),
        )
        return [
            {
                'id': _i(row['id']),
                'nombre': row['nombre'],
                'stock': _i(row['stock']),
                'unidades': _i(row['unidades']),
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def fetch_doh_categorias(desde, hasta):
    """
    Stock y unidades vendidas del periodo por categoria (Accesorios, Repuestos).

    Con eso el servicio calcula los dias de inventario (stock / demanda diaria).
    """
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.categoria AS categoria,
                       COALESCE(SUM(p.stock),0) AS stock,
                       COALESCE(SUM(COALESCE(s.unidades,0)),0) AS unidades
                FROM {almacen}.productos p
                LEFT JOIN ({_UNIDADES_VENDIDAS_SUB}) s ON s.pid = p.id
                WHERE COALESCE(p.estado,'activo') = 'activo'
                  AND p.categoria IN (%s, %s)
                GROUP BY p.categoria""",
            (desde, hasta, settings.CATEGORIA_ACCESORIOS,
             settings.CATEGORIA_REPUESTOS),
        )
        return {
            (row['categoria'] or ''): {
                'stock': _i(row['stock']),
                'unidades': _i(row['unidades']),
            }
            for row in cur.fetchall()
        }
    finally:
        cur.close()


# ─────────────────────────────────────────────
# 3. SEGURIDAD Y CONTROL DE USUARIOS
# ─────────────────────────────────────────────
def fetch_seguridad(desde, hasta):
    """
    Intentos fallidos de login y bloqueos del periodo.

    `intentos_usuario` guarda una fila por correo con los fallos acumulados de
    la rafaga ('intentos') y el pico historico ('max_intentos'). Se cuentan las
    cuentas con actividad en el periodo, las que llegaron al umbral de bloqueo
    y los intentos registrados. `bloqueos_ip` cuenta los bloqueos por IP.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(*) AS cuentas_intentos,
                      COALESCE(SUM(CASE WHEN COALESCE(max_intentos,0) >= %s
                                        THEN 1 ELSE 0 END),0) AS cuentas_bloqueadas,
                      COALESCE(SUM(intentos),0) AS intentos_total
               FROM intentos_usuario
               WHERE ultimo_fallo >= %s AND ultimo_fallo < %s""",
            (settings.UMBRAL_BLOQUEO_LOGIN, desde, hasta),
        )
        row = cur.fetchone() or {}

        cur.execute(
            """SELECT COUNT(*) AS ips
               FROM bloqueos_ip
               WHERE created_at >= %s AND created_at < %s""",
            (desde, hasta),
        )
        ips = _i((cur.fetchone() or {}).get('ips'))

        return {
            'cuentas_intentos': _i(row.get('cuentas_intentos')),
            'cuentas_bloqueadas': _i(row.get('cuentas_bloqueadas')),
            'intentos_total': _i(row.get('intentos_total')),
            'ips_bloqueadas': ips,
        }
    finally:
        cur.close()


# ─────────────────────────────────────────────
# 4. E-COMMERCE Y CARRITOS
# ─────────────────────────────────────────────
def fetch_carritos_abandonados(desde, hasta):
    """
    Carritos que se armaron en el periodo y nunca se convirtieron en venta.

    El carrito se borra al confirmar la compra, asi que lo que queda en la
    tabla es, por definicion, un carrito abandonado. Se cuenta por usuario y no
    por linea para que un carrito con 5 productos no pese 5 veces.

    Es una aproximacion honesta: el sistema no registra la sesion de tienda ni
    los pasos intermedios, mide "quien armo un pedido y no lo termino".
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(DISTINCT usuario_id) AS carritos
               FROM carrito
               WHERE created_at >= %s AND created_at < %s""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('carritos'))
    finally:
        cur.close()


# ─────────────────────────────────────────────
# INCIDENCIAS (registro manual de motivos)
# ─────────────────────────────────────────────
def fetch_incidencias(desde, hasta):
    """Incidencias del periodo, agrupadas por tipo y motivo."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT tipo, motivo,
                      COUNT(*) AS n,
                      COALESCE(SUM(cantidad), 0) AS unidades
               FROM kpi_incidencias
               WHERE fecha >= %s AND fecha < %s
               GROUP BY tipo, motivo
               ORDER BY n DESC""",
            (desde, hasta),
        )
        return [
            {
                'tipo': row['tipo'],
                'motivo': row['motivo'],
                'n': _i(row['n']),
                'unidades': _i(row['unidades']),
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def fetch_incidencias_recientes(limite=10, tipos=None):
    """Ultimas incidencias registradas, para que el almacen vea la cola."""
    sql = """SELECT id, tipo, motivo, cantidad, venta_id, producto_id,
                    referencia, nota, fecha
             FROM kpi_incidencias"""
    params = []
    if tipos:
        marcadores = ','.join(['%s'] * len(tipos))
        sql += f" WHERE tipo IN ({marcadores})"
        params.extend(tipos)
    sql += " ORDER BY fecha DESC, id DESC LIMIT %s"
    params.append(int(limite))

    cur = get_cursor()
    try:
        cur.execute(sql, tuple(params))
        return [
            {
                'id': _i(row['id']),
                'tipo': row['tipo'],
                'motivo': row['motivo'],
                'cantidad': _i(row['cantidad']),
                'venta_id': _i(row['venta_id']) if row['venta_id'] else None,
                'producto_id': _i(row['producto_id']) if row['producto_id'] else None,
                'referencia': row['referencia'],
                'nota': row['nota'],
                'fecha': row['fecha'],
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def insert_incidencia(tipo, motivo, cantidad=1, venta_id=None, producto_id=None,
                      referencia=None, nota=None):
    """Registra una incidencia de operacion. Devuelve el id creado."""
    def _insertar(cur):
        cur.execute(
            """INSERT INTO kpi_incidencias
               (tipo, motivo, cantidad, venta_id, producto_id, referencia,
                nota, fecha)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (tipo, motivo, int(cantidad), venta_id, producto_id,
             (referencia or None), (nota or '')[:settings.MAX_NOTA] or None,
             datetime.now()),
        )
        return cur.lastrowid

    return ejecutar_escritura(_insertar)


def eliminar_incidencia(incidencia_id):
    """Borra una incidencia mal registrada. Devuelve True si habia fila."""
    def _borrar(cur):
        cur.execute("DELETE FROM kpi_incidencias WHERE id = %s",
                    (int(incidencia_id),))
        return cur.rowcount > 0

    return bool(ejecutar_escritura(_borrar))


# ─────────────────────────────────────────────
# TRAZA DE ERRORES
# ─────────────────────────────────────────────
def insert_error_event(ruta, tipo_error, mensaje, origen='servidor'):
    """
    Guarda una excepcion no controlada.

    No es un KPI: es la traza que el errorhandler global necesita para poder
    diagnosticar.
    """
    def _insertar(cur):
        cur.execute(
            """INSERT INTO kpi_error_events
               (route, error_type, mensaje, origen, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (ruta, tipo_error, (mensaje or '')[:settings.MAX_NOTA] or None,
             origen, datetime.now()),
        )
        return True

    try:
        return ejecutar_escritura(_insertar)
    except Exception:
        return False
