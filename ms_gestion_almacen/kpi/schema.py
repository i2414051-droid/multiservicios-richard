"""
Esquema propio del modulo de KPIs.

El modulo se apoya en las tablas que la aplicacion ya tiene (ventas,
seguimiento_entregas, productos, ingresos...) y solo anade una: las
incidencias de operacion. Devoluciones, errores de picking, danos de
transporte, mermas y cotizaciones perdidas no se deducen de ningun sitio, hay
que registrarlas.

Se mantiene aparte de repository.py porque es lo unico del modulo que crea
estructura, y conviene poder leerlo sin recorrer 600 lineas de SQL.
"""
import logging

from . import repository

log = logging.getLogger(__name__)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS kpi_incidencias (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        tipo       VARCHAR(30) NOT NULL,
        motivo     VARCHAR(40) NOT NULL DEFAULT 'otro',
        cantidad   INT NOT NULL DEFAULT 1,
        venta_id   INT,
        producto_id INT,
        referencia VARCHAR(200),
        nota       VARCHAR(500),
        -- La marca la escribe SIEMPRE Python, nunca CURRENT_TIMESTAMP: el
        -- calculo de periodos compara contra datetime.now() y con dos relojes
        -- las incidencias mas recientes se cairian fuera de la ventana.
        fecha      DATETIME NOT NULL,
        INDEX idx_kpi_inc_fecha (fecha),
        INDEX idx_kpi_inc_tipo (tipo, fecha),
        INDEX idx_kpi_inc_venta (venta_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # Traza de errores no controlados. No es un KPI: es el log que ya
    # escribia el errorhandler global, conservado por si hay que diagnosticar.
    # Se respetan los nombres de columna que ya tenia la tabla en instalaciones
    # anteriores, porque CREATE TABLE IF NOT EXISTS no altera una tabla que ya
    # existe y un nombre distinto dejaria el INSERT contra una columna inexistente.
    """
    CREATE TABLE IF NOT EXISTS kpi_error_events (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        route      VARCHAR(120),
        error_type VARCHAR(150) NOT NULL,
        mensaje    VARCHAR(500),
        origen     VARCHAR(30) NOT NULL DEFAULT 'servidor',
        created_at DATETIME NOT NULL,
        INDEX idx_kpi_err_fecha (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

_esquema_listo = False


def ensure_schema() -> None:
    """Crea las tablas propias del modulo si no existen. Es idempotente."""
    global _esquema_listo
    if _esquema_listo:
        return

    def _crear(cur):
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)

    repository.ejecutar_escritura(_crear)
    _esquema_listo = True


def esquema_listo() -> bool:
    return _esquema_listo
