"""
Capa de acceso a datos de los KPIs de ventas y almacen.

Aqui vive TODO el SQL del modulo. Dos reglas que se cumplen en todas las
funciones:

1. Las columnas DECIMAL de MySQL llegan como decimal.Decimal. Mezclar un
   Decimal con un float lanza TypeError y jsonify no sabe serializarlo, asi
   que todo el SQL convierte a float/int en la frontera con `_f` e `_i`.
2. Las marcas de tiempo las escribe SIEMPRE Python, nunca CURRENT_TIMESTAMP.
   El calculo de periodos compara contra datetime.now(); con dos relojes las
   filas mas recientes se cairian fuera de la ventana 'hasta' y el panel
   mostraria cero con datos reales.

Ninguna funcion devuelve filas crudas al front: devuelve estructuras ya
agregadas, con las claves que la vista espera aunque no haya datos.
"""
from datetime import datetime, timedelta
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
        'db': config.get('MYSQL_DB'),
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

    return MySQLdb.connect(**argumentos)


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
    modulo: app.py es quien la decide, con su regla de no caer nunca en la
    base principal, y duplicar esa regla aqui seria una fuente de errores.
    """
    return current_app.config.get('ALMACEN_DB') or current_app.config.get('MYSQL_DB')


# ─────────────────────────────────────────────
# LECTURAS — VENTAS
# ─────────────────────────────────────────────
def fetch_ventas_por_estado(desde, hasta):
    """
    Numero de ventas e importe por estado dentro del periodo.

    Los estados que usa la aplicacion son 'en espera', 'entregado' y
    'cancelado'. Se devuelve el desglose completo en vez del total para que el
    servicio pueda repartir entre ventas netas, canceladas y en espera sin
    repetir la consulta.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT estado,
                      COUNT(*) AS pedidos,
                      COALESCE(SUM(total), 0) AS monto
               FROM ventas
               WHERE fecha >= %s AND fecha < %s
               GROUP BY estado""",
            (desde, hasta),
        )
        return {
            (row['estado'] or 'sin estado'): {
                'pedidos': _i(row['pedidos']),
                'monto': round(_f(row['monto']), 2),
            }
            for row in cur.fetchall()
        }
    finally:
        cur.close()


def fetch_unidades_vendidas(desde, hasta, solo_entregadas=False):
    """
    Unidades que salieron por venta en el periodo.

    `solo_entregadas` recorta a las ventas ya marcadas como entregadas, que es
    el denominador correcto de la tasa de devoluciones: no se puede devolver
    algo que no llego.
    """
    sql = """SELECT COALESCE(SUM(d.cantidad), 0) AS unidades
             FROM detalle_venta d JOIN ventas v ON v.id = d.venta_id
             WHERE v.fecha >= %s AND v.fecha < %s"""
    if solo_entregadas:
        sql += " AND v.estado = 'entregado'"

    cur = get_cursor()
    try:
        cur.execute(sql, (desde, hasta))
        return _i((cur.fetchone() or {}).get('unidades'))
    finally:
        cur.close()


def fetch_unidades_recibidas(desde, hasta):
    """Unidades que entraron al almacen en el periodo, desde los ingresos."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT COALESCE(SUM(d.cantidad), 0) AS unidades
                FROM {almacen}.detalle_ingreso d
                JOIN {almacen}.ingresos i ON i.id = d.ingreso_id
                WHERE i.fecha >= %s AND i.fecha < %s""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('unidades'))
    finally:
        cur.close()


def fetch_ventas_por_dia(desde, hasta):
    """Serie diaria de ventas: pedidos, importe, entregadas y canceladas."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT DATE(fecha) AS dia,
                      COUNT(*) AS pedidos,
                      COALESCE(SUM(total), 0) AS monto,
                      COALESCE(SUM(estado = 'entregado'), 0) AS entregadas,
                      COALESCE(SUM(estado = 'cancelado'), 0) AS canceladas
               FROM ventas
               WHERE fecha >= %s AND fecha < %s
               GROUP BY dia ORDER BY dia ASC""",
            (desde, hasta),
        )
        return [
            {
                'dia': _iso(row['dia']),
                'pedidos': _i(row['pedidos']),
                'monto': round(_f(row['monto']), 2),
                'entregadas': _i(row['entregadas']),
                'canceladas': _i(row['canceladas']),
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def fetch_top_productos(desde, hasta, limite=8):
    """Productos con mas unidades vendidas en el periodo."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.id, p.nombre, p.categoria,
                       SUM(d.cantidad) AS unidades,
                       SUM(d.cantidad * d.precio) AS monto
                FROM detalle_venta d
                JOIN ventas v ON v.id = d.venta_id
                JOIN {almacen}.productos p ON p.id = d.producto_id
                WHERE v.fecha >= %s AND v.fecha < %s
                GROUP BY p.id, p.nombre, p.categoria
                ORDER BY unidades DESC
                LIMIT %s""",
            (desde, hasta, int(limite)),
        )
        return [
            {
                'id': _i(row['id']),
                'nombre': row['nombre'],
                'categoria': row['categoria'],
                'unidades': _i(row['unidades']),
                'monto': round(_f(row['monto']), 2),
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def _iso(valor):
    """
    DATE() devuelve un date, que no existe en Python: se pasa a texto ISO para
    que el HTML y el JSON lo traten igual sin conversiones.
    """
    return valor.isoformat() if hasattr(valor, 'isoformat') else str(valor)


# ─────────────────────────────────────────────
# LECTURAS — ENTREGAS, OTIF Y PEDIDOS PERFECTOS
# ─────────────────────────────────────────────
def fetch_entregas_cerradas(desde, hasta):
    """
    Entregas completadas en el periodo, con lo que se pidio y lo que salio.

    Devuelve una fila por entrega con tres cantidades ya resueltas en SQL
    (entregadas y pedidas) porque son subconsultas correlacionadas y meterlas
    en un JOIN multiplicaria las filas. El juicio de "a tiempo" y "completo" lo
    hace el servicio en Python: depende de la fecha estimada de cada entrega y
    meter un CASE por fila en el SQL lo haria ilegible.

    `COALESCE(fecha_entrega, created_at)` cubre las entregas marcadas como
    entregadas sin fecha: antes de existir la columna, esas filas tienen
    created_at y son entregas validas de todos modos.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT e.id AS entrega_id,
                      e.venta_id,
                      e.fecha_estimada,
                      e.created_at,
                      COALESCE(e.fecha_entrega, e.created_at) AS fecha_entrega,
                      (SELECT COALESCE(SUM(ep.cantidad), 0)
                         FROM entrega_productos ep
                        WHERE ep.entrega_id = e.id) AS entregadas,
                      (SELECT COALESCE(SUM(d.cantidad), 0)
                         FROM detalle_venta d
                        WHERE d.venta_id = e.venta_id) AS pedidas
               FROM seguimiento_entregas e
               WHERE e.estado = 'entregado'
                 AND COALESCE(e.fecha_entrega, e.created_at) >= %s
                 AND COALESCE(e.fecha_entrega, e.created_at) < %s
               ORDER BY fecha_entrega ASC""",
            (desde, hasta),
        )
        return [
            {
                'entrega_id': _i(row['entrega_id']),
                'venta_id': _i(row['venta_id']),
                'fecha_entrega': row['fecha_entrega'],
                'fecha_estimada': row['fecha_estimada'],
                'entregadas': _i(row['entregadas']),
                'pedidas': _i(row['pedidas']),
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def fetch_entregas_en_periodo(desde, hasta):
    """Entregas cerradas en el periodo, solo el conteo. Denominador de ratios."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(*) AS entregas
               FROM seguimiento_entregas
               WHERE estado = 'entregado'
                 AND COALESCE(fecha_entrega, created_at) >= %s
                 AND COALESCE(fecha_entrega, created_at) < %s""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('entregas'))
    finally:
        cur.close()


# ─────────────────────────────────────────────
# LECTURAS — CANCELACIONES Y CARRITO
# ─────────────────────────────────────────────
def fetch_pedidos_proveedor(desde, hasta):
    """
    Pedidos de reabastecimiento a proveedores por estado.

    Es lo mas cercano que tiene el sistema a "propuestas comerciales
    aceptadas": un pedido a proveedor que se cancela es un pedido perdido.
    """
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT estado, COUNT(*) AS n
                FROM {almacen}.productos_para_pedir
                WHERE fecha >= %s AND fecha < %s
                GROUP BY estado""",
            (desde, hasta),
        )
        return {
            (row['estado'] or 'pendiente'): _i(row['n'])
            for row in cur.fetchall()
        }
    finally:
        cur.close()


def fetch_carritos_abandonados(desde, hasta):
    """
    Carritos que se armaron en el periodo y nunca se convirtieron en venta.

    El carrito se borra al confirmar la compra, asi que lo que queda en la
    tabla es, por definicion, un carrito abandonado. Se cuenta por usuario y no
    por linea para que un carrito con 5 productos no pese 5 veces.

    Es una aproximacion, no la tasa real de un embudo: aqui no se registra la
    sesion de tienda ni los pasos intermedios. Mide "quien armo un pedido y no
    lo termino", que es lo que el negocio puede corregir.
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
# LECTURAS — BACKORDER E INVENTARIO
# ─────────────────────────────────────────────
def fetch_backorder():
    """
    Pedidos abiertos que el almacen no puede cumplir por falta de stock.

    No lleva periodo: un pedido sigue pendiente porque le falta mercancia hoy,
    sea cual sea el periodo que se este mirando. Por eso este indicador se
    rotula "abiertos ahora" y no lleva comparacion con el periodo anterior.

    Un pedido cuenta como backorder cuando al menos una de sus lineas apunta a
    un producto con stock fisico 0. Con stock 1 no se cuenta: se puede
    completar con el stock que hay, y el propio sistema bloquea la compra si
    no alcanza.
    """
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT COUNT(*) AS pedidos
                FROM ventas v
                WHERE v.estado = 'en espera'
                  AND EXISTS (
                      SELECT 1 FROM detalle_venta d
                        JOIN {almacen}.productos p ON p.id = d.producto_id
                       WHERE d.venta_id = v.id AND p.stock <= 0
                  )""",
        )
        con_falta = _i((cur.fetchone() or {}).get('pedidos'))

        cur.execute("SELECT COUNT(*) AS pedidos FROM ventas WHERE estado = 'en espera'")
        abiertos = _i((cur.fetchone() or {}).get('pedidos'))

        return {'abiertos': abiertos, 'sin_stock': con_falta}
    finally:
        cur.close()


def fetch_pedidos_reposicion_pendientes():
    """Lineas de reabastecimiento encoladas y pendientes de enviar."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {almacen}.productos_para_pedir "
            "WHERE estado = 'pendiente'"
        )
        return _i((cur.fetchone() or {}).get('n'))
    finally:
        cur.close()


def fetch_inventario():
    """
    Foto del almacen ahora mismo: unidades, valor y salud del stock.

    El valor se calcula como stock * precio de venta porque es el unico precio
    que el sistema tiene guardado de forma consistente. Sirve para dimensionar
    la mercancia, no como resultado contable.
    """
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT COUNT(*) AS productos,
                       COALESCE(SUM(stock), 0) AS unidades,
                       COALESCE(SUM(stock * precio), 0) AS valor,
                       COALESCE(SUM(stock = 0), 0) AS agotados,
                       COALESCE(SUM(stock BETWEEN 1 AND 5), 0) AS criticos
                FROM {almacen}.productos
                WHERE estado = 'activo'"""
        )
        row = cur.fetchone() or {}
        return {
            'productos': _i(row.get('productos')),
            'unidades': _i(row.get('unidades')),
            'valor': round(_f(row.get('valor')), 2),
            'agotados': _i(row.get('agotados')),
            'criticos': _i(row.get('criticos')),
        }
    finally:
        cur.close()


def fetch_reposiciones_sugeridas(limite=8):
    """Productos con reposicion pendiente: la cola de trabajo del almacen."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT p.id, p.nombre, p.stock, p.categoria,
                       pp.cantidad_pedido, pp.fecha, pr.nombre AS proveedor
                FROM {almacen}.productos_para_pedir pp
                JOIN {almacen}.productos p ON p.id = pp.producto_id
                LEFT JOIN {almacen}.proveedores pr ON pr.id = pp.proveedor_id
                WHERE pp.estado = 'pendiente'
                ORDER BY p.stock ASC, pp.fecha ASC
                LIMIT %s""",
            (int(limite),),
        )
        return [
            {
                'id': _i(row['id']),
                'nombre': row['nombre'],
                'stock': _i(row['stock']),
                'categoria': row['categoria'],
                'cantidad_pedido': _i(row['cantidad_pedido']),
                'fecha': row['fecha'],
                'proveedor': row['proveedor'],
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


def fetch_inventario_valor():
    """Valor total del inventario: suma de stock * precio de todos los productos activos."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"SELECT COALESCE(SUM(stock * precio), 0) AS valor_total FROM {almacen}.productos WHERE estado='activo'"
        )
        return round(_f((cur.fetchone() or {}).get('valor_total')), 2)
    finally:
        cur.close()


def fetch_stock_promedio():
    """Promedio de stock de todos los productos activos."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"SELECT COALESCE(AVG(stock), 0) AS stock_promedio FROM {almacen}.productos WHERE estado='activo'"
        )
        return _f((cur.fetchone() or {}).get('stock_promedio'))
    finally:
        cur.close()


def fetch_total_ventas_periodo(desde, hasta):
    """Numero de ventas y total de unidades vendidas en el periodo."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(*) AS total_ventas, COALESCE(SUM(d.cantidad), 0) AS total_unidades
               FROM ventas v JOIN detalle_venta d ON d.venta_id = v.id
               WHERE v.fecha >= %s AND v.fecha < %s""",
            (desde, hasta),
        )
        row = cur.fetchone() or {}
        return {'total_ventas': _i(row.get('total_ventas')), 'total_unidades': _i(row.get('total_unidades'))}
    finally:
        cur.close()


def fetch_clientes_recurrentes(desde, hasta):
    """Clientes que hicieron mas de una compra en el periodo."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(DISTINCT usuario_id) AS recurrentes
               FROM (
                   SELECT cliente_id AS usuario_id, COUNT(*) AS n
                   FROM ventas
                   WHERE fecha >= %s AND fecha < %s
                   GROUP BY cliente_id
                   HAVING COUNT(*) > 1
               ) sub""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('recurrentes', 0))
    finally:
        cur.close()


def fetch_total_clientes_unicos(desde, hasta):
    """Total de clientes unicos que compraron en el periodo."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(DISTINCT cliente_id) AS clientes
               FROM ventas
               WHERE fecha >= %s AND fecha < %s""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('clientes', 0))
    finally:
        cur.close()


def fetch_carritos_creados(desde, hasta):
    """Numero de carritos creados en el periodo."""
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT COUNT(DISTINCT usuario_id) AS carritos
               FROM carrito
               WHERE created_at >= %s AND created_at < %s""",
            (desde, hasta),
        )
        return _i((cur.fetchone() or {}).get('carritos', 0))
    finally:
        cur.close()


def fetch_margen_bruto(desde, hasta):
    """Margen bruto: suma de (precio_venta - precio_compra) * cantidad en el periodo."""
    almacen = almacen_db()
    cur = get_cursor()
    try:
        cur.execute(
            f"""SELECT COALESCE(SUM((dv.precio - COALESCE(di.precio_compra, 0)) * dv.cantidad), 0) AS margen
                FROM detalle_venta dv
                JOIN ventas v ON v.id = dv.venta_id
                LEFT JOIN {almacen}.detalle_ingreso di ON di.producto_id = dv.producto_id
                WHERE v.fecha >= %s AND v.fecha < %s""",
            (desde, hasta),
        )
        return round(_f((cur.fetchone() or {}).get('margen')), 2)
    finally:
        cur.close()


# ─────────────────────────────────────────────
# LECTURAS Y ESCRITURAS — INCIDENCIAS
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


def fetch_incidencias_por_tipo(desde, hasta):
    """
    Conteo simple por tipo, que es lo que necesitan los indicadores.

    Se separa de fetch_incidencias porque la vista quiere el desglose por
    motivo (para explicar el "por que" de las devoluciones) y los KPIs solo
    el total por tipo.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT tipo, COUNT(*) AS n,
                      COALESCE(SUM(cantidad), 0) AS unidades
               FROM kpi_incidencias
               WHERE fecha >= %s AND fecha < %s
               GROUP BY tipo""",
            (desde, hasta),
        )
        return {
            row['tipo']: {'n': _i(row['n']), 'unidades': _i(row['unidades'])}
            for row in cur.fetchall()
        }
    finally:
        cur.close()


def fetch_ventas_con_incidencia(desde, hasta):
    """
    Ventas del periodo con al menos una incidencia registrada.

    Sirve para el pedido perfecto: un pedido con una devolucion o con un error
    de picking no es un pedido perfecto, aunque llegara a tiempo y completo.
    """
    cur = get_cursor()
    try:
        cur.execute(
            """SELECT DISTINCT venta_id
               FROM kpi_incidencias
               WHERE fecha >= %s AND fecha < %s AND venta_id IS NOT NULL""",
            (desde, hasta),
        )
        return {_i(row['venta_id']) for row in cur.fetchall()}
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
    diagnosticar. Se escribe de forma sincrona porque los errores son eventos
    raros y perderlos por un corte del contenedor seria justo lo que no
    puede permitirse.
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


def purgar_errores(dias):
    """Borra la traza de errores mas antigua de `dias`."""
    corte = datetime.now() - timedelta(days=int(dias))

    def _purgar(cur):
        cur.execute("DELETE FROM kpi_error_events WHERE created_at < %s", (corte,))
        return cur.rowcount

    return ejecutar_escritura(_purgar)
