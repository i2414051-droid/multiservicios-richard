"""
Esquema propio del modulo de KPIs.

El modulo se apoya en las tablas que la aplicacion ya tiene (ventas,
seguimiento_entregas, productos, ingresos...) y solo anade una: las
incidencias de operacion. Devoluciones, errores de picking, danos de
transporte, mermas y cotizaciones perdidas no se deducen de ningun sitio, hay
que registrarlas.

Ademas anade a dos tablas de la aplicacion la columna de fecha que les falta
(`carrito.created_at` y `bloqueos_ip.created_at`). Sin ella no hay forma de
saber cuando se armo un carrito ni cuando se bloqueo una IP, y los
indicadores de abandono de carrito y de bloqueos se quedan sin datos para
siempre. El DEFAULT CURRENT_TIMESTAMP hace que los INSERT que ya existian en
la aplicacion los rellenen solos, sin tocar el codigo del checkout.

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
        -- La fecha la pone MySQL (CURRENT_TIMESTAMP), no Python. El modulo
        -- compara los periodos contra el reloj de la base de datos, porque
        -- alwaysdata corre MySQL en CEST (UTC+2) y el contenedor de la app
        -- en UTC: con el reloj de Python las filas mas recientes caian fuera
        -- de la ventana y el panel mostraba cero.
        fecha      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_kpi_err_fecha (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # ── Recorrido del cliente ──
    # Una fila por intencion de compra. Nace cuando el cliente arma el
    # carrito y avanza por los pasos: carrito -> checkout -> formulario ->
    # valido -> venta. Si el formulario falla, `motivo_fallo` guarda POR QUE
    # se quedo: es la parte accionable del indicador de abandono.
    #
    # El estado 'abierta' significa que la intencion sigue viva. Cuando pasa
    # a 'convertida' se cierra; las que quedan abiertas mucho tiempo son las
    # candidatas a "carrito abandonado".
    """
    CREATE TABLE IF NOT EXISTS kpi_sesiones_compra (
        id                     INT AUTO_INCREMENT PRIMARY KEY,
        usuario_id             INT,
        -- Identificador del invitado: los que no tienen cuenta tambien
        -- compran, y sin esto sus pasos no se podrian agrupar.
        visitante              VARCHAR(64),
        -- Visita del navegador: agrupa las busquedas y la sesion de compra
        -- que pertenecen a la misma visita.
        visita_id              VARCHAR(64),
        iniciada_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        cerrada_at             DATETIME,
        estado                 VARCHAR(20) NOT NULL DEFAULT 'abierta',
        paso_alcanzado         VARCHAR(30) NOT NULL DEFAULT 'carrito',
        motivo_fallo           VARCHAR(80),
        venta_id               INT,
        -- Los deja libres la fase de recuperacion (aviso al cliente).
        recordatorio_enviado_at DATETIME,
        canal_aviso            VARCHAR(20),
        INDEX idx_kpi_ses_estado (estado, iniciada_at),
        INDEX idx_kpi_ses_usuario (usuario_id),
        INDEX idx_kpi_ses_visitante (visitante),
        INDEX idx_kpi_ses_visita (visita_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # Una fila por busqueda escrita en la tienda. `resultados` a cero significa
    # que la gente busca algo que NO tenemos: es la lista de candidatos a
    # traer. `convertido` se activa cuando esa visita acaba en compra.
    """
    CREATE TABLE IF NOT EXISTS kpi_busquedas (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        usuario_id        INT,
        visitante         VARCHAR(64),
        visita_id         VARCHAR(64),
        texto             VARCHAR(120) NOT NULL,
        resultados        INT NOT NULL DEFAULT 0,
        producto_visto_id INT,
        convertido        TINYINT NOT NULL DEFAULT 0,
        fecha             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_kpi_bus_fecha (fecha),
        INDEX idx_kpi_bus_texto (texto),
        INDEX idx_kpi_bus_visita (visita_id),
        INDEX idx_kpi_bus_usuario (usuario_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

# Columnas de fecha que el modulo necesita y que las tablas de la aplicacion
# no tienen. Se anaden con ALTER, no con CREATE: la tabla ya existe.
ALTER_STATEMENTS = (
    # Carrito abandonado: hay que saber cuando se armo el carrito.
    "ALTER TABLE carrito ADD COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE carrito ADD INDEX ix_carrito_created (created_at)",
    # Bloqueo por IP: hay que saber cuando se bloqueo.
    "ALTER TABLE bloqueos_ip ADD COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE bloqueos_ip ADD INDEX ix_bloqueos_ip_created (created_at)",
    # La fecha de la incidencia pasa a tener default del servidor.
    "ALTER TABLE kpi_incidencias MODIFY fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
)


def _aplicar_alter():
    """
    Anade las columnas que faltan. Cada ALTER es idempotente por su naturaleza
    (si la columna existe, MySQL responde error 1060 y se ignora).
    """
    for statement in ALTER_STATEMENTS:
        try:
            repository.ejecutar_escritura(lambda cur, s=statement: cur.execute(s))
        except Exception as exc:
            # 1060 = columna duplicada (ya existe): no es un problema.
            if '1060' not in str(exc) and 'Duplicate column' not in str(exc):
                log.warning('KPI schema: no se pudo aplicar "%s": %s', statement, exc)


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
    _aplicar_alter()
    # El cache de columnas ya no vale: despues de los ALTER hay columnas nuevas.
    repository.limpiar_cache_columnas()
    _esquema_listo = True


def esquema_listo() -> bool:
    return _esquema_listo
