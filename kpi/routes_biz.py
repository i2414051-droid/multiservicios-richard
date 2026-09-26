"""
Rutas del modulo de KPIs de Ventas y Almacen.

Se registra como blueprint 'kpi_biz' para no mezclar con los KPIs tecnicos
existentes. Todas las rutas exigen sesion de administrador.

El panel vive en /kpi-biz/panel y se puede abrir en ventana completa desde
cualquier pagina con el boton flotante. El fragmento que se incrusta en
/admin usa el mismo endpoint que el panel completo, solo cambia la plantilla
base.
"""
import datetime
import decimal

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
PANEL_FRAGMENTO = 'kpi_biz/_bloque.html'      # se incrusta en /admin
PANEL_COMPLETO = 'kpi_biz/panel.html'         # ventana completa

ENDPOINTS_CON_KPIS_BIZ = frozenset({'admin', 'kpi_biz.api_panel'})


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
def get_snapshot():
    """Punto unico de construccion del snapshot de negocio.

    Nunca lanza excepciones: si algo falla, devuelve estructura vacia
    para que el panel muestre "sin datos" en lugar de 500.
    """
    try:
        periodo = service.resolve_period(request.args)
        snapshot = service.build_snapshot(periodo)
        return snapshot
    except Exception as exc:
        current_app.logger.warning('KPI-BIZ get_snapshot fallo: %s', exc)
        # Estructura minima que la plantilla espera
        return {
            'periodo': {'preset': '30d', 'dias': 30},
            'exito': {},
            'cancelaciones': {},
            'problemas': {},
            'resumen': {},
            'tendencia': [],
            'almacen': {},
            'generado_en': datetime.datetime.now(),
        }


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
        return render_template(PANEL_FRAGMENTO, kpis=get_snapshot())
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
    return render_template(PANEL_COMPLETO, kpis=get_snapshot())


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