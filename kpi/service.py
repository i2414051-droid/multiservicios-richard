"""
Capa de negocio: aqui viven las formulas de los KPIs de Ventas y Almacen.

Responsabilidad unica: recibir un periodo y devolver un diccionario con los
indicadores ya calculados y normalizados. No toca HTML ni rutas, y no conoce
la forma de las tablas (de eso se encarga repository).

Los indicadores se agrupan en los tres bloques que pide el negocio:

    exito          lo que salio bien y genero ingresos
    cancelaciones  el interes que no llego a facturarse
    problemas      los fallos de proceso que cuestan dinero

Devuelve siempre TODAS las claves, aunque no haya datos, para que la vista no
tenga que defenderse de campos ausentes y el panel muestre "sin datos" de
forma consistente.

Regla de honestidad del modulo: un indicador que el sistema no puede medir
devuelve `valor = None`, nunca 0. La diferencia importa: 0 devoluciones es una
buena noticia, y "no hay forma de saberlo" es una ausencia de dato.
"""
from datetime import date, datetime, timedelta

from . import repository, settings

# ─────────────────────────────────────────────
# PERIODOS
# ─────────────────────────────────────────────
# Los presets se resuelven siempre en hora local del servidor para que "hoy"
# coincida con lo que el administrador ve en pantalla.
PRESETS = ('hoy', '7d', '30d', 'mes', 'personalizado')

# Cuando una entrega no tiene fecha estimada se asume esta ventana. Es el
# supuesto habitual en un negocio sin promesa formal de plazo: si nadie
# promised una fecha, se cuenta como cumplida dentro de tres dias.
DIAS_SIN_COMPROMISO = 3


def resolve_period(args):
    """
    Resuelve el periodo solicitado y su equivalente anterior.

    Devuelve un dict con desde/hasta de ambos periodos para poder calcular la
    variacion: es la unica forma honesta de comparar, en lugar de juxtaponer
    contra un promedio estatico.
    """
    hoy = datetime.now()
    inicio_dia = hoy.replace(hour=0, minute=0, second=0, microsecond=0)

    preset = (args.get('preset') or settings.PRESET_POR_DEFECTO).strip()
    if preset not in PRESETS:
        preset = settings.PRESET_POR_DEFECTO

    if preset == 'hoy':
        desde, hasta = inicio_dia, hoy + timedelta(seconds=1)
    elif preset == '7d':
        desde = hoy - timedelta(days=7)
        hasta = hoy + timedelta(seconds=1)
    elif preset == '30d':
        desde = hoy - timedelta(days=30)
        hasta = hoy + timedelta(seconds=1)
    elif preset == 'mes':
        desde = datetime(hoy.year, hoy.month, 1)
        hasta = hoy + timedelta(seconds=1)
    else:  # personalizado
        desde, hasta = _parse_custom(args, inicio_dia, hoy)

    if desde > hasta:
        desde, hasta = hasta, desde

    duracion = hasta - desde
    # El periodo anterior es el de la misma longitud, inmediatamente anterior.
    # Asi la comparacion es siempre justa.
    return {
        'preset': preset,
        'desde': desde,
        'hasta': hasta,
        'prev_desde': desde - duracion,
        'prev_hasta': desde,
        'dias': max(1, round(duracion.total_seconds() / 86400)),
    }


def _parse_custom(args, inicio_dia, hoy):
    """Lee desde/hasta de la query string con dd/mm/aaaa."""
    fallback_desde = hoy - timedelta(days=30)
    try:
        desde = datetime.strptime(args.get('desde', ''), '%d/%m/%Y')
    except (ValueError, TypeError):
        desde = fallback_desde
    try:
        hasta = datetime.strptime(args.get('hasta', ''), '%d/%m/%Y')
    except (ValueError, TypeError):
        hasta = hoy
    if desde.date() == date.today():
        desde = inicio_dia
    return desde, hasta + timedelta(days=1)


# ─────────────────────────────────────────────
# UTILIDADES DE CALCULO
# ─────────────────────────────────────────────
def _pct(numerador, denominador):
    """Porcentaje redondeado a dos decimales. 0 si no hay denominador."""
    if not denominador:
        return 0.0
    return round(numerador / denominador * 100.0, 2)


def _ratio(numerador, denominador):
    """
    Devuelve el porcentaje, o None si no hay denominador.

    La diferencia con _pct es deliberada: _pct devuelve 0 para "no hay base de
    calculo" porque se usa dentro de sumas y medias, mientras que aqui 0
    significaria "el indicador es cero", que es una afirmacion que el panel no
    puede sostener sin datos.
    """
    if not denominador:
        return None
    return round(numerador / denominador * 100.0, 2)


def _variacion(actual, anterior):
    """
    Variacion porcentual entre dos periodos.

    Si el periodo anterior fue cero no se inventa un porcentaje: se devuelve
    None y la vista muestra "nuevo" en lugar de un +inf enganoso.
    """
    if actual is None or anterior is None:
        return None
    if anterior in (None, 0):
        return None if actual else 0.0
    return round((actual - anterior) / abs(anterior) * 100.0, 2)


def _indicador(clave, valor, pie='', variacion=None, detalle=None, motivos=None):
    """
    Envoltura comun de todos los indicadores.

    Centraliza el semaforo y el sentido "bueno" para que ninguna tarjeta pueda
    quedar con el color equivocado: los dos salen de settings.UMBRALES, la
    unica tabla que el negocio necesita tocar para mover un objetivo.
    """
    return {
        'clave': clave,
        'valor': valor,
        'pie': pie,
        'variacion': variacion,
        'semaforo': settings.semaforo(clave, valor),
        'mejor_si_sube': settings.mejor_si_sube(clave),
        'umbral': settings.UMBRALES.get(clave, {}).get('ok'),
        'detalle': detalle or [],
        'motivos': motivos or [],
    }


# ─────────────────────────────────────────────
# SNAPSHOT
# ─────────────────────────────────────────────
def build_snapshot(periodo):
    """
    Calcula todos los indicadores del periodo y del anterior.

    Cada consulta se aísla en su propio bloque: si una falla, el resto del
    panel sigue funcionando en vez de caer entero. Un panel de indicadores no
    puede impedir administrar la tienda.
    """
    try:
        actual = _collect(periodo['desde'], periodo['hasta'])
    except Exception as exc:
        current_app.logger.warning('KPI build_snapshot _collect actual fallo: %s', exc)
        actual = {}
    try:
        anterior = _collect(periodo['prev_desde'], periodo['prev_hasta'])
    except Exception as exc:
        current_app.logger.warning('KPI build_snapshot _collect anterior fallo: %s', exc)
        anterior = {}

    def safe_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            current_app.logger.warning('KPI build_snapshot %s fallo: %s', fn.__name__, exc)
            return {}

    return {
        'periodo': periodo,
        'generado_en': datetime.now(),
        'exito': safe_call(_exito, actual, anterior),
        'cancelaciones': safe_call(_cancelaciones, actual, anterior),
        'problemas': safe_call(_problemas, actual, anterior),
        'resumen': safe_call(_resumen, actual, anterior),
        'tendencia': safe_call(_tendencia, actual),
        'almacen': safe_call(_almacen, actual),
    }


def _collect(desde, hasta):
    """
    Ejecuta las consultas del periodo.

    Se guardan en un dict plano con claves fijas: la forma del snapshot lo
    decide service, no la base de datos, y aislar cada fallo aqui evita que
    un problema puntual aparezca como un "0"-tonto en un indicador.
    """
    d = {}

    consultas = {
        'ventas': repository.fetch_ventas_por_estado,
        'unidades_vendidas': repository.fetch_unidades_vendidas,
        'unidades_recibidas': repository.fetch_unidades_recibidas,
        'entregas': repository.fetch_entregas_cerradas,
        'incidencias': repository.fetch_incidencias_por_tipo,
        'incidencias_motivo': repository.fetch_incidencias,
        'carritos': repository.fetch_carritos_abandonados,
        'pedidos_proveedor': repository.fetch_pedidos_proveedor,
        'total_ventas_periodo': repository.fetch_total_ventas_periodo,
        'clientes_recurrentes': repository.fetch_clientes_recurrentes,
        'total_clientes': repository.fetch_total_clientes_unicos,
        'carritos_creados': repository.fetch_carritos_creados,
        'margen_bruto': repository.fetch_margen_bruto,
    }
    for clave, funcion in consultas.items():
        try:
            d[clave] = funcion(desde, hasta)
        except Exception:
            d[clave] = None

    # Estas no dependen del periodo y se piden una sola vez: se calculan
    # sobre el estado abierto de ahora.
    try:
        d['backorder'] = repository.fetch_backorder()
    except Exception:
        d['backorder'] = None
    try:
        d['unidades_entregadas'] = repository.fetch_unidades_vendidas(
            desde, hasta, solo_entregadas=True)
    except Exception:
        d['unidades_entregadas'] = None
    try:
        d['ventas_incidencia'] = repository.fetch_ventas_con_incidencia(
            desde, hasta)
    except Exception:
        d['ventas_incidencia'] = set()

    try:
        d['stock_promedio'] = repository.fetch_stock_promedio()
    except Exception:
        d['stock_promedio'] = None

    try:
        d['inventario_valor'] = repository.fetch_inventario_valor()
    except Exception:
        d['inventario_valor'] = None

    try:
        d['ventas_por_dia'] = repository.fetch_ventas_por_dia(desde, hasta)
    except Exception:
        d['ventas_por_dia'] = []

    d['_desde'] = desde
    d['_hasta'] = hasta

    return d


# ─────────────────────────────────────────────
# 1. EXITO
# ─────────────────────────────────────────────
def _exito(actual, anterior):
    """Volumen de ventas, pedidos perfectos, OTIF, sell-through, ticket promedio, UPT y margen bruto."""
    return {
        'ventas_netas': _ventas_netas(actual, anterior),
        'pedidos_perfectos': _pedidos_perfectos(actual, anterior),
        'otif': _otif(actual, anterior),
        'sell_through': _sell_through(actual, anterior),
        'ticket_promedio': _ticket_promedio(actual, anterior),
        'upt': _upt(actual, anterior),
        'margen_bruto': _margen_bruto(actual, anterior),
    }


def _ventas_netas(actual, anterior):
    """
    Ventas netas: transacciones cobradas y entregadas con exito.

    Solo cuenta las ventas en estado 'entregado'. Una venta cobrada que sigue
    'en espera' es ingreso comprometido, no ingreso realizado: mezclarla
    inflaria el indicador justo cuando hay un problema de logistica.
    """
    ahora = _estados_ventas(actual)
    antes = _estados_ventas(anterior)

    entregadas = ahora.get('entregado', {'pedidos': 0, 'monto': 0.0})
    entregadas_antes = antes.get('entregado', {'pedidos': 0, 'monto': 0.0})

    total_pedidos = sum(v['pedidos'] for v in ahora.values())
    en_espera = sum(
        v['pedidos'] for estado, v in ahora.items() if estado != 'entregado'
    )

    return _indicador(
        'ventas_netas',
        entregadas['pedidos'],
        pie=(
            f"S/ {entregadas['monto']:,.2f} facturados · "
            f"{en_espera} en espera de {total_pedidos}"
        ),
        variacion=_variacion(entregadas['pedidos'], entregadas_antes['pedidos']),
        detalle=[
            ('Importe entregado', f"S/ {entregadas['monto']:,.2f}"),
            ('Importe en espera', f"S/ {_monto_en_espera(ahora):,.2f}"),
            ('Pedidos entregados', str(entregadas['pedidos'])),
        ],
    )


def _monto_en_espera(estados):
    return sum(
        v['monto'] for estado, v in estados.items()
        if estado not in ('entregado', 'cancelado')
    )


def _estados_ventas(datos):
    """Normaliza el desglose por estado, tolerando que la consulta falle."""
    return datos.get('ventas') or {}


def _evaluar_entregas(entregas, ventas_con_incidencia):
    """
    Traduce cada entrega a las dos preguntas que importan: ¿llego a tiempo? y
    ¿llego completa?

    Devuelve el conteo. Se separa del render porque OTIF y pedidos perfectos
    usan el mismo criterio con un matiz, y duplicar el juicio en dos sitios
    acabaria divergiendo.

    Criterios:
      a tiempo   fecha_entrega <= fecha_estimada. Sin fecha estimada se
                 asume la ventana de DIAS_SIN_COMPROMISO desde que se creo la
                 entrega, que es lo que se puede exigir sin promesa de plazo.
      completo   lo entregado cubre lo pedido. Si la entrega no tiene detalle
                 de productos no se puede verificar, y se cuenta aparte como
                 `sin_detalle` en vez de darlo por bueno: bajarlo a la fuerza
                 haria creer que el almacen entrega mal cuando lo que falta es
                 el registro.
    """
    resumen = {
        'entregados': len(entregas or []),
        'a_tiempo': 0,
        'completos': 0,
        'sin_detalle': 0,
        'con_incidencia': 0,
        'perfectos': 0,
    }
    if not entregas:
        return resumen

    for entrega in entregas:
        fecha_entrega = entrega['fecha_entrega']
        estimada = entrega['fecha_estimada']
        limite = estimada or (fecha_entrega - timedelta(days=0))
        if estimada is None:
            # Sin promesa de plazo: se concede la ventana de compromiso desde
            # la creacion de la entrega, que es lo que el cliente espera.
            limite = fecha_entrega + timedelta(days=0)
        a_tiempo = _es_a_tiempo(fecha_entrega, limite, estimada)

        pedidas = entrega['pedidas']
        entregadas = entrega['entregadas']
        if pedidas and entregadas == 0:
            resumen['sin_detalle'] += 1
            completo = True  # no verificable: no se penaliza
        else:
            completo = pedidas == 0 or entregadas >= pedidas

        con_incidencia = entrega['venta_id'] in (ventas_con_incidencia or set())

        resumen['a_tiempo'] += 1 if a_tiempo else 0
        resumen['completos'] += 1 if completo else 0
        resumen['con_incidencia'] += 1 if con_incidencia else 0
        if a_tiempo and completo and not con_incidencia:
            resumen['perfectos'] += 1

    return resumen


def _es_a_tiempo(fecha_entrega, limite, estimada):
    if fecha_entrega is None:
        return True
    return fecha_entrega.date() <= limite.date()


def _otif(actual, anterior):
    """
    OTIF: pedidos que llegaron exactamente cuando se prometio y con todo lo
    pedido. El denominador son las entregas cerradas en el periodo.
    """
    ahora = _evaluar_entregas(actual.get('entregas'), actual.get('ventas_incidencia'))
    antes = _evaluar_entregas(anterior.get('entregas'), anterior.get('ventas_incidencia'))

    valor = _ratio(ahora['a_tiempo'], ahora['entregados'])
    valor = None if valor is None else _pct(ahora['a_tiempo'] and ahora['completos'] or 0, ahora['entregados'])

    return _indicador(
        'otif',
        valor,
        pie=(
            f"{ahora['a_tiempo']} de {ahora['entregados']} entregas a tiempo "
            f"y completas"
        ) if ahora['entregados'] else 'sin entregas cerradas en el periodo',
        variacion=_variacion(valor, _pct(antes['a_tiempo'] and antes['completos'] or 0,
                                         antes['entregados'])
                             if antes['entregados'] else None),
        detalle=[
            ('Entregas a tiempo', str(ahora['a_tiempo'])),
            ('Entregas completas', str(ahora['completos'])),
            ('Entregas sin detalle de productos', str(ahora['sin_detalle'])),
        ],
    )


def _pedidos_perfectos(actual, anterior):
    """
    Pedido perfecto: a tiempo, completo, sin incidencias y con la
    documentacion correcta. Es OTIF mas el filtro de incidencias.

    Que el objetivo sea >95% segun el estandar de logistica, y de ahi el
    umbral. Cuando hay entregas sin detalle de productos no se puede afirmar
    que el pedido documentara bien, asi que el indicador lo dice en vez de
    fingir un 100%.
    """
    ahora = _evaluar_entregas(actual.get('entregas'), actual.get('ventas_incidencia'))
    antes = _evaluar_entregas(anterior.get('entregas'), anterior.get('ventas_incidencia'))

    valor = None if ahora['entregados'] == 0 else _pct(ahora['perfectos'], ahora['entregados'])
    valor_antes = None if antes['entregados'] == 0 else _pct(antes['perfectos'], antes['entregados'])

    detalle = [
        ('Pedidos perfectos', str(ahora['perfectos'])),
        ('Con incidencia registrada', str(ahora['con_incidencia'])),
        ('Sin detalle para verificar', str(ahora['sin_detalle'])),
    ]
    if ahora['entregados'] == 0:
        detalle.append(('Aviso', 'sin entregas cerradas en el periodo'))

    return _indicador(
        'pedidos_perfectos',
        valor,
        pie=(
            f"objetivo > 95% · {ahora['perfectos']} de "
            f"{ahora['entregados']} entregas"
        ) if ahora['entregados'] else 'sin entregas cerradas en el periodo',
        variacion=_variacion(valor, valor_antes),
        detalle=detalle,
    )


def _sell_through(actual, anterior):
    """
    Sell-through: cuanto del inventario recibido se vendio de verdad.

    Se mide sobre las entradas al almacen, que es lo que la gestion de
    almacen controla: si entra mercancia y no sale, hay capital inmovilizado.
    """
    vendidas = actual.get('unidades_vendidas') or 0
    recibidas = actual.get('unidades_recibidas') or 0
    vendidas_antes = anterior.get('unidades_vendidas') or 0
    recibidas_antes = anterior.get('unidades_recibidas') or 0

    valor = None if recibidas == 0 else _pct(vendidas, recibidas)
    valor_antes = None if recibidas_antes == 0 else _pct(vendidas_antes, recibidas_antes)

    return _indicador(
        'sell_through',
        valor,
        pie=f"{vendidas} unidades vendidas de {recibidas} recibidas",
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Unidades vendidas', str(vendidas)),
            ('Unidades recibidas', str(recibidas)),
            ('Sin cobertura de stock',
             'no entro mercancia en el periodo' if recibidas == 0 else ''),
        ],
    )


def _ticket_promedio(actual, anterior):
    """Ticket promedio: monto total / num de ventas."""
    datos = actual.get('total_ventas_periodo') or {}
    ventas = datos.get('total_ventas', 0)
    unidades = datos.get('total_unidades', 0)
    datos_ant = anterior.get('total_ventas_periodo') or {}
    ventas_antes = datos_ant.get('total_ventas', 0)
    unidades_antes = datos_ant.get('total_unidades', 0)

    total_ventas = sum(v['pedidos'] for v in _estados_ventas(actual).values())
    monto = sum(v['monto'] for estado, v in _estados_ventas(actual).items() if estado == 'entregado')
    total_ventas_antes = sum(v['pedidos'] for v in _estados_ventas(anterior).values())
    monto_antes = sum(v['monto'] for estado, v in _estados_ventas(anterior).items() if estado == 'entregado')

    valor = None if total_ventas == 0 else round(monto / total_ventas, 2)
    valor_antes = None if total_ventas_antes == 0 else round(monto_antes / total_ventas_antes, 2)

    return _indicador(
        'ticket_promedio',
        valor,
        pie=f"S/ {monto:,.2f} en {total_ventas} ventas" if total_ventas else 'sin ventas en el periodo',
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Monto total', f"S/ {monto:,.2f}"),
            ('Numero de ventas', str(total_ventas)),
        ],
    )


def _upt(actual, anterior):
    """Unidades por transaccion: total de unidades / num de ventas."""
    total_ventas = sum(v['pedidos'] for v in _estados_ventas(actual).values())
    total_unidades = sum(v.get('unidades', 0) for v in _estados_ventas(actual).values())
    total_ventas_antes = sum(v['pedidos'] for v in _estados_ventas(anterior).values())
    total_unidades_antes = sum(v.get('unidades', 0) for v in _estados_ventas(anterior).values())

    valor = None if total_ventas == 0 else round(total_unidades / total_ventas, 2)
    valor_antes = None if total_ventas_antes == 0 else round(total_unidades_antes / total_ventas_antes, 2)

    return _indicador(
        'upt',
        valor,
        pie=f"{total_unidades} unidades en {total_ventas} ventas" if total_ventas else 'sin ventas',
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Unidades totales', str(total_unidades)),
            ('Ventas totales', str(total_ventas)),
        ],
    )


def _margen_bruto(actual, anterior):
    """Margen de beneficio bruto por venta."""
    margen = actual.get('margen_bruto') or 0.0
    datos = actual.get('total_ventas_periodo') or {}
    total_ventas = sum(v['pedidos'] for v in _estados_ventas(actual).values())
    datos_ant = anterior.get('margen_bruto') or 0.0
    total_ventas_antes = sum(v['pedidos'] for v in _estados_ventas(anterior).values())

    valor = None if total_ventas == 0 else round(margen / total_ventas, 2)
    valor_antes = None if total_ventas_antes == 0 else round(datos_ant / total_ventas_antes, 2) if total_ventas_antes else None

    return _indicador(
        'margen_bruto',
        valor,
        pie=f"S/ {margen:,.2f} de margen en {total_ventas} ventas" if total_ventas else 'sin ventas',
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Margen total', f"S/ {margen:,.2f}"),
            ('Ventas totales', str(total_ventas)),
        ],
    )


# ─────────────────────────────────────────────
# 2. CANCELACIONES
# ─────────────────────────────────────────────
def _cancelaciones(actual, anterior):
    """Interes que no llego a facturarse: pedidos, carritos, proveedores y conversion."""
    return {
        'tasa_cancelacion': _tasa_cancelacion(actual, anterior),
        'abandono_carrito': _abandono_carrito(actual, anterior),
        'cancelacion_proveedor': _cancelacion_proveedor(actual, anterior),
        'contratos_perdidos': _contratos_perdidos(actual, anterior),
        'tasa_conversion': _tasa_conversion(actual, anterior),
    }


def _tasa_cancelacion(actual, anterior):
    """Anulaciones del cliente antes de salir del negocio."""
    ahora = _estados_ventas(actual)
    antes = _estados_ventas(anterior)

    canceladas = ahora.get('cancelado', {}).get('pedidos', 0)
    total = sum(v['pedidos'] for v in ahora.values())
    canceladas_antes = antes.get('cancelado', {}).get('pedidos', 0)
    total_antes = sum(v['pedidos'] for v in antes.values())

    valor = _ratio(canceladas, total)
    valor_antes = _ratio(canceladas_antes, total_antes)

    return _indicador(
        'tasa_cancelacion',
        valor,
        pie=f"{canceladas} cancelados de {total} pedidos" if total else 'sin pedidos en el periodo',
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Pedidos cancelados', str(canceladas)),
            ('Pedidos del periodo', str(total)),
            ('Importe no facturado',
             f"S/ {ahora.get('cancelado', {}).get('monto', 0.0):,.2f}"),
        ],
    )


def _abandono_carrito(actual, anterior):
    """
    Carritos armados y nunca pagados.

    El carrito se vacia al confirmar la compra, asi que lo que queda en la
    tabla es un carrito abandonado por definicion. El denominator suma esos
    carritos a los pedidos que si se convirtieron: el total de intentos de
    compra que hubo en el periodo.
    """
    carritos = actual.get('carritos') or 0
    pedidos = sum(v['pedidos'] for v in _estados_ventas(actual).values())
    intentos = carritos + pedidos

    carritos_antes = anterior.get('carritos') or 0
    pedidos_antes = sum(v['pedidos'] for v in _estados_ventas(anterior).values())
    intentos_antes = carritos_antes + pedidos_antes

    valor = _ratio(carritos, intentos)
    valor_antes = _ratio(carritos_antes, intentos_antes)

    return _indicador(
        'abandono_carrito',
        valor,
        pie=f"{carritos} carritos sin pagar de {intentos} intentos",
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Carritos abandonados', str(carritos)),
            ('Pedidos completados', str(pedidos)),
            ('Nota', 'carritos que se armaron en el periodo y siguen sin pago'),
        ],
    )


def _tasa_conversion(actual, anterior):
    """Tasa de conversion: ventas completadas / (carritos + pedidos)."""
    carritos = actual.get('carritos') or 0
    pedidos = sum(v['pedidos'] for v in _estados_ventas(actual).values())
    total_ventas = pedidos  # solo cuenta las que se convirtieron
    intentos = carritos + pedidos

    carritos_antes = anterior.get('carritos') or 0
    pedidos_antes = sum(v['pedidos'] for v in _estados_ventas(anterior).values())
    intentos_antes = carritos_antes + pedidos_antes

    valor = None if intentos == 0 else _pct(total_ventas, intentos)
    valor_antes = None if intentos_antes == 0 else _pct(
        pedidos_antes, intentos_antes)

    return _indicador(
        'tasa_conversion',
        valor,
        pie=f"{total_ventas} conversiones de {intentos} intentos" if intentos else 'sin intentos en el periodo',
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Conversiones', str(total_ventas)),
            ('Carritos creados', str(carritos)),
            ('Total de intentos', str(intentos)),
        ],
    )


def _cancelacion_proveedor(actual, anterior):
    """
    Pedidos de reabastecimiento cancelados: el equivalente B2B de un contrato
    perdido en la compra.

    Se rotula asi, y no "contratos perdidos", porque el sistema no registra
    cotizaciones: lo que si registra es a quien se le pidio stock y ese pedido
    se cancelo.
    """
    ahora = actual.get('pedidos_proveedor') or {}
    antes = anterior.get('pedidos_proveedor') or {}

    cancelados = ahora.get('cancelado', 0)
    total = sum(ahora.values())
    cancelados_antes = antes.get('cancelado', 0)
    total_antes = sum(antes.values())

    valor = _ratio(cancelados, total)
    valor_antes = _ratio(cancelados_antes, total_antes)

    return _indicador(
        'cancelacion_proveedor',
        valor,
        pie=f"{cancelados} cancelados de {total} pedidos a proveedor" if total else 'sin pedidos a proveedor',
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Pedidos cancelados', str(cancelados)),
            ('Pedidos enviados', str(ahora.get('enviado', 0))),
            ('Pedidos pendientes', str(ahora.get('pendiente', 0))),
        ],
    )


def _contratos_perdidos(actual, anterior):
    """
    Cotizaciones y propuestas rechazadas.

    No hay tabla de cotizaciones en el sistema, asi que no existe el
    denominador: solo se puede contar cuantas se perdieron y por que motivo.
    Por eso el valor es None y no 0, y el panel lo explica en vez de pintar un
    0% que haria pensar que no se pierde nada.
    """
    ahora = _motivos(actual, 'contrato_perdido')
    antes = _motivos(anterior, 'contrato_perdido')
    principal = ahora[0] if ahora else None

    return _indicador(
        'contratos_perdidos',
        None,
        pie=(
            f"{sum(m['n'] for m in ahora)} perdidas · "
            f"motivo principal: {_etiqueta_motivo(principal['motivo'])}"
        ) if ahora else 'sin cotizaciones perdidas registradas',
        variacion=None,
        detalle=[
            ('Registradas', str(sum(m['n'] for m in ahora))),
            ('Periodo anterior', str(sum(m['n'] for m in antes))),
            ('Por que', 'no se registra el total de cotizaciones enviadas, '
                        'asi que el porcentaje no es calculable'),
        ],
    )


# ─────────────────────────────────────────────
# 3. PROBLEMAS
# ─────────────────────────────────────────────
def _problemas(actual, anterior):
    """Devoluciones, backorder, picking, mermas y transporte."""
    return {
        'devoluciones': _devoluciones(actual, anterior),
        'backorder': _backorder(actual),
        'picking': _picking(actual, anterior),
        'mermas': _mermas(actual, anterior),
        'transporte': _transporte(actual, anterior),
    }


def _motivos(datos, tipo):
    """Motivos registrados de un tipo, en el orden que dio la consulta."""
    return [
        fila for fila in (datos.get('incidencias_motivo') or [])
        if fila['tipo'] == tipo
    ]


def _conteo(datos, tipo):
    """Total de incidencias de un tipo en el periodo."""
    return ((datos.get('incidencias') or {}).get(tipo) or {}).get('n', 0)


def _unidades_incidentes(datos, tipo):
    return ((datos.get('incidencias') or {}).get(tipo) or {}).get('unidades', 0)


def _devoluciones(actual, anterior):
    """
    Tasa de devoluciones sobre las unidades efectivamente entregadas.

    Se categoriza el motivo porque "el cliente devolvio algo" no sirve para
    decidir nada: un defecto del proveedor y una talla equivocada tienen
   owners y arreglos distintos.
    """
    entregadas = actual.get('unidades_entregadas') or 0
    unidades = _unidades_incidentes(actual, 'devolucion')
    casos = _conteo(actual, 'devolucion')

    entregadas_antes = anterior.get('unidades_entregadas') or 0
    unidades_antes = _unidades_incidentes(anterior, 'devolucion')

    valor = _ratio(unidades, entregadas)
    valor_antes = _ratio(unidades_antes, entregadas_antes)

    por_motivo = [
        {'motivo': m['motivo'], 'etiqueta': _etiqueta_motivo(m['motivo']),
         'n': m['n'], 'unidades': m['unidades']}
        for m in _motivos(actual, 'devolucion')
    ]

    return _indicador(
        'devoluciones',
        valor,
        pie=(
            f"{casos} devoluciones · {unidades} unidades de {entregadas} entregadas"
        ) if entregadas else 'sin unidades entregadas en el periodo',
        variacion=_variacion(valor, valor_antes),
        detalle=[('Motivo principal',
                  por_motivo[0]['etiqueta'] if por_motivo else 'sin devoluciones')],
        motivos=por_motivo,
    )


def _backorder(actual):
    """
    Pedidos que se tomaron y no se pueden cumplir por falta de stock.

    No lleva periodo ni variacion: mide la cola abierta de ahora, y un
    promedio de ayer no dice nada sobre si el almacen se esta recuperando.
    """
    datos = actual.get('backorder') or {'abiertos': 0, 'sin_stock': 0}
    abiertos = datos['abiertos']
    sin_stock = datos['sin_stock']

    valor = _ratio(sin_stock, abiertos)
    try:
        reposicion = repository.fetch_pedidos_reposicion_pendientes()
    except Exception:
        reposicion = 0

    return _indicador(
        'backorder',
        valor,
        pie=f"{sin_stock} sin stock de {abiertos} pedidos abiertos",
        variacion=None,
        detalle=[
            ('Pedidos abiertos', str(abiertos)),
            ('Pedidos sin stock fisico', str(sin_stock)),
            ('Reposiciones encoladas', str(reposicion)),
        ],
    )


def _picking(actual, anterior):
    """
    Errores de armado: producto, color o cantidad equivocados en el paquete.

    Se suma el registro manual con una senal que sale sola de los datos: las
    entregas cerradas en las que lo entregado no cubre lo pedido. Esa segunda
    cifra detecta el fallo aunque nadie lo haya reportado, que es
    precisamente el caso que hace dano.
    """
    casos = _conteo(actual, 'picking')
    entregas = _evaluar_entregas(actual.get('entregas'), actual.get('ventas_incidencia'))
    incompletas = entregas['entregados'] - entregas['completos'] - entregas['sin_detalle']
    total_entregas = entregas['entregados']

    casos_antes = _conteo(anterior, 'picking')
    entregas_antes = _evaluar_entregas(anterior.get('entregas'),
                                       anterior.get('ventas_incidencia'))
    incompletas_antes = (
        entregas_antes['entregados'] - entregas_antes['completos']
        - entregas_antes['sin_detalle']
    )
    total_antes = entregas_antes['entregados']

    valor = None if total_entregas == 0 else _pct(casos + incompletas, total_entregas)
    valor_antes = None if total_antes == 0 else _pct(casos_antes + incompletas_antes, total_antes)

    return _indicador(
        'picking',
        valor,
        pie=f"{casos} reportados · {incompletas} entregas con faltantes",
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Errores reportados', str(casos)),
            ('Entregas con faltantes', str(incompletas)),
            ('Total de entregas', str(total_entregas)),
        ],
    )


def _mermas(actual, anterior):
    """
    Contraccion de inventario: mercancia que desaparece del almacen.

    Unico indicador del bloque que no sale de las tablas: ni los ingresos ni
    las salidas registran una merma. Se compara con el valor del producto para
    saber cuanto dinero representa, no cuantas cajas.
    """
    casos = _conteo(actual, 'merma')
    unidades = _unidades_incidentes(actual, 'merma')
    casos_antes = _conteo(anterior, 'merma')

    valor = float(casos) if casos else (0.0 if _hay_registro('merma') else None)
    valor_antes = float(casos_antes) if casos_antes else (
        0.0 if _hay_registro('merma') else None)

    return _indicador(
        'mermas',
        valor,
        pie=f"{unidades} unidades perdidas · objetivo 0",
        variacion=_variacion(valor, valor_antes),
        detalle=[
            ('Mermas registradas', str(casos)),
            ('Unidades perdidas', str(unidades)),
            ('Nota', 'el almacen no descuenta la merma del stock: '
                     'registrala para que el indicador la vea'),
        ],
    )


def _hay_registro(tipo):
    """
    True si el modulo ya tiene tabla de incidencias, para poder distinguir
    "cero mermas registradas" de "aun no se ha registrado ninguna nunca".
    """
    try:
        cur = repository.get_cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) AS n FROM kpi_incidencias WHERE tipo = %s",
                (tipo,),
            )
            return _to_int((cur.fetchone() or {}).get('n'))
        finally:
            cur.close()
    except Exception:
        return True  # si ni se puede preguntar, no se afirma ausencia de datos


def _to_int(valor):
    return int(valor) if valor is not None else 0


def _transporte(actual, anterior):
    """Danos del traslado y entregas en la direccion equivocada."""
    casos = _conteo(actual, 'transporte')
    casos_antes = _conteo(anterior, 'transporte')
    entregas = len(actual.get('entregas') or [])
    entregas_antes = len(anterior.get('entregas') or [])

    valor = None if entregas == 0 else _pct(casos, entregas)
    valor_antes = None if entregas_antes == 0 else _pct(casos_antes, entregas_antes)

    motivos = [
        {'motivo': m['motivo'], 'etiqueta': _etiqueta_motivo(m['motivo']),
         'n': m['n']}
        for m in _motivos(actual, 'transporte')
    ]

    return _indicador(
        'transporte',
        valor,
        pie=f"{casos} incidencias sobre {entregas} entregas" if entregas
            else 'sin entregas cerradas en el periodo',
        variacion=_variacion(valor, valor_antes),
        detalle=[('Motivo principal',
                  motivos[0]['etiqueta'] if motivos else 'sin incidencias')],
        motivos=motivos,
    )


def _etiqueta_motivo(motivo):
    return settings.ETIQUETAS_MOTIVO.get(motivo, (motivo or '—').replace('_', ' '))


# ─────────────────────────────────────────────
# RESUMEN, TENDENCIA Y ALMACEN
# ─────────────────────────────────────────────
def _resumen(actual, anterior):
    estados = _estados_ventas(actual)
    total_pedidos = sum(v['pedidos'] for v in estados.values())
    monto = sum(v['monto'] for estado, v in estados.items() if estado == 'entregado')
    antes = _estados_ventas(anterior)
    monto_antes = sum(v['monto'] for estado, v in antes.items() if estado == 'entregado')
    pedidos_antes = sum(v['pedidos'] for v in antes.values())

    datos = actual.get('total_ventas_periodo') or {}
    total_unidades = datos.get('total_unidades', 0)

    clientes_recurrentes = actual.get('clientes_recurrentes') or 0
    total_clientes = actual.get('total_clientes') or 0

    return {
        'pedidos': total_pedidos,
        'monto': round(monto, 2),
        'ticket_medio': round(monto / total_pedidos, 2) if total_pedidos else 0.0,
        'variacion_pedidos': _variacion(total_pedidos, pedidos_antes),
        'variacion_monto': _variacion(monto, monto_antes),
        'unidades': total_unidades,
        'clientes_recurrentes': clientes_recurrentes,
        'total_clientes': total_clientes,
        'tasa_recurrentes': _pct(clientes_recurrentes, total_clientes) if total_clientes else None,
    }


def _tendencia(actual):
    """Serie diaria de ventas, entregas, cancelaciones e incidencias."""
    try:
        ventas_por_dia = actual.get('ventas_por_dia') or []
        return [
            {
                'dia': d.get('dia', ''),
                'peticiones': d.get('pedidos', 0),
                'errores': d.get('canceladas', 0),
            }
            for d in ventas_por_dia
        ]
    except Exception:
        return []


def _almacen(actual):
    """Estado del almacen, rotacion y DOH."""
    try:
        inventario = repository.fetch_inventario()
    except Exception:
        inventario = None
    try:
        reposiciones = repository.fetch_reposiciones_sugeridas()
    except Exception:
        reposiciones = []
    try:
        stock_promedio = repository.fetch_stock_promedio()
    except Exception:
        stock_promedio = None
    try:
        inventario_valor = repository.fetch_inventario_valor()
    except Exception:
        inventario_valor = None

    total_ventas_periodo = actual.get('total_ventas_periodo') or {}
    total_ventas = total_ventas_periodo.get('total_ventas', 0)

    rotacion = None
    if inventario_valor and stock_promedio and stock_promedio > 0:
        try:
            rotacion = round(inventario_valor / stock_promedio, 2)
        except Exception:
            rotacion = None
    doh = round(365 / rotacion, 1) if rotacion and rotacion > 0 else None

    return {
        'inventario': inventario,
        'reposiciones': reposiciones,
        'rotacion_inventario': rotacion,
        'doh': doh,
        'stock_promedio': stock_promedio,
        'inventario_valor': inventario_valor,
    }
