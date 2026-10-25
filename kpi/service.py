"""
Formulas de los KPIs de negocio.

Reglas del modulo:

* Cada indicador se aisla: si una consulta falla, devuelve "sin datos" en vez
  de tumbar el panel. Un panel de indicadores no puede impedir administrar.
* Ningun indicador se inventa un cero. Cuando no hay base de calculo el valor
  es None y la tarjeta lo dice; un 0 real y un "no hay nada que medir" son
  cosas distintas.
* Una venta cancelada no es una venta: el filtro vive en repository.
"""
import datetime

from flask import current_app

from . import repository, settings

PRESETS = ('hoy', '7d', '30d', 'mes', 'personalizado')


# ─────────────────────────────────────────────
# PERIODO
# ─────────────────────────────────────────────
def resolve_period(args):
    """
    Resuelve el periodo solicitado y su equivalente anterior.

    Devuelve un dict con desde/hasta de ambos periodos para poder calcular la
    variacion: es la unica forma honesta de comparar.

    El "ahora" es el reloj de la BASE DE DATOS, no el de Python. alwaysdata
    corre MySQL en CEST (UTC+2) y el contenedor de la app en UTC: con el reloj
    local, las ventas de las ultimas horas caian fuera del periodo y el panel
    mostraba cero pedidos con ventas registradas.
    """
    hoy = repository.ahora_bd()
    inicio_dia = hoy.replace(hour=0, minute=0, second=0, microsecond=0)

    preset = (args.get('preset') or settings.PRESET_POR_DEFECTO).strip()
    if preset not in PRESETS:
        preset = settings.PRESET_POR_DEFECTO

    if preset == 'hoy':
        desde, hasta = inicio_dia, hoy + datetime.timedelta(seconds=1)
    elif preset == '7d':
        desde = hoy - datetime.timedelta(days=7)
        hasta = hoy + datetime.timedelta(seconds=1)
    elif preset == '30d':
        desde = hoy - datetime.timedelta(days=30)
        hasta = hoy + datetime.timedelta(seconds=1)
    elif preset == 'mes':
        desde = datetime.datetime(hoy.year, hoy.month, 1)
        hasta = hoy + datetime.timedelta(seconds=1)
    else:  # personalizado
        desde, hasta = _parse_custom(args, inicio_dia, hoy)

    if desde > hasta:
        desde, hasta = hasta, desde

    duracion = hasta - desde
    # El periodo anterior es el de la misma longitud, inmediatamente anterior.
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
    fallback_desde = hoy - datetime.timedelta(days=30)
    try:
        desde = datetime.datetime.strptime(args.get('desde', ''), '%d/%m/%Y')
    except (ValueError, TypeError):
        desde = fallback_desde
    try:
        hasta = datetime.datetime.strptime(args.get('hasta', ''), '%d/%m/%Y')
    except (ValueError, TypeError):
        hasta = hoy
    if desde.date() == hoy.date():
        desde = inicio_dia
    return desde, hasta + datetime.timedelta(days=1)


# ─────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────
def _pct(numerador, denominador):
    """Porcentaje redondeado a dos decimales. 0 si no hay denominador."""
    if not denominador:
        return 0.0
    return round(numerador / denominador * 100.0, 2)


def _ratio(numerador, denominador):
    """Porcentaje, o None si no hay base de calculo."""
    if not denominador:
        return None
    return round(numerador / denominador * 100.0, 2)


def _variacion(actual, anterior):
    """Variacion porcentual entre dos periodos (None si no es comparable)."""
    if actual is None or anterior is None:
        return None
    if anterior == 0:
        return None if actual else 0.0
    return round((actual - anterior) / abs(anterior) * 100.0, 2)


def _indicador(clave, valor, pie='', variacion=None, detalle=None, motivos=None):
    """
    Envoltura comun de todos los indicadores.

    Centraliza el semaforo y el sentido "bueno" para que ninguna tarjeta quede
    con el color equivocado: ambos salen de settings.UMBRALES.
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


def _etiqueta(clave, tabla):
    return tabla.get(clave, clave)


def _percentil(valores, p):
    """Percentil simple (interpolacion lineal) sobre una lista de numeros."""
    if not valores:
        return None
    ordenados = sorted(valores)
    if len(ordenados) == 1:
        return float(ordenados[0])
    k = (len(ordenados) - 1) * (p / 100.0)
    bajo = int(k)
    alto = min(bajo + 1, len(ordenados) - 1)
    return ordenados[bajo] + (ordenados[alto] - ordenados[bajo]) * (k - bajo)


# ─────────────────────────────────────────────
# SNAPSHOT
# ─────────────────────────────────────────────
def build_snapshot(periodo):
    """Calcula todas las secciones del periodo y del anterior."""
    try:
        actual = _collect(periodo['desde'], periodo['hasta'], periodo['dias'])
    except Exception as exc:
        current_app.logger.warning('KPI _collect actual fallo: %s', exc)
        actual = {}
    try:
        anterior = _collect(periodo['prev_desde'], periodo['prev_hasta'],
                            periodo['dias'])
    except Exception as exc:
        current_app.logger.warning('KPI _collect anterior fallo: %s', exc)
        anterior = {}

    def safe_call(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            current_app.logger.warning('KPI seccion %s fallo: %s', fn.__name__, exc)
            return {}

    return {
        'periodo': periodo,
        # La hora de generacion es la de la base de datos: es la misma que
        # puso las marcas de tiempo de los datos que se estan mostrando.
        'generado_en': repository.ahora_bd(),
        'ventas': safe_call(_seccion_ventas, actual, anterior),
        'almacen': safe_call(_seccion_almacen, actual, anterior),
        'seguridad': safe_call(_seccion_seguridad, actual, anterior),
        'carritos': safe_call(_seccion_carritos, actual, anterior),
        # Catalogo y cola de incidencias: el panel los usa para poder
        # registrar devoluciones, mermas y abandonos sin salir del KPI.
        'tipos_incidencia': [
            {'clave': clave, 'nombre': nombre, 'icono': icono,
             'motivos': list(settings.MOTIVOS_POR_TIPO.get(clave, ()))}
            for clave, nombre, icono, _ in settings.TIPOS_INCIDENCIA
        ],
        'incidencias_recientes': safe_call(_incidencias_recientes),
    }


def _incidencias_recientes():
    """Ultimas incidencias registradas, para la cola del panel."""
    try:
        return repository.fetch_incidencias_recientes(limite=15)
    except Exception:
        return []


def _collect(desde, hasta, dias):
    """
    Ejecuta las consultas del periodo.

    Cada una se aisla: si falla, se guarda un valor vacio y el indicador sale
    como "sin datos" en vez de contagiarse al resto del panel.
    """
    consultas = {
        'ventas': lambda: repository.fetch_ventas_periodo(desde, hasta),
        'cancel_motivos': lambda: repository.fetch_cancelaciones_por_motivo(desde, hasta),
        'compras_cliente': lambda: repository.fetch_compras_por_cliente(desde, hasta),
        'repuestos': lambda: repository.fetch_repuestos_demanda(desde, hasta),
        'doh': lambda: repository.fetch_doh_categorias(desde, hasta),
        'categorias': lambda: repository.categorias_existentes(),
        'seguridad': lambda: repository.fetch_seguridad(desde, hasta),
        'carritos': lambda: repository.fetch_carritos_abandonados(desde, hasta),
        'incidencias': lambda: repository.fetch_incidencias(desde, hasta),
    }
    d = {'desde': desde, 'hasta': hasta, 'dias': max(1, int(dias))}
    for clave, funcion in consultas.items():
        try:
            d[clave] = funcion()
        except Exception as exc:
            current_app.logger.warning('KPI consulta %s fallo: %s', clave, exc)
            d[clave] = None
    return d


# ─────────────────────────────────────────────
# 1. VENTAS Y FACTURACION
# ─────────────────────────────────────────────
def _seccion_ventas(actual, anterior):
    v = actual.get('ventas') or {}
    va = anterior.get('ventas') or {}
    validas = v.get('validas') or {}
    validas_ant = va.get('validas') or {}
    canceladas = v.get('canceladas') or {}

    n = validas.get('n', 0)
    monto = validas.get('monto', 0.0)
    n_ant = validas_ant.get('n', 0)
    monto_ant = validas_ant.get('monto', 0.0)
    total_n = v.get('total_n', 0)

    volumen = _indicador(
        'volumen_ventas', n,
        pie=f"{canceladas.get('n', 0)} cancelado(s) en el periodo",
        variacion=_variacion(n, n_ant),
        detalle=[
            ('Pedidos validos', str(n)),
            ('Pedidos cancelados', str(canceladas.get('n', 0))),
            ('Total de pedidos', str(total_n)),
        ],
    )

    ingresos = _indicador(
        'ingresos_brutos', round(monto, 2),
        pie=f"S/ {monto:,.2f} facturado",
        variacion=_variacion(monto, monto_ant),
        detalle=[
            ('Ingresos brutos', f"S/ {monto:,.2f}"),
            ('Importe no facturado (cancelados)', f"S/ {canceladas.get('monto', 0.0):,.2f}"),
        ],
    )

    # Motivos de cancelacion (KPIs 3 y 4)
    motivos_raw = actual.get('cancel_motivos') or []
    por_motivo = {m['motivo']: m['n'] for m in motivos_raw}
    error_cliente = por_motivo.get(settings.MOTIVO_CANCELACION_CLIENTE, 0)

    cancelacion = _indicador(
        'cancelacion_error_cliente', _ratio(error_cliente, total_n),
        pie=f"{error_cliente} por error del cliente de {total_n} pedidos",
        detalle=[
            ('Cancelados por error del cliente', str(error_cliente)),
            ('Total de pedidos', str(total_n)),
            ('Total de cancelados', str(canceladas.get('n', 0))),
        ],
    )

    # Indice de motivos (tabla, no tarjeta)
    motivos_tabla = []
    total_cancel = sum(m['n'] for m in motivos_raw) or 0
    for m in motivos_raw:
        motivos_tabla.append({
            'etiqueta': _etiqueta(m['motivo'], settings.ETIQUETAS_CANCELACION),
            'n': m['n'],
            'pct': _pct(m['n'], total_cancel),
        })

    # Frecuencia de compra por ciclo de mantenimiento
    freq_valor, detalle_freq = _frecuencia_compra(actual)
    frecuencia = _indicador(
        'frecuencia_compra', freq_valor,
        pie=f"ciclo objetivo: {settings.CICLO_MANTENIMIENTO_DIAS} dias",
        detalle=detalle_freq,
    )

    return {
        'volumen': volumen,
        'ingresos': ingresos,
        'cancelacion_error_cliente': cancelacion,
        'frecuencia_compra': frecuencia,
        'motivos_cancelacion': motivos_tabla,
    }


def _frecuencia_compra(actual):
    """
    Mide cada cuanto vuelve un cliente a comprar.

    Para cada cliente se calcula el intervalo medio entre sus compras del
    periodo. El indicador es el porcentaje de clientes recurrentes cuyo
    intervalo no pasa del ciclo de mantenimiento.
    """
    filas = actual.get('compras_cliente') or []
    por_cliente = {}
    for fila in filas:
        por_cliente.setdefault(fila['cliente_id'], []).append(fila['fecha'])

    clientes = len(por_cliente)
    recurrentes = 0
    cumplen = 0
    intervalos = []
    for fechas in por_cliente.values():
        n = len(fechas)
        if n >= 2:
            recurrentes += 1
            intervalo = (fechas[-1] - fechas[0]).days / (n - 1)
            intervalos.append(intervalo)
            if intervalo <= settings.CICLO_MANTENIMIENTO_DIAS:
                cumplen += 1

    dias_prom = round(sum(intervalos) / len(intervalos), 1) if intervalos else None
    detalle = [
        ('Clientes que compraron', str(clientes)),
        ('Clientes recurrentes (2+ compras)', str(recurrentes)),
        ('Dias promedio entre compras',
         f"{dias_prom:.1f}" if dias_prom is not None else '—'),
        ('Ciclo de mantenimiento', f"{settings.CICLO_MANTENIMIENTO_DIAS} dias"),
    ]
    return _ratio(cumplen, recurrentes), detalle


# ─────────────────────────────────────────────
# 2. GESTION DE ALMACEN E INVENTARIO
# ─────────────────────────────────────────────
def _seccion_almacen(actual, anterior):
    # Quiebre de stock en repuestos de alta demanda
    repuestos = actual.get('repuestos') or []
    demanda = [r for r in repuestos if (r.get('unidades') or 0) > 0]
    umbral = _percentil([r['unidades'] for r in demanda], 80)
    alta = [r for r in demanda if umbral and r['unidades'] >= umbral]
    agotados = [r for r in alta if (r.get('stock') or 0) <= 0]

    categorias = actual.get('categorias') or []
    falta_repuestos = not repuestos
    aviso_categorias = _aviso_categorias(
        settings.CATEGORIA_REPUESTOS, categorias)

    quiebre = _indicador(
        'quiebre_repuestos', _ratio(len(agotados), len(alta)),
        pie=(aviso_categorias or
             f"{len(agotados)} agotado(s) de {len(alta)} de alta demanda"),
        detalle=[
            ('Repuestos activos', str(len(repuestos))),
            ('Repuestos con demanda', str(len(demanda))),
            ('Repuestos de alta demanda', str(len(alta))),
            ('Agotados (stock <= 0)', str(len(agotados))),
        ] + ([('Aviso', aviso_categorias)] if aviso_categorias else []),
        motivos=[{'etiqueta': r['nombre'], 'n': r['stock']} for r in agotados[:8]],
    )

    # Dias de inventario por categoria
    doh = actual.get('doh') or {}
    dias = max(1, int((actual.get('dias') or 30)))
    acc = _doh(doh, settings.CATEGORIA_ACCESORIOS, dias)
    rep = _doh(doh, settings.CATEGORIA_REPUESTOS, dias)

    doh_acc = _indicador(
        'doh_accesorios', acc[0],
        pie='dias de stock al ritmo de venta actual',
        detalle=acc[1],
    )
    doh_rep = _indicador(
        'doh_repuestos', rep[0],
        pie='dias de stock al ritmo de venta actual',
        detalle=rep[1],
    )

    # Devoluciones por incompatibilidad (motivo registrado)
    incidencias = actual.get('incidencias') or []
    devol = [i for i in incidencias if i['tipo'] == 'devolucion']
    incompat = sum(i['n'] for i in devol
                   if i['motivo'] == settings.MOTIVO_DEVOLUCION_INCOMPATIBILIDAD)
    total_devol = sum(i['n'] for i in devol)
    validas_n = ((actual.get('ventas') or {}).get('validas') or {}).get('n', 0)

    dev_inc = _indicador(
        'devoluciones_incompat', _ratio(incompat, validas_n),
        pie=f"{incompat} de {validas_n} pedidos validos",
        detalle=[
            ('Devoluciones por incompatibilidad', str(incompat)),
            ('Devoluciones totales', str(total_devol)),
            ('Pedidos validos', str(validas_n)),
        ],
        motivos=[{'etiqueta': _etiqueta(i['motivo'], settings.ETIQUETAS_MOTIVO),
                  'n': i['n']} for i in devol],
    )

    return {
        'quiebre_repuestos': quiebre,
        'doh_accesorios': doh_acc,
        'doh_repuestos': doh_rep,
        'devoluciones_incompat': dev_inc,
    }


def _doh(doh, categorias, dias):
    """
    Dias de inventario de un grupo de categorias: stock / (unidades/dia).

    `doh` es el desglose por categoria que devuelve el repositorio. Se suman
    las categorias pedidas para que "Repuestos tecnicos" pueda repartirse en
    varias etiquetas del catalogo sin rehacer el SQL.
    """
    categorias = tuple(categorias or ())
    if not categorias:
        return None, [('Sin categorias configuradas', '')]

    stock = 0
    unidades = 0
    existentes = []
    for categoria in categorias:
        info = (doh or {}).get(categoria) or {}
        stock += info.get('stock', 0)
        unidades += info.get('unidades', 0)
        if info.get('existe'):
            existentes.append(categoria)

    detalle = [
        ('Categorias', ', '.join(categorias)),
        ('Stock actual (unidades)', str(stock)),
        ('Vendidas en el periodo', str(unidades)),
    ]
    if not existentes:
        detalle.append(('Aviso',
                        'Ningun producto activo tiene esas categorias'))
        return None, detalle
    if not unidades:
        detalle.append(('Nota', 'sin ventas en el periodo'))
        return None, detalle
    return round(stock / (unidades / dias), 1), detalle


def _aviso_categorias(categorias, existentes):
    """
    Explica por que un indicador de inventario se queda sin datos.

    Sin esto el panel mostraba un "sin datos" mudo cuando la categoria del KPI
    ("Repuestos") no existia en el catalogo real de la tienda, y no habia
    forma de saber que el problema era de configuracion y no de datos.
    """
    categorias = tuple(categorias or ())
    if not categorias or not existentes:
        return ''
    faltan = [c for c in categorias if c not in existentes]
    if not faltan:
        return ''
    return (f"Sin productos en la categoria '{', '.join(faltan)}'. "
            f"Categorias del catalogo: {', '.join(existentes) or 'ninguna'}")


# ─────────────────────────────────────────────
# 3. SEGURIDAD Y CONTROL DE USUARIOS
# ─────────────────────────────────────────────
def _seccion_seguridad(actual, anterior):
    s = actual.get('seguridad') or {}
    sa = anterior.get('seguridad') or {}

    cuentas = s.get('cuentas_intentos', 0)
    bloqueadas = s.get('cuentas_bloqueadas', 0)
    bloqueadas_ant = sa.get('cuentas_bloqueadas', 0)
    intentos = s.get('intentos_total', 0)
    intentos_ant = sa.get('intentos_total', 0)
    dias = max(1, int((actual.get('dias') or 30)))
    dias_ant = max(1, int((anterior.get('dias') or 30)))

    tasa_bloqueo = _indicador(
        'bloqueo_cuentas', _ratio(bloqueadas, cuentas),
        pie=f"{bloqueadas} cuenta(s) bloqueada(s) de {cuentas} con intentos",
        variacion=_variacion(bloqueadas, bloqueadas_ant),
        detalle=[
            ('Cuentas con intentos fallidos', str(cuentas)),
            ('Cuentas bloqueadas', str(bloqueadas)),
            ('IPs bloqueadas', str(s.get('ips_bloqueadas', 0))),
        ],
    )

    por_dia = round(intentos / dias, 2)
    intentos_dia_ant = round(intentos_ant / dias_ant, 2)
    frecuencia = _indicador(
        'intentos_fallidos', por_dia,
        pie=f"{intentos} intentos fallidos en {dias} dia(s)",
        variacion=_variacion(por_dia, intentos_dia_ant),
        detalle=[
            ('Intentos fallidos (total)', str(intentos)),
            ('Intentos por dia', str(por_dia)),
            ('Cuentas afectadas', str(cuentas)),
            ('Nota', 'la base guarda la racha desde el 3er fallo (UMBRAL_INTENTOS); '
                     'los dos primeros se cuentan en el navegador'),
        ],
    )

    return {
        'bloqueo_cuentas': tasa_bloqueo,
        'intentos_fallidos': frecuencia,
    }


# ─────────────────────────────────────────────
# 4. E-COMMERCE Y CARRITOS
# ─────────────────────────────────────────────
def _seccion_carritos(actual, anterior):
    # `carritos` es None cuando la tabla no tiene columna de fecha: entonces
    # no hay forma de saber cuando se armo el carrito y el indicador se queda
    # sin datos. Un 0 de aqui seria un "0% de abandono" falso.
    carritos = actual.get('carritos')
    carritos_ant = anterior.get('carritos')
    pedidos = ((actual.get('ventas') or {}).get('total_n')) or 0
    pedidos_ant = ((anterior.get('ventas') or {}).get('total_n')) or 0

    # El carrito se vacia al pagar: lo que queda es, por definicion, abandonado.
    # El denominador son los intentos de compra: carritos armados + pedidos.
    if carritos is None:
        intentos = None
        intentos_ant = None
    else:
        intentos = carritos + pedidos
        intentos_ant = (carritos_ant or 0) + pedidos_ant

    abandono = _indicador(
        'abandono_carrito', _ratio(carritos, intentos),
        pie=(f"{carritos} carrito(s) sin pagar de {intentos} intentos"
             if intentos is not None
             else 'Sin columna de fecha en `carrito`: no se puede medir'),
        variacion=_variacion(_ratio(carritos, intentos),
                             _ratio(carritos_ant, intentos_ant)),
        detalle=[
            ('Carritos abandonados', '—' if carritos is None else str(carritos)),
            ('Pedidos completados', str(pedidos)),
            ('Nota', 'carritos armados en el periodo que siguen sin pago'),
        ],
    )

    incidencias = actual.get('incidencias') or []
    motivos = [
        {'etiqueta': _etiqueta(i['motivo'], settings.ETIQUETAS_MOTIVO), 'n': i['n']}
        for i in incidencias if i['tipo'] == 'abandono_carrito'
    ]
    abandono['motivos'] = motivos

    return {
        'abandono': abandono,
        'motivos_abandono': motivos,
    }
