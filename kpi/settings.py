"""
Configuracion del modulo de KPIs de Ventas y Gestion de Almacen.

Los umbrales de cada indicador viven aqui y no dentro de las formulas: son la
unica parte del modulo que el negocio necesita tocar, y ajustarlos no debe
obligar a releer el calculo.

Regla del modulo: los indicadores se derivan de las tablas que la aplicacion
ya tiene (ventas, detalle_venta, seguimiento_entregas, entrega_productos,
carrito, productos, ingresos, salidas, productos_para_pedir). Solo hay una
tabla propia, `kpi_incidencias`, para lo que el sistema todavia no registra:
devoluciones, errores de picking, incidencias de transporte, mermas y
cotizaciones perdidas. Sin esa tabla, la mitad de los KPIs serian siempre
cero.
"""
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Periodo que se muestra si el administrador no pide otro.
PRESET_POR_DEFECTO = os.environ.get('KPI_PRESET', '30d')

# Dias que se conservan las metricas crudas.
METRIC_RETENTION_DAYS = _int_env('KPI_METRIC_RETENTION_DAYS', 30)

# Dias que se conservan los chequeos de salud.
HEALTH_RETENTION_DAYS = _int_env('KPI_HEALTH_RETENTION_DAYS', 7)

# Dias que se conservan los avisos de error tecnico. Son trazas de
# diagnostico del errorhandler global, no indicadores de negocio, asi que se
# pueden descartar pronto sin perder nada util.
ERROR_LOG_RETENTION_DAYS = _int_env('KPI_ERROR_LOG_RETENTION_DAYS', 30)

# Ventana de atribucion de las devoluciones. Un producto se puede devolver
# semanas despues de entregado, asi que el numerador se cuentan sobre todo el
# historial mientras el denominador se recorta al periodo elegido.
DIAS_ATRIBUCION_DEVOLUCION = _int_env('KPI_DIAS_DEVOLUCION', 30)

# Intervalo en segundos entre volcados del buffer de metricas.
FLUSH_INTERVAL_SECONDS = _int_env('KPI_FLUSH_INTERVAL_SECONDS', 30)

# Cantidad de registros en el buffer antes de forzar un volcado.
FLUSH_BATCH_SIZE = _int_env('KPI_FLUSH_BATCH_SIZE', 100)

# ─────────────────────────────────────────────
# SEMAFORO DE LOS INDICADORES
# ─────────────────────────────────────────────
# Cada entrada declara en que sentido es buena la cifra:
#
#   'sube' = True   -> mejora cuando sube. ok/aviso son minimos.
#   'sube' = False  -> mejora cuando baja.   ok/aviso son maximos.
#   ok = None       -> no hay objetivo: basta con que haya cifra (>0).
#
# Los umbrales salen de los objetivos del negocio: pedidos perfectos por encima
# del 95%, y por debajo de la mitad de cancelaciones o devoluciones para
# considerar que el proceso esta sano.
UMBRALES = {
    # 1. Exito
    'ventas_netas':        {'sube': True,  'ok': None, 'aviso': None},
    'pedidos_perfectos':   {'sube': True,  'ok': 95.0, 'aviso': 90.0},
    'otif':                {'sube': True,  'ok': 95.0, 'aviso': 90.0},
    'sell_through':        {'sube': True,  'ok': 60.0, 'aviso': 40.0},

    # 2. Cancelaciones
    'tasa_cancelacion':    {'sube': False, 'ok': 5.0,  'aviso': 10.0},
    'abandono_carrito':    {'sube': False, 'ok': 60.0, 'aviso': 80.0},
    'cancelacion_proveedor': {'sube': False, 'ok': 10.0, 'aviso': 20.0},

    # 3. Problemas
    'devoluciones':        {'sube': False, 'ok': 5.0,  'aviso': 10.0},
    'backorder':           {'sube': False, 'ok': 5.0,  'aviso': 15.0},
    'picking':             {'sube': False, 'ok': 2.0,  'aviso': 5.0},
    'mermas':              {'sube': False, 'ok': 0.0,  'aviso': 1.0},
    'transporte':          {'sube': False, 'ok': 2.0,  'aviso': 5.0},
}


def semaforo(clave: str, valor):
    """
    Traduce una cifra al semaforo de la tarjeta: ok, aviso, critico o
    sin_datos.

    `valor` es None cuando el indicador no se puede calcular, por ejemplo
    porque no hay pedidos en el periodo. Se distingue de un 0 real: un 0 de
    devoluciones es una buena noticia, y un None significa que no hay nada que
    medir.
    """
    config = UMBRALES.get(clave)
    if config is None or valor is None:
        return 'sin_datos'

    valor = float(valor)
    if config.get('ok') is None:
        return 'ok' if valor > 0 else 'sin_datos'

    if config['sube']:
        if valor >= config['ok']:
            return 'ok'
        return 'aviso' if valor >= config['aviso'] else 'critico'

    if valor <= config['ok']:
        return 'ok'
    return 'aviso' if valor <= config['aviso'] else 'critico'


def mejor_si_sube(clave: str) -> bool:
    """
    Sentido "bueno" del indicador, para pintar la flecha de variacion.

    Vive junto a los umbrales para que la tarjeta y la flecha nunca discrepen
    sobre si una subida es una mejora.
    """
    return bool(UMBRALES.get(clave, {}).get('sube', True))


# ─────────────────────────────────────────────
# INCIDENCIAS
# ─────────────────────────────────────────────
# Lo que el sistema no tiene forma de deducir de las tablas: lo registra una
# persona. Cada tipo lleva sus motivos, porque "el cliente devolvio algo" no
# sirve para decidir nada y "devolvio una talla incorrecta" si.
TIPOS_INCIDENCIA = (
    ('devolucion', 'Devolucion de cliente', 'bi-arrow-counterclockwise',
     ('producto_defectuoso', 'talla_incorrecta', 'envio_equivocado', 'otro')),
    ('picking', 'Error de picking / armado',
     'bi-box-seam',
     ('producto_equivocado', 'color_equivocado', 'cantidad_equivocada', 'otro')),
    ('transporte', 'Incidencia de transporte', 'bi-truck',
     ('producto_danado', 'direccion_incorrecta', 'paquete_perdido',
      'retraso_paquetera', 'otro')),
    ('merma', 'Merma o perdida de mercancia', 'bi-box-arrow-down',
     ('robo', 'dao_interno', 'ruptura', 'perdida_administrativa',
      'vencimiento', 'otro')),
    ('contrato_perdido', 'Cotizacion o contrato perdido', 'bi-file-earmark-x',
     ('precio', 'plazo_entrega', 'proveedor_elegido', 'sin_presupuesto',
      'otro')),
)

# indice por tipo, para no recorrer la tupla en cada lectura
MOTIVOS_POR_TIPO = {clave: motivos for clave, _, _, motivos in TIPOS_INCIDENCIA}

ETIQUETAS_TIPO = {clave: texto for clave, texto, _, _ in TIPOS_INCIDENCIA}

ICONOS_TIPO = {clave: icono for clave, _, icono, _ in TIPOS_INCIDENCIA}

# Motivos de devolucion con su lectura para el administrador. El resto de
# tipos se muestra tal cual, en snake_case, que ya es legible.
ETIQUETAS_MOTIVO = {
    'producto_defectuoso': 'Producto defectuoso',
    'talla_incorrecta': 'Talla / medida incorrecta',
    'envio_equivocado': 'Se envio otro producto',
    'producto_danado': 'Producto danado en el traslado',
    'direccion_incorrecta': 'Direccion incorrecta',
    'paquete_perdido': 'Paquete perdido',
    'retraso_paquetera': 'Retraso de la paqueteria',
    'perdida_administrativa': 'Perdida administrativa',
    'dao_interno': 'Dano interno',
    'otro': 'Otro',
}


# Alias para compatibilidad con codigo existente
RETENTION_DAYS = METRIC_RETENTION_DAYS
HEALTH_RETENTION_DAYS = HEALTH_RETENTION_DAYS
FLUSH_INTERVAL_SECONDS = FLUSH_INTERVAL_SECONDS
FLUSH_BATCH_SIZE = FLUSH_BATCH_SIZE

# Duracion maxima de la nota de una incidencia (evita filas gigantes).
MAX_NOTA = 500