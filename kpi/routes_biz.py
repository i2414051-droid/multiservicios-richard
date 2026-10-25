"""
Rutas del modulo de KPIs de Ventas y Almacen.

Se registra como blueprint 'kpi_biz' para no mezclar con los KPIs tecnicos
existentes. Todas las rutas exigen sesion de administrador.

El panel vive en /panel (el blueprint se registra sin url_prefix) y se abre
desde el boton "KPIs" de Accesos Rapidos en /dashboard. Antes se incrustaba
tambien en /admin con `{% include %}`; ya no. El fragmento que se reinyecta
al cambiar de periodo usa el mismo endpoint que el panel completo, solo
cambia la plantilla base.
"""
import datetime
import decimal
import os
import threading
import time

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)

from . import repository, schema, service, settings

kpi_biz_bp = Blueprint('kpi_biz', __name__)

# Plantillas
PANEL_FRAGMENTO = 'kpi_biz/_bloque.html'      # se reinyecta al cambiar periodo
PANEL_COMPLETO = 'kpi_biz/panel.html'         # pagina /panel

# Endpoints que reciben el snapshot en la plantilla.
#
# `kpi_biz.panel_completo` es la pagina /panel, a la que llega el boton "KPIs"
# de Accesos Rapidos en /dashboard. `kpi_biz.api_panel` la replenish cuando el
# administrador cambia el periodo sin recargar la pagina anfitriona.
#
# `admin` ya no entra: los KPIs se movieron fuera del panel de productos.
ENDPOINTS_CON_KPIS_BIZ = frozenset({'kpi_biz.api_panel', 'kpi_biz.panel_completo'})


# ─────────────────────────────────────────────
# AUTORIZACION
# ─────────────────────────────────────────────
def _es_admin():
    return flask_session.get('rol') in ('admin', 'administrador')


def _no_autorizado():
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Acceso denegado'}), 403
    return redirect(url_for('login'))


def _json_safe(valor):
    """Convierte la estructura del snapshot a tipos que jsonify sabe serializar."""
    if isinstance(valor, dict):
        return {k: _json_safe(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_json_safe(v) for v in valor]
    if isinstance(valor, datetime.datetime):
        return valor.isoformat(timespec='seconds')
    if isinstance(valor, datetime.date):
        return valor.isoformat()
    if isinstance(valor, datetime.timedelta):
        return valor.total_seconds()
    if isinstance(valor, decimal.Decimal):
        return float(valor)
    return valor


# ─────────────────────────────────────────────
# SNAPSHOT
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# CACHE DEL SNAPSHOT
# ─────────────────────────────────────────────
# Construir el snapshot cuesta ~22 consultas (~4 s contra la BD remota) y el
# JavaScript del panel lo vuelve a pedir cada 30 s. Los KPIs no necesitan ser
# tiempo real, asi que se guarda el resultado por periodo durante
# KPI_CACHE_TTL segundos (60 por defecto).
#
# La cache es por proceso: cada worker de gunicorn tiene la suya, de modo que
# como mucho se calcula una vez por worker y ventana de tiempo.
_CACHE_MAX = 24                      # periodos distintos que se conservan
_cache_snapshots = {}
_cache_lock = threading.Lock()


def _ttl_desde_entorno():
    """Lee KPI_CACHE_TTL sin romper el arranque si viene vacio o mal escrito."""
    try:
        return max(0, int(os.environ.get('KPI_CACHE_TTL') or 60))
    except (TypeError, ValueError):
        return 60


_CACHE_TTL = _ttl_desde_entorno()


def _clave_cache(args):
    """Identifica el periodo pedido SIN consultar la base de datos.

    Se arma con los parametros de la URL y no con las fechas ya resueltas:
    `resolve_period` parte de `datetime.now()`, asi que dos peticiones
    seguidas darian claves distintas (con microsegundos) y la cache no
    acertaria nunca. Ademas, resolver el periodo cuesta 1 consulta (~190 ms),
    y en un acierto de cache no queremos pagarla.
    """
    preset = (args.get('preset') or settings.PRESET_POR_DEFECTO).strip()
    if preset not in getattr(service, 'PRESETS', ()):
        preset = settings.PRESET_POR_DEFECTO
    return (preset, (args.get('desde') or '').strip(),
            (args.get('hasta') or '').strip())


def _snapshot_vacio():
    """Estructura minima que la plantilla espera si algo falla."""
    return {
        'periodo': {'preset': '30d', 'dias': 30},
        'ventas': {},
        'almacen': {},
        'seguridad': {},
        'carritos': {},
        'generado_en': datetime.datetime.now(),
    }


def invalidar_cache():
    """Borra la cache (util tras cargar datos de prueba o al cambiar el periodo)."""
    with _cache_lock:
        _cache_snapshots.clear()


def get_snapshot():
    """Punto unico de construccion del snapshot de negocio.

    Nunca lanza excepciones: si algo falla, devuelve estructura vacía
    para que el panel muestre "sin datos" en lugar de 500.
    """
    clave = _clave_cache(request.args)
    ahora = time.time()
    with _cache_lock:
        guardado = _cache_snapshots.get(clave)
        if guardado is not None and (ahora - guardado[0]) < _CACHE_TTL:
            return guardado[1]

    try:
        periodo = service.resolve_period(request.args)
        snapshot = service.build_snapshot(periodo)
    except Exception as exc:
        current_app.logger.warning('KPI-BIZ get_snapshot fallo: %s', exc)
        return _snapshot_vacio()

    with _cache_lock:
        _cache_snapshots[clave] = (ahora, snapshot)
        if len(_cache_snapshots) > _CACHE_MAX:
            viejos = sorted(_cache_snapshots,
                            key=lambda k: _cache_snapshots[k][0])
            for k in viejos[:len(_cache_snapshots) - _CACHE_MAX]:
                _cache_snapshots.pop(k, None)
    return snapshot


# ─────────────────────────────────────────────
# API — SNAPSHOT
# ─────────────────────────────────────────────
@kpi_biz_bp.route('/api/kpi-biz')
def api_snapshot():
    """Snapshot completo en JSON."""
    if not _es_admin():
        return _no_autorizado()
    try:
        return jsonify({'ok': True, 'kpis': _json_safe(get_snapshot())})
    except Exception as exc:
        current_app.logger.error('KPI-BIZ: fallo al construir snapshot: %s', exc)
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ─────────────────────────────────────────────
# PANEL — FRAGMENTO (para /admin)
# ─────────────────────────────────────────────
@kpi_biz_bp.route('/api/kpi-biz/panel')
def api_panel():
    """
    Devuelve solo el fragmento del panel, ya renderizado.

    Lo pide el navegador al cambiar de periodo. NO devuelve la pagina
    anfitriona: si lo hiciera, cambiar el periodo recargaria la tabla de
    productos y las consultas de /admin enteras.
    """
    if not _es_admin():
        return _no_autorizado()
    try:
        # No se pasa `kpis` a proposito: la plantilla lee `kpis_biz`, que
        # inyecta el context processor para este mismo endpoint. Pasarlo
        # ademas aqui calcularia el snapshot dos veces por peticion.
        return render_template(PANEL_FRAGMENTO)
    except Exception as exc:
        current_app.logger.error('KPI-BIZ: fallo al construir panel: %s', exc)
        return (
            '<div class="kpi-biz-error visible">'
            'No se pudieron calcular los indicadores. '
            'Los datos mostrados son los de la ultima carga correcta.'
            '</div>',
            500,
        )


# ─────────────────────────────────────────────
# PANEL — VENTANA COMPLETA
# ─────────────────────────────────────────────
@kpi_biz_bp.route('/panel')
def panel_completo():
    """Panel en pagina propia, con su propia plantilla base."""
    if not _es_admin():
        return _no_autorizado()
    # NO pasar `kpis=get_snapshot()`: la plantilla usa `kpis_biz`, que inyecta
    # el context processor para este mismo endpoint. Pasarlo aqui calculaba el
    # snapshot dos veces por visita (~8 s tirados a la basura).
    return render_template(PANEL_COMPLETO)


# ─────────────────────────────────────────────
# API — INCIDENCIAS (registro manual)
# ─────────────────────────────────────────────
@kpi_biz_bp.route('/api/kpi-biz/incidencia', methods=['POST'])
def api_registrar_incidencia():
    """Registra una incidencia de operacion (devolucion, picking, etc.)."""
    if not _es_admin():
        return _no_autorizado()

    if not request.is_json:
        return jsonify({'ok': False, 'error': 'Request debe ser JSON'}), 400

    data = request.get_json()
    tipo = data.get('tipo')
    motivo = data.get('motivo', 'otro')
    cantidad = int(data.get('cantidad', 1))
    venta_id = data.get('venta_id')
    producto_id = data.get('producto_id')
    referencia = data.get('referencia')
    nota = data.get('nota')

    if tipo not in settings.ETIQUETAS_TIPO:
        return jsonify({'ok': False, 'error': 'Tipo de incidencia invalido'}), 400

    if motivo not in settings.MOTIVOS_POR_TIPO.get(tipo, ('otro',)):
        motivo = 'otro'

    try:
        inc_id = repository.insert_incidencia(
            tipo=tipo,
            motivo=motivo,
            cantidad=cantidad,
            venta_id=venta_id,
            producto_id=producto_id,
            referencia=referencia,
            nota=nota,
        )
        return jsonify({'ok': True, 'id': inc_id})
    except Exception as exc:
        current_app.logger.error('KPI-BIZ: fallo al registrar incidencia: %s', exc)
        return jsonify({'ok': False, 'error': str(exc)}), 500


@kpi_biz_bp.route('/api/kpi-biz/incidencia/<int:inc_id>', methods=['DELETE'])
def api_eliminar_incidencia(inc_id):
    """Elimina una incidencia mal registrada."""
    if not _es_admin():
        return _no_autorizado()
    try:
        ok = repository.eliminar_incidencia(inc_id)
        return jsonify({'ok': ok})
    except Exception as exc:
        current_app.logger.error('KPI-BIZ: fallo al borrar incidencia: %s', exc)
        return jsonify({'ok': False, 'error': str(exc)}), 500


@kpi_biz_bp.route('/api/kpi-biz/incidencias-recientes')
def api_incidencias_recientes():
    """Ultimas incidencias para el panel lateral."""
    if not _es_admin():
        return _no_autorizado()
    try:
        tipo = request.args.get('tipo')
        tipos = [tipo] if tipo else None
        data = repository.fetch_incidencias_recientes(limite=10, tipos=tipos)
        return jsonify({'ok': True, 'incidencias': _json_safe(data)})
    except Exception as exc:
        current_app.logger.error('KPI-BIZ: fallo al leer incidencias: %s', exc)
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ─────────────────────────────────────────────
# CONTEXT PROCESSOR (para incrustar en /admin)
# ─────────────────────────────────────────────
def registrar_context_processor(app):
    """
    Inyecta `kpis_biz` en las plantillas de ENDPOINTS_CON_KPIS_BIZ.

    Solo carga en /admin (y en el propio endpoint del fragmento) para no
    penalizar la tienda. Si la BD falla, devuelve {} y el resto de /admin
    sigue funcionando.
    """
    @app.context_processor
    def inyectar_kpis_biz():
        # Seguridad extrema: si CUALQUIER cosa falla, no rompemos la request
        try:
            if request.endpoint not in ENDPOINTS_CON_KPIS_BIZ or not _es_admin():
                return {}
            return {'kpis_biz': get_snapshot()}
        except Exception as exc:
            current_app.logger.warning('KPI-BIZ context processor fallo: %s', exc)
            return {}
        except BaseException as exc:
            # Nunca propagar KeyboardInterrupt, SystemExit, etc.
            current_app.logger.error('KPI-BIZ context processor error critico: %s', exc)
            return {}


# ─────────────────────────────────────────────
# ARRANQUE
# ─────────────────────────────────────────────


def init_kpi_biz(app):
    """
    Punto de entrada: instala hooks y asegura el esquema.

    Se llama desde app.py DESPUES de init_db, porque necesita que ALMACEN_DB
    ya este resuelto en la configuracion de la app.
    """
    # Crear tablas UNA SOLA VEZ al arrancar (síncrono, sin before_request)
    try:
        with app.app_context():
            schema.ensure_schema()
        app.logger.info('KPI-BIZ: esquema listo')
    except Exception as exc:
        app.logger.warning('KPI-BIZ: esquema no disponible al inicio: %s', exc)

    app.register_blueprint(kpi_biz_bp)
    registrar_context_processor(app)

    app.logger.info('KPI-BIZ: modulo inicializado')
    return kpi_biz_bp