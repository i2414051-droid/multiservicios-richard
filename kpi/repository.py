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

# Driver MySQL: mysqlclient (MySQLdb) en produccion; PyMySQL como respaldo
# cuando no hay librerias nativas (por ejemplo en Windows local).
try:
    import MySQLdb
except ImportError:  # pragma: no cover - depende del entorno
    import pymysql
    pymysql.install_as_MySQLdb()
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
# RELOJ Y ESQUEMA
# ─────────────────────────────────────────────
# El modulo KPI mide periodos, asi que la hora con la que compara tiene que ser
# LA MISMA con la que se escribieron los datos. En alwaysdata el servidor
# MySQL corre en CEST (UTC+2) mientras el contenedor de la app corre en UTC:
# comparar con datetime.now() dejaba fuera las ventas mas recientes, porque
# para Python todavia no habian ocurrido.
#
# La regla del modulo es una sola: manda el reloj de la base de datos. Las
# columnas que el KPI lee las escribe MySQL (CURRENT_TIMESTAMP) o las escribe
# este modulo con NOW(), nunca con el reloj local de Python.
def ahora_bd():
    """
    Hora de la base de datos, con el reloj con el que se escribieron los datos.

    Si la consulta falla (BD caida, sin permiso) devuelve la hora local de
    Python: es peor, pero es mejor que dejar el panel entero sin calcular.
    """
    try:
        cur = get_cursor()
        try:
            cur.execute("SELECT NOW() AS ahora")
            fila = cur.fetchone() or {}
            valor = fila.get('ahora')
            if valor:
                return valor
        finally:
            cur.close()
    except Exception:
        pass
    return datetime.datetime.now()


# Columnas que existen de verdad, cacheadas por proceso. Se usa para que una
# tabla a la que le falte una columna degrade a "sin datos" en vez de tumbar
# la seccion entera con un error 1054.
_columnas_cache = {}


def columnas_de(tabla):
    """Conjunto de nombres de columna de `tabla` en la base de la conexion."""
    if tabla not in _columnas_cache:
        try:
            cur = get_cursor()
            try:
                cur.execute(
                    """SELECT COLUMN_NAME FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""",
                    (tabla,),
                )
                _columnas_cache[tabla] = {r['COLUMN_NAME'] for r in cur.fetchall()}
            finally:
                cur.close()
        except Exception:
            _columnas_cache[tabla] = set()
    return _columnas_cache[tabla]


def limpiar_cache_columnas():
    """Olvida el cache de columnas (lo usa init_db tras crear el esquema)."""
    _columnas_cache.clear()


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


def fetch_repuestos_demanda(desde, hasta, categorias=None):
    """
    Repuestos activos con su stock actual y sus unidades vendidas del periodo.

    El cruce de unidades se hace en una subconsulta y no con un LEFT JOIN
    directo: asi un repuesto sin ventas aparece con 0 y no multiplica filas.
    """
    almacen = almacen_db()
    categorias = tuple(categorias or settings.CATEGORIA_REPUESTOS)
    if not categorias:
        return []
    marcadores = ','.join(['%s'] * len(categorias))
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.id AS id, p.nombre AS nombre,
                       COALESCE(p.stock,0) AS stock,
                       COALESCE(s.unidades,0) AS unidades
                FROM {almacen}.productos p
                LEFT JOIN ({_UNIDADES_VENDIDAS_SUB}) s ON s.pid = p.id
                WHERE p.categoria IN ({marcadores})
                  AND COALESCE(p.estado,'activo') = 'activo'""",
            (desde, hasta) + categorias,
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


def fetch_doh_categorias(desde, hasta, categorias=None):
    """
    Stock y unidades vendidas del periodo por categoria.

    Con eso el servicio calcula los dias de inventario (stock / demanda diaria).

    Devuelve un dict {categoria: {stock, unidades}} con TODAS las categorias
    pedidas: las que no existen en el catalogo vienen con stock 0 y unidades 0
    y con `existe = False`. Asi el servicio puede avisar de que la categoria
    esta mal configurada en vez de mostrar un "sin datos" sin explicacion.
    """
    almacen = almacen_db()
    categorias = tuple(categorias or (settings.CATEGORIA_ACCESORIOS
                                       + settings.CATEGORIA_REPUESTOS))
    resultado = {
        c: {'stock': 0, 'unidades': 0, 'existe': False}
        for c in categorias
    }
    if not categorias:
        return resultado

    marcadores = ','.join(['%s'] * len(categorias))
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.categoria AS categoria,
                       COALESCE(SUM(p.stock),0) AS stock,
                       COALESCE(SUM(COALESCE(s.unidades,0)),0) AS unidades
                FROM {almacen}.productos p
                LEFT JOIN ({_UNIDADES_VENDIDAS_SUB}) s ON s.pid = p.id
                WHERE COALESCE(p.estado,'activo') = 'activo'
                  AND p.categoria IN ({marcadores})
                GROUP BY p.categoria""",
            (desde, hasta) + categorias,
        )
        for row in cur.fetchall():
            categoria = (row['categoria'] or '').strip()
            if categoria in resultado:
                resultado[categoria] = {
                    'stock': _i(row['stock']),
                    'unidades': _i(row['unidades']),
                    'existe': True,
                }
    finally:
        cur.close()
    return resultado


def categorias_existentes():
    """Categorias que hay de verdad en el catalogo de productos."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(f"SELECT DISTINCT categoria FROM {almacen}.productos")
        return sorted({(r['categoria'] or '').strip() for r in cur.fetchall()})
    except Exception as exc:
        current_app.logger.warning('KPI categorias_existentes fallo: %s', exc)
        return []
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

    Las dos consultas van aisladas: `bloqueos_ip` no tiene columna de fecha en
    instalaciones viejas y, cuando falta, el error 1054 tumbo toda la seccion
    de seguridad (los dos indicadores) en lugar de solo el dato de IPs. Ahora
    cada consulta cae por separado y la que falla deja su indicador en 0.
    """
    resultado = {
        'cuentas_intentos': 0,
        'cuentas_bloqueadas': 0,
        'intentos_total': 0,
        'ips_bloqueadas': 0,
    }

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
        resultado['cuentas_intentos'] = _i(row.get('cuentas_intentos'))
        resultado['cuentas_bloqueadas'] = _i(row.get('cuentas_bloqueadas'))
        resultado['intentos_total'] = _i(row.get('intentos_total'))
    except Exception as exc:
        current_app.logger.warning('KPI seguridad (intentos) fallo: %s', exc)
    finally:
        cur.close()

    # La tabla tiene `created_at` solo si el modulo la ha anadido. Sin ella no
    # hay forma de saber cuando se bloqueo la IP: se informa 0, no un fallo.
    if 'created_at' in columnas_de('bloqueos_ip'):
        cur = get_cursor()
        try:
            cur.execute(
                """SELECT COUNT(*) AS ips
                   FROM bloqueos_ip
                   WHERE created_at >= %s AND created_at < %s""",
                (desde, hasta),
            )
            resultado['ips_bloqueadas'] = _i((cur.fetchone() or {}).get('ips'))
        except Exception as exc:
            current_app.logger.warning('KPI seguridad (IPs) fallo: %s', exc)
        finally:
            cur.close()

    return resultado


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

    `created_at` lo anade el modulo a la tabla `carrito`: sin esa columna no
    hay forma de saber cuando se armo el carrito, asi que el indicador se
    queda sin datos en vez de dar una cifra inventada.
    """
    if 'created_at' not in columnas_de('carrito'):
        current_app.logger.warning(
            'KPI carritos: la tabla `carrito` no tiene `created_at`; '
            'el indicador de abandono se queda sin datos')
        return None

    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(DISTINCT usuario_id) AS carritos
               FROM carrito
               WHERE created_at >= %s AND created_at < %s""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('carritos'))
    except Exception as exc:
        current_app.logger.warning('KPI carritos fallo: %s', exc)
        return None
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


# ─────────────────────────────────────────────
# SNAPSHOT COMPLETO EN POCAS CONSULTAS
# ─────────────────────────────────────────────
# El panel pedia los mismos datos dos veces (periodo actual y anterior) con una
# consulta por dataset: 20 idas y vueltas, ~190 ms cada una contra la BD
# remota. Estas funciones piden los dos periodos en la misma consulta y dejan
# la agrupacion a Python. El resultado es el mismo que el de las funciones de
# arriba, que se conservan como respaldo y para el resto del modulo.
def fetch_snapshot(desde, hasta, prev_desde, prev_hasta, dias):
    """Datos de los dos periodos en 4 consultas (antes: 20)."""
    ventas = _ventas_dos_periodos(desde, hasta, prev_desde, prev_hasta)
    productos = _productos_dos_periodos(desde, hasta, prev_desde, prev_hasta)
    escalares = _escalares_dos_periodos(desde, hasta, prev_desde, prev_hasta)
    incidencias = _incidencias_dos_periodos(desde, hasta, prev_desde, prev_hasta)

    dias = max(1, int(dias))
    salida = {
        'categorias': productos['categorias'],
        'ahora': escalares['ahora'],
    }

    for clave, p_desde, p_hasta in (('actual', desde, hasta),
                                    ('anterior', prev_desde, prev_hasta)):
        v = _armar_ventas(ventas[clave])
        salida[clave] = {
            'desde': p_desde,
            'hasta': p_hasta,
            'dias': dias,
            'ventas': v['ventas'],
            'cancel_motivos': v['cancel_motivos'],
            'compras_cliente': v['compras_cliente'],
            'repuestos': productos[clave]['repuestos'],
            'doh': productos[clave]['doh'],
            'categorias': productos['categorias'],
            'seguridad': escalares[clave],
            'carritos': escalares['carritos_' + clave],
            'incidencias': incidencias[clave],
        }
    return salida


def _armar_ventas(filas):
    """Reparte las filas crudas en los mismos agregados que las funciones viejas."""
    from decimal import Decimal

    validas = {'n': 0, 'monto': Decimal('0')}
    canceladas = {'n': 0, 'monto': Decimal('0')}
    motivos = {}
    compras = []
    for row in filas:
        estado = (row['estado'] or '').lower()
        monto = Decimal(str(row['total'] or 0))
        if estado == 'cancelado':
            canceladas['n'] += 1
            canceladas['monto'] += monto
            motivo = row['motivo'] or 'sin_motivo'
            motivos[motivo] = motivos.get(motivo, 0) + 1
        else:
            validas['n'] += 1
            validas['monto'] += monto
            compras.append({'cliente_id': row['cliente_id'],
                            'fecha': row['fecha']})
    # Mismo criterio de ordenacion que el SQL anterior: por cliente (NULL
    # primero, como hace MySQL en ASC) y luego por fecha.
    compras.sort(key=lambda f: (f['cliente_id'] is not None,
                               f['cliente_id'] if f['cliente_id'] is not None else 0,
                               f['fecha']))
    return {
        'ventas': {
            'validas': {'n': validas['n'],
                        'monto': round(float(validas['monto']), 2)},
            'canceladas': {'n': canceladas['n'],
                           'monto': round(float(canceladas['monto']), 2)},
            'total_n': validas['n'] + canceladas['n'],
        },
        'cancel_motivos': [
            {'motivo': motivo, 'n': n}
            for motivo, n in sorted(motivos.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        'compras_cliente': compras,
    }


def _ventas_dos_periodos(desde, hasta, prev_desde, prev_hasta):
    """Filas de venta (con su motivo de cancelacion) de los dos periodos."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT 'actual' AS periodo, v.estado AS estado,
                      v.cliente_id AS cliente_id, v.fecha AS fecha,
                      v.total AS total,
                      COALESCE(c.motivo_codigo,'sin_motivo') AS motivo
               FROM ventas v
               LEFT JOIN cancelaciones c ON c.venta_id = v.id
               WHERE v.fecha >= %s AND v.fecha < %s
               UNION ALL
               SELECT 'anterior', v.estado, v.cliente_id, v.fecha, v.total,
                      COALESCE(c.motivo_codigo,'sin_motivo')
               FROM ventas v
               LEFT JOIN cancelaciones c ON c.venta_id = v.id
               WHERE v.fecha >= %s AND v.fecha < %s""",
            (desde, hasta, prev_desde, prev_hasta),
        )
        filas = cur.fetchall()
    finally:
        cur.close()
    salida = {'actual': [], 'anterior': []}
    for row in filas:
        salida[row['periodo']].append(row)
    return salida


def _productos_dos_periodos(desde, hasta, prev_desde, prev_hasta):
    """Catalogo con stock y unidades vendidas de cada periodo, en una consulta."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.id AS id, p.nombre AS nombre,
                       p.categoria AS categoria,
                       COALESCE(p.stock,0) AS stock,
                       COALESCE(p.estado,'activo') AS estado,
                       COALESCE(s.u_actual,0) AS u_actual,
                       COALESCE(s.u_anterior,0) AS u_anterior
                FROM {almacen}.productos p
                LEFT JOIN (
                    SELECT dv.producto_id AS pid,
                           SUM(CASE WHEN v.fecha >= %s AND v.fecha < %s
                                    THEN dv.cantidad ELSE 0 END) AS u_actual,
                           SUM(CASE WHEN v.fecha >= %s AND v.fecha < %s
                                    THEN dv.cantidad ELSE 0 END) AS u_anterior
                    FROM detalle_venta dv
                    JOIN ventas v ON v.id = dv.venta_id
                    WHERE COALESCE(v.estado,'') <> 'cancelado'
                    GROUP BY dv.producto_id
                ) s ON s.pid = p.id""",
            (desde, hasta, prev_desde, prev_hasta),
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    repuestos_cfg = tuple(settings.CATEGORIA_REPUESTOS)
    repuestos_ci = {c.casefold() for c in repuestos_cfg}
    doh_cfg = tuple(settings.CATEGORIA_ACCESORIOS + settings.CATEGORIA_REPUESTOS)

    categorias = sorted({(r['categoria'] or '').strip() for r in filas})
    salida = {
        'categorias': categorias,
        'actual': {'repuestos': [], 'doh': {
            c: {'stock': 0, 'unidades': 0, 'existe': False} for c in doh_cfg}},
        'anterior': {'repuestos': [], 'doh': {
            c: {'stock': 0, 'unidades': 0, 'existe': False} for c in doh_cfg}},
    }
    for clave, campo in (('actual', 'u_actual'), ('anterior', 'u_anterior')):
        repuestos = salida[clave]['repuestos']
        doh = salida[clave]['doh']
        for r in filas:
            categoria = (r['categoria'] or '')
            activo = r['estado'] == 'activo'
            unidades = _i(r[campo])
            if activo and categoria.casefold() in repuestos_ci:
                repuestos.append({
                    'id': _i(r['id']),
                    'nombre': r['nombre'],
                    'stock': _i(r['stock']),
                    'unidades': unidades,
                })
            if activo and categoria in doh:
                doh[categoria]['stock'] += _i(r['stock'])
                doh[categoria]['unidades'] += unidades
                doh[categoria]['existe'] = True
    return salida


def _escalares_dos_periodos(desde, hasta, prev_desde, prev_hasta):
    """Seguridad, carritos y hora de la BD en una sola consulta."""
    umbral = settings.UMBRAL_BLOQUEO_LOGIN
    campos = [
        "(SELECT NOW()) AS ahora",
        "(SELECT COUNT(*) FROM intentos_usuario WHERE ultimo_fallo >= %s AND ultimo_fallo < %s) AS a_cuentas",
        "(SELECT COALESCE(SUM(CASE WHEN COALESCE(max_intentos,0) >= %s THEN 1 ELSE 0 END),0) FROM intentos_usuario WHERE ultimo_fallo >= %s AND ultimo_fallo < %s) AS a_bloqueadas",
        "(SELECT COALESCE(SUM(intentos),0) FROM intentos_usuario WHERE ultimo_fallo >= %s AND ultimo_fallo < %s) AS a_intentos",
        "(SELECT COUNT(*) FROM intentos_usuario WHERE ultimo_fallo >= %s AND ultimo_fallo < %s) AS b_cuentas",
        "(SELECT COALESCE(SUM(CASE WHEN COALESCE(max_intentos,0) >= %s THEN 1 ELSE 0 END),0) FROM intentos_usuario WHERE ultimo_fallo >= %s AND ultimo_fallo < %s) AS b_bloqueadas",
        "(SELECT COALESCE(SUM(intentos),0) FROM intentos_usuario WHERE ultimo_fallo >= %s AND ultimo_fallo < %s) AS b_intentos",
    ]
    params = [
        desde, hasta,
        umbral, desde, hasta,
        desde, hasta,
        prev_desde, prev_hasta,
        umbral, prev_desde, prev_hasta,
        prev_desde, prev_hasta,
    ]
    tiene_bloqueos = 'created_at' in columnas_de('bloqueos_ip')
    if tiene_bloqueos:
        campos += [
            "(SELECT COUNT(*) FROM bloqueos_ip WHERE created_at >= %s AND created_at < %s) AS a_ips",
            "(SELECT COUNT(*) FROM bloqueos_ip WHERE created_at >= %s AND created_at < %s) AS b_ips",
        ]
        params += [desde, hasta, prev_desde, prev_hasta]
    tiene_carrito = 'created_at' in columnas_de('carrito')
    if tiene_carrito:
        campos += [
            "(SELECT COUNT(DISTINCT usuario_id) FROM carrito WHERE created_at >= %s AND created_at < %s) AS a_carritos",
            "(SELECT COUNT(DISTINCT usuario_id) FROM carrito WHERE created_at >= %s AND created_at < %s) AS b_carritos",
        ]
        params += [desde, hasta, prev_desde, prev_hasta]

    cur = get_cursor()
    try:
        cur.execute("SELECT " + ", ".join(campos), tuple(params))
        fila = cur.fetchone() or {}
    finally:
        cur.close()

    def _seg(prefijo):
        return {
            'cuentas_intentos': _i(fila.get(prefijo + '_cuentas')),
            'cuentas_bloqueadas': _i(fila.get(prefijo + '_bloqueadas')),
            'intentos_total': _i(fila.get(prefijo + '_intentos')),
            'ips_bloqueadas': _i(fila.get(prefijo + '_ips')) if tiene_bloqueos else 0,
        }

    return {
        'ahora': fila.get('ahora') or ahora_bd(),
        'actual': _seg('a'),
        'anterior': _seg('b'),
        'carritos_actual': _i(fila.get('a_carritos')) if tiene_carrito else None,
        'carritos_anterior': _i(fila.get('b_carritos')) if tiene_carrito else None,
    }


def _incidencias_dos_periodos(desde, hasta, prev_desde, prev_hasta):
    """Incidencias agrupadas por tipo y motivo, para los dos periodos."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT 'actual' AS periodo, tipo, motivo,
                      COUNT(*) AS n,
                      COALESCE(SUM(cantidad), 0) AS unidades
               FROM kpi_incidencias
               WHERE fecha >= %s AND fecha < %s
               GROUP BY tipo, motivo
               UNION ALL
               SELECT 'anterior', tipo, motivo,
                      COUNT(*), COALESCE(SUM(cantidad), 0)
               FROM kpi_incidencias
               WHERE fecha >= %s AND fecha < %s
               GROUP BY tipo, motivo""",
            (desde, hasta, prev_desde, prev_hasta),
        )
        filas = cur.fetchall()
    finally:
        cur.close()
    salida = {'actual': [], 'anterior': []}
    for row in filas:
        salida[row['periodo']].append({
            'tipo': row['tipo'],
            'motivo': row['motivo'],
            'n': _i(row['n']),
            'unidades': _i(row['unidades']),
        })
    for clave in salida:
        salida[clave].sort(key=lambda i: (-i['n'], i['tipo'], i['motivo']))
    return salida


def insert_incidencia(tipo, motivo, cantidad=1, venta_id=None, producto_id=None,
                      referencia=None, nota=None):
    """Registra una incidencia de operacion. Devuelve el id creado."""
    def _insertar(cur):
        # La fecha la pone MySQL (NOW()), no Python: el modulo compara los
        # periodos contra el reloj de la base de datos y las dos horas de
        # desfase con alwaysdata dejaban las incidencias mas recientes fuera.
        cur.execute(
            """INSERT INTO kpi_incidencias
               (tipo, motivo, cantidad, venta_id, producto_id, referencia,
                nota, fecha)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
            (tipo, motivo, int(cantidad), venta_id, producto_id,
             (referencia or None), (nota or '')[:settings.MAX_NOTA] or None),
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
               VALUES (%s, %s, %s, %s, NOW())""",
            (ruta, tipo_error, (mensaje or '')[:settings.MAX_NOTA] or None,
             origen),
        )
        return True

    try:
        return ejecutar_escritura(_insertar)
    except Exception:
        return False
