"""
Instrumentacion: de donde salen los datos de los KPIs.

Registra tres cosas y ninguna mas:

1. Cada peticion (ruta, status, duracion) para los KPIs de rendimiento y
   disponibilidad.
2. Cada excepcion no controlada para el KPI de estabilidad.
3. Chequeos de salud (latencia de BD y memoria del proceso).

Principio de diseno importante: la instrumentacion NUNCA puede romper la
aplicacion. Todo lo que escriba en la base de datos va envuelto en un
try/except que se traga el fallo, porque un panel de metricas que tumba la
tienda seria un fallo mucho peor que el que dice medir.
"""
import atexit
import os
import platform
import queue
import threading
import time
from datetime import datetime

from flask import g, request, session

from . import repository, settings

# ─────────────────────────────────────────────
# BUFFER DE ESCRITURA
# ─────────────────────────────────────────────
# Escribir un INSERT por peticion convertiria la BD en el cuello de botella
# y, peor todavia, inflaria la propia metrica de rendimiento. Se acumulan
# los registros en memoria y un hilo los vuelca por lotes.
_buffer = queue.Queue()
_flusher_started = False
_start_lock = threading.Lock()


def _flush_once(app) -> None:
    """Vuelca a MySQL todo lo que haya acumulado en el buffer."""
    rows = []
    while True:
        try:
            rows.append(_buffer.get_nowait())
        except queue.Empty:
            break
    if not rows:
        return
    try:
        with app.app_context():
            repository.insert_request_metrics(rows)
    except Exception:
        # Si la escritura falla se descartan las filas: es preferible perder
        # metricas a dejar crecer la memoria sin control.
        #
        # Pero no en silencio. Este bloque era un `pass` puro, y asi se perdio
        # el fallo que hacia que get_cursor() no encontrara la conexion: el
        # buffer se vaciaba, las filas nunca llegaban a la tabla y no habia ni
        # una linea en el log que lo explicara. Un fallo silencioso en la
        # instrumentacion es indistinguible de "todo va bien" desde fuera.
        #
        # Se registra con app.logger y no con current_app.logger porque aqui
        # ya se ha salido del contexto de aplicacion.
        app.logger.error(
            'kpi: no se pudieron guardar %d metricas; se descartan',
            len(rows),
            exc_info=True,
        )


def _flusher_loop(app) -> None:
    """Vuelca el buffer cada FLUSH_INTERVAL_SECONDS, vacie o no se haya lleno."""
    while True:
        time.sleep(settings.FLUSH_INTERVAL_SECONDS)
        _flush_once(app)


def start_flusher(app) -> None:
    """Arranca el hilo de volcado una sola vez por proceso."""
    global _flusher_started
    with _start_lock:
        if _flusher_started:
            return
        _flusher_started = True
        thread = threading.Thread(
            target=_flusher_loop, args=(app,), daemon=True,
            name='kpi-flusher')
        thread.start()
        # Al apagar el proceso se vuelca lo que quede en el buffer.
        atexit.register(_flush_once, app)


def _is_excluded(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in settings.EXCLUDED_PATHS)


def _current_route() -> str:
    """
    Nombre del endpoint en vez de la ruta literal.

    request.endpoint agrupa /producto/1 y /producto/2 bajo 'producto', lo
    que mantiene baja la cardinalidad de la tabla y hace correctos los
    agregados por pagina.
    """
    endpoint = request.endpoint
    if endpoint and not endpoint.startswith('static'):
        return endpoint
    return request.path[:120]


# ─────────────────────────────────────────────
# HOOKS DE FLASK
# ─────────────────────────────────────────────
def register_hooks(app) -> None:
    """Instala los hooks que alimentan el modulo de KPIs."""
    start_flusher(app)

    @app.before_request
    def _kpi_mark_start():
        # perf_counter es monotono: immune a cambios de reloj del sistema,
        # a diferencia de time.time().
        g._kpi_started_at = time.perf_counter()

    @app.after_request
    def _kpi_record_request(response):
        started = getattr(g, '_kpi_started_at', None)
        if started is None or _is_excluded(request.path):
            return response

        duration_ms = (time.perf_counter() - started) * 1000.0
        # La marca de tiempo se toma aqui, cuando se sirvio la peticion, y no
        # al vaciar el buffer: un lote puede esperar hasta FLUSH_INTERVAL_SECONDS
        # y la fila debe decir cuando ocurrio, no cuando se escribio.
        #
        # Se usa el reloj de Python y no el DEFAULT CURRENT_TIMESTAMP de MySQL
        # a proposito. El calculo de periodos y la purga comparan contra
        # datetime.now() de Python, asi que si las marcas las pusiera el
        # servidor MySQL y viviera en otra zona horaria, las peticiones mas
        # recientes caerian fuera de la ventana 'hasta' y el panel mostraria
        # cero con trafico real. Un solo reloj en todo el modulo.
        _buffer.put((
            _current_route(),
            request.method[:10],
            int(response.status_code),
            round(duration_ms, 2),
            (session.get('rol') or None),
            datetime.now(),
        ))
        return response

    @app.teardown_request
    def _kpi_flush_if_full(exc):
        """Si el buffer se llena mucho (pico de trafico), volcamos ya."""
        try:
            if _buffer.qsize() >= settings.FLUSH_BATCH_SIZE:
                from flask import current_app
                _flush_once(current_app._get_current_object())
        except Exception:
            pass


def persist_error(error, origen='servidor'):
    """
    Guarda una excepcion para el KPI de estabilidad.

    No se registra como errorhandler propio a proposito: Flask resuelve el
    manejador de una excepcion con el primero que encuentra, y app.py ya
    define uno global. Si este modulo declarara el suyo, el suyo no se
    ejecutaria nunca. Por eso app.py llama a esta funcion desde dentro de su
    manejador existente, que es ademas el punto correcto: se registra el
    error antes de decidir que mensaje se le devuelve al usuario.

    Nunca propaga excepciones: registrar una metrica no puede romper la
    atencion de la peticion que estaba fallando.
    """
    try:
        from flask import request
        route = _current_route() if request else None
    except Exception:
        route = None
    try:
        repository.insert_error_event(
            route=route,
            error_type=type(error).__name__,
            mensaje=str(error),
            origen=origen,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
# CHEQUEO DE SALUD
# ─────────────────────────────────────────────
def read_process_memory_kb():
    """
    Memoria residente del proceso leida de /proc (solo en Linux).

    En Render el contenedor es Linux, asi que esto da una medida real y
    gratuita del consumo. Si no existe /proc se devuelve None en vez de
    inventar un valor.
    """
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def run_health_check(app, persist=True) -> dict:
    """
    Mide la latencia real de la BD y comprueba que responde.

    Se usa tanto para el boton de 'Comprobar ahora' del panel como para el
    registro periodico.
    """
    resultado = {'ok': False, 'db_ms': 0.0, 'rss_kb': None, 'detalle': None}
    started = time.perf_counter()
    try:
        with app.app_context():
            cur = repository.get_cursor()
            try:
                cur.execute('SELECT 1')
                cur.fetchone()
            finally:
                cur.close()
        resultado['db_ms'] = round((time.perf_counter() - started) * 1000.0, 2)
        resultado['ok'] = True
    except Exception as exc:
        resultado['db_ms'] = round((time.perf_counter() - started) * 1000.0, 2)
        resultado['detalle'] = f'BD no disponible: {type(exc).__name__}'
        if persist:
            _try_persist_health(app, resultado)

    resultado['rss_kb'] = read_process_memory_kb()

    if persist and resultado['ok']:
        _try_persist_health(app, resultado)
    return resultado


def _try_persist_health(app, resultado) -> None:
    try:
        with app.app_context():
            repository.insert_health_check(
                ok=resultado['ok'],
                db_ms=resultado['db_ms'],
                rss_kb=resultado['rss_kb'] or 0,
                python_version=platform.python_version(),
                detalle=resultado['detalle'],
            )
    except Exception:
        pass


def record_boot_event(app) -> None:
    """
    Registra el arranque del proceso.

    Sirve para saber cuando hubo un reinicio (un despliegue, un crash loop o
    un cold start de Render), que es el dato mas cercano a 'caida' que se
    puede obtener desde dentro de la aplicacion.
    """
    try:
        with app.app_context():
            repository.insert_health_check(
                ok=True,
                db_ms=0.0,
                rss_kb=read_process_memory_kb() or 0,
                python_version=platform.python_version(),
                detalle=(
                    f'arranque pid={os.getpid()} '
                    f'python={platform.python_version()}'
                ),
            )
    except Exception:
        pass
