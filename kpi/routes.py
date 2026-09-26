"""
Rutas del modulo de KPIs.

Se registra como blueprint para no anadir logica a app.py, que ya tiene mas
de 2.700 lineas. Todas las rutas exigen sesion de administrador.

El panel se pinta dentro de /admin, que es su unica ubicacion. Para no
duplicar ese render, /kpis redirige ahi y un context processor inyecta el
snapshot en la plantilla que lo contiene.
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
)

from . import collectors, repository, retention, service

kpi_bp = Blueprint('kpi', __name__)

# Plantilla del fragmento que se incrusta en /admin. Es la unica que pinta
# indicadores, de modo que no puede haber dos versiones desincronizadas.
PANEL_FRAGMENTO = 'kpi/_bloque.html'

# Endpoints cuyas plantillas reciben el snapshot. La lista es explicita y no
# un filtro "si parece de administracion": cualquier otra pagina de la tienda
# (el indice, el carrito, el login) se ahorra las consultas de los KPIs.
ENDPOINTS_CON_KPIS = frozenset({'admin', 'kpi.api_kpis_panel'})


def _es_admin():
    """
    Indica si la peticion viene de un administrador.

    Se lee el proxy global `session` de Flask y no `request.session`. En
    Flask 3.1 el atributo `session` del objeto Request lanza AttributeError
    siempre, con cookie o sin ella, asi que usarlo devolvia un 500 en todas
    las rutas del modulo. El resto del proyecto tambien usa el proxy global.
    """
    return flask_session.get('rol') in ('admin', 'administrador')


def _no_autorizado():
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Acceso denegado'}), 403
    return 'Acceso denegado', 403


def _json_safe(valor):
    """
    Convierte la estructura del snapshot a tipos que jsonify sabe serializar.

    El dominio trabaja con datetime de verdad porque las plantillas los
    formatean con strftime y las consultas los necesitan como parametros.
    Traducirlos aqui, en la frontera del API, evita un 500 en produccion
    sin tener que deformar la logica de negocio.
    """
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


def get_snapshot():
    """
    Punto unico de construccion del snapshot.

    Lo consumen el context processor de /admin y el endpoint del fragmento; el
    coste se paga una sola vez por peticion porque Flask reutiliza el contexto.

    Nunca lanza excepciones: devuelve estructura vacia en caso de fallo.
    """
    try:
        periodo = service.resolve_period(request.args)
        snapshot = service.build_snapshot(periodo)
        snapshot['retention'] = retention.describe_retention()
        return snapshot
    except Exception as exc:
        current_app.logger.warning('KPI get_snapshot fallo: %s', exc)
        return {
            'periodo': {'preset': '30d', 'dias': 30},
            'disponibilidad': {},
            'rendimiento': {},
            'estabilidad': {},
            'seguridad': {},
            'tendencia': [],
            'salud': {},
            'operaciones': [],
            'retention': 'N/A',
            'generado_en': datetime.datetime.now(),
        }


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────
@kpi_bp.route('/api/kpis')
def api_kpis():
    """Snapshot completo de KPIs en JSON, con el periodo pedido en la query."""
    if not _es_admin():
        return _no_autorizado()
    try:
        return jsonify({'ok': True, 'kpis': _json_safe(get_snapshot())})
    except Exception as exc:
        current_app.logger.error('KPI: fallo al construir el snapshot: %s', exc)
        return jsonify({'ok': False, 'error': str(exc)}), 500


@kpi_bp.route('/api/kpis/salud', methods=['GET', 'POST'])
def api_salud():
    """Dispara un chequeo de salud al momento y lo persiste."""
    if not _es_admin():
        return _no_autorizado()
    resultado = collectors.run_health_check(current_app, persist=True)
    return jsonify({'ok': True, 'salud': resultado})


@kpi_bp.route('/api/kpis/purgar', methods=['POST'])
def api_purgar():
    """Fuerza la purga de metricas antiguas."""
    if not _es_admin():
        return _no_autorizado()
    borrado = retention.maybe_purge(current_app, force=True)
    return jsonify({'ok': True, 'borrado': borrado})


@kpi_bp.route('/kpis')
def panel_kpis():
    """
    El panel vive en /admin. Esta ruta se mantiene para no romper los enlaces
    y marcadores que ya apuntan aqui, y lleva a su unica ubicacion real.
    """
    if not _es_admin():
        return _no_autorizado()
    return redirect('/admin#kpiPanel', code=302)


@kpi_bp.route('/api/kpis/panel')
def api_kpis_panel():
    """
    Devuelve solo el fragmento del panel, ya renderizado.

    Es lo que pide el navegador al cambiar de periodo. Deliberadamente NO
    devuelve la pagina anfitriona: si lo hiciera, cambiar el periodo de los
    KPIs recargaria la tabla de productos y las consultas de /admin enteras.
    """
    if not _es_admin():
        return _no_autorizado()
    try:
        return render_template(PANEL_FRAGMENTO, kpis=get_snapshot())
    except Exception as exc:
        current_app.logger.error('KPI: fallo al construir el panel: %s', exc)
        return (
            '<div class="kpi-error visible">No se pudieron calcular los '
            'indicadores. Los datos mostrados son los de la ultima carga '
            'correcta.</div>',
            500,
        )


# ─────────────────────────────────────────────
# INYECCION EN LAS PLANTILLAS QUE LO MUESTRAN
# ─────────────────────────────────────────────
def registrar_context_processor(app):
    """
    Pone `kpis` a disposicion de las plantillas de ENDPOINTS_CON_KPIS.

    Es lo que evita tocar app.py: sus vistas siguen pasando sus variables
    como siempre y el bloque aparece sin que el monolith sepa que los KPIs
    existen. El coste se paga una vez por peticion, porque Flask reutiliza el
    contexto mientras se resuelve la plantilla.

    Devolver {} en vez de propagar el error es deliberado: si la base de datos
    no permite calcular los indicadores, /admin tiene que seguir mostrando la
    gestion de productos. El fragmento se dibuja entonces en su estado de
    degradacion, que explica el motivo.
    """
    @app.context_processor
    def inyectar_kpis():
        # Seguridad extrema: si CUALQUIER cosa falla, no rompemos la request
        try:
            if request.endpoint not in ENDPOINTS_CON_KPIS or not _es_admin():
                return {}
            return {'kpis': get_snapshot()}
        except Exception as exc:
            current_app.logger.warning('KPI context processor fallo: %s', exc)
            return {}
        except BaseException as exc:
            current_app.logger.error('KPI context processor error critico: %s', exc)
            return {}


# ─────────────────────────────────────────────
# ARRANQUE
# ─────────────────────────────────────────────


def init_kpi(app, ensure_schema_fn=None):
    """
    Punto de entrada del modulo: instala hooks y asegura el esquema.

    ensure_schema_fn permite que app.py reutilice su init_db() ya existente
    en lugar de crear un segundo camino de inicializacion de la BD.
    """
    # Crear tablas UNA SOLA VEZ al arrancar (sin before_request)
    try:
        with app.app_context():
            if ensure_schema_fn is not None:
                ensure_schema_fn()
            repository.ensure_schema()
        app.logger.info('KPI: esquema listo')
    except Exception as exc:
        # Sin tablas el modulo no registra nada, pero la app arranca igual:
        # la instrumentacion nunca debe tumbar el servicio.
        app.logger.warning('KPI: esquema no disponible al inicio: %s', exc)

    collectors.register_hooks(app)
    app.register_blueprint(kpi_bp)
    registrar_context_processor(app)

    collectors.record_boot_event(app)
    app.logger.info('KPI: modulo inicializado')
    return kpi_bp