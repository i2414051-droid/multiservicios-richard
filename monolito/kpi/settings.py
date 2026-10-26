"""
Configuracion del modulo de KPIs de negocio.

Los umbrales de cada indicador viven aqui y no dentro de las formulas: son la
unica parte del modulo que el negocio necesita tocar para mover un objetivo.

Los indicadores se derivan de las tablas que la aplicacion ya tiene (ventas,
detalle_venta, carrito, usuarios, intentos_usuario, bloqueos_ip, productos).
Solo hay una tabla propia, `kpi_incidencias`, para lo que el sistema todavia
no registra solo: devoluciones, errores de picking, incidencias de transporte,
mermas, cotizaciones perdidas y el motivo del abandono de carrito.
"""
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _categorias_env(name: str, defecto: str) -> tuple:
    """
    Categorias de producto que el KPI debe considerar, como tupla.

    Acepta varias separadas por coma: los KPIs de inventario miran a una
    categoria cada vez, pero el catalogo real de la tienda puede agrupar
    "Repuestos tecnicos" en mas de una etiqueta. Con una sola categoria fija
    el indicador se quedaba vacio para siempre sin explicar por que.
    """
    bruto = os.environ.get(name, defecto) or defecto
    return tuple(c.strip() for c in bruto.split(',') if c.strip())


# Periodo que se muestra si el administrador no pide otro.
PRESET_POR_DEFECTO = os.environ.get('KPI_PRESET', '30d')

# Ciclo de mantenimiento: cada cuanto "deberia" volver un cliente a comprar.
# Se usa para medir la frecuencia de compra (KPI de Ventas y Facturacion).
CICLO_MANTENIMIENTO_DIAS = _int_env('KPI_CICLO_MANTENIMIENTO_DIAS', 90)

# Categoria de productos considerada "repuestos tecnicos" y accesorios.
# Se pueden indicar varias separadas por coma (KPI_CATEGORIA_REPUESTOS).
CATEGORIA_REPUESTOS = _categorias_env('KPI_CATEGORIA_REPUESTOS', 'Repuestos')
CATEGORIA_ACCESORIOS = _categorias_env('KPI_CATEGORIA_ACCESORIOS', 'Accesorios')

# Umbral de intentos que la app considera bloqueo (PRIMER_BLOQUEO_FALLO).
UMBRAL_BLOQUEO_LOGIN = _int_env('KPI_UMBRAL_BLOQUEO_LOGIN', 5)

# ─────────────────────────────────────────────
# SEMAFORO DE LOS INDICADORES
# ─────────────────────────────────────────────
# Cada entrada declara en que sentido es buena la cifra:
#
#   'sube' = True   -> mejora cuando sube. ok/aviso son minimos.
#   'sube' = False  -> mejora cuando baja.   ok/aviso son maximos.
#   ok = None       -> no hay objetivo: basta con que haya cifra (>0).
UMBRALES = {
    # 1. Ventas y Facturacion
    'volumen_ventas':            {'sube': True,  'ok': None, 'aviso': None},
    'ingresos_brutos':           {'sube': True,  'ok': None, 'aviso': None},
    'cancelacion_error_cliente': {'sube': False, 'ok': 3.0,  'aviso': 6.0},
    'frecuencia_compra':         {'sube': True,  'ok': 60.0, 'aviso': 40.0},

    # 2. Gestion de Almacen e Inventario
    'quiebre_repuestos':         {'sube': False, 'ok': 5.0,  'aviso': 15.0},
    'doh_accesorios':            {'sube': False, 'ok': 30.0, 'aviso': 60.0},
    'doh_repuestos':             {'sube': False, 'ok': 30.0, 'aviso': 60.0},
    'devoluciones_incompat':     {'sube': False, 'ok': 2.0,  'aviso': 5.0},

    # 3. Seguridad y Control de Usuarios
    'bloqueo_cuentas':           {'sube': False, 'ok': 5.0,  'aviso': 10.0},
    'intentos_fallidos':         {'sube': False, 'ok': 20.0, 'aviso': 50.0},

    # 4. Comercio Electronico y Carritos
    'abandono_carrito':          {'sube': False, 'ok': 60.0, 'aviso': 80.0},
}


def semaforo(clave: str, valor):
    """
    Traduce una cifra al semaforo de la tarjeta: ok, aviso, critico o
    sin_datos.

    `valor` es None cuando el indicador no se puede calcular, por ejemplo
    porque no hay pedidos en el periodo. Se distingue de un 0 real.
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
    """Sentido "bueno" del indicador, para pintar la flecha de variacion."""
    return bool(UMBRALES.get(clave, {}).get('sube', True))


# ─────────────────────────────────────────────
# MOTIVOS DE CANCELACION (KPI 3 y 4)
# ─────────────────────────────────────────────
# El catalogo real vive en la tabla `cancelaciones` de la base de VENTAS, que
# llena el flujo de cancelacion (cliente o admin). Estas etiquetas son solo
# para pintar el indice; si el codigo no esta aqui se muestra tal cual.
MOTIVOS_CANCELACION = (
    ('cambio_opinion', 'Cambio de opinion'),
    ('mejor_precio', 'Encontro mejor precio'),
    ('demora_entrega', 'Demora en la entrega'),
    ('error_pedido', 'Error en el pedido'),
    ('producto_equivocado', 'Producto equivocado'),
    ('otro', 'Otro'),
)

ETIQUETAS_CANCELACION = {clave: texto for clave, texto in MOTIVOS_CANCELACION}

# Motivo que alimenta "Tasa de Cancelacion por Error del Cliente": el pedido se
# cayo por un error al armarlo, que es lo que el cliente puede haber cometido.
MOTIVO_CANCELACION_CLIENTE = 'error_pedido'


# ─────────────────────────────────────────────
# INCIDENCIAS
# ─────────────────────────────────────────────
# Lo que el sistema no tiene forma de deducir de las tablas: lo registra una
# persona. Cada tipo lleva sus motivos, porque "el cliente devolvio algo" no
# sirve para decidir nada y "devolvio por incompatibilidad" si.
TIPOS_INCIDENCIA = (
    ('devolucion', 'Devolucion de cliente', 'bi-arrow-counterclockwise',
     ('incompatibilidad', 'producto_defectuoso', 'envio_equivocado', 'otro')),
    ('picking', 'Error de picking / armado',
     'bi-box-seam',
     ('producto_equivocado', 'cantidad_equivocada', 'otro')),
    ('transporte', 'Incidencia de transporte', 'bi-truck',
     ('producto_danado', 'direccion_incorrecta', 'paquete_perdido',
      'retraso_paquetera', 'otro')),
    ('merma', 'Merma o perdida de mercancia', 'bi-box-arrow-down',
     ('robo', 'dano_interno', 'ruptura', 'vencimiento', 'otro')),
    ('contrato_perdido', 'Cotizacion o contrato perdido', 'bi-file-earmark-x',
     ('precio', 'plazo_entrega', 'proveedor_elegido', 'otro')),
    ('abandono_carrito', 'Motivo de abandono de carrito', 'bi-cart-x',
     ('incertidumbre', 'precio', 'envio', 'proceso_largo', 'otro')),
)

# indice por tipo, para no recorrer la tupla en cada lectura
MOTIVOS_POR_TIPO = {clave: motivos for clave, _, _, motivos in TIPOS_INCIDENCIA}

ETIQUETAS_TIPO = {clave: texto for clave, texto, _, _ in TIPOS_INCIDENCIA}

ICONOS_TIPO = {clave: icono for clave, _, icono, _ in TIPOS_INCIDENCIA}

# Motivos con su lectura para el administrador. El resto se muestra tal cual,
# en snake_case, que ya es legible.
ETIQUETAS_MOTIVO = {
    'incompatibilidad': 'Incompatibilidad con el equipo / repuesto',
    'producto_defectuoso': 'Producto defectuoso',
    'envio_equivocado': 'Se envio otro producto',
    'producto_equivocado': 'Producto equivocado',
    'cantidad_equivocada': 'Cantidad equivocada',
    'producto_danado': 'Producto danado en el traslado',
    'direccion_incorrecta': 'Direccion incorrecta',
    'paquete_perdido': 'Paquete perdido',
    'retraso_paquetera': 'Retraso de la paqueteria',
    'dano_interno': 'Dano interno',
    'incertidumbre': 'Incertidumbre / dudas tecnicas',
    'precio': 'Precio',
    'envio': 'Costo o tiempo de envio',
    'proceso_largo': 'Proceso de pago largo',
    'otro': 'Otro',
}

# Motivo de devolucion que alimenta su propio KPI.
MOTIVO_DEVOLUCION_INCOMPATIBILIDAD = 'incompatibilidad'

# Duracion maxima de la nota de una incidencia (evita filas gigantes).
MAX_NOTA = 500
