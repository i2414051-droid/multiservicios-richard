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
        app.logger.error(
            'kpi: no se pudieron guardar %d metricas; se descartan',
            len(rows),
            exc_info=True,
        )


def _flusher_loop(app) -> None:
    """Vuelca el buffer cada FLUSH_INTERVAL_SECONDS, vacie o no se haya llenado."""
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
        atexit.register(_flush_once, app)


def _is_excluded(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in settings.EXCLUDED_PATHS)


def _current_route() -> str:
    endpoint = request.endpoint
    if endpoint and not endpoint.startswith('static'):
        return endpoint
    return request.path[:120]


# ─────────────────────────────────────────────
# HOOKS DE FLASK
# ─────────────────────────────────────────────
def register_hooks(app) -> None:
    start_flusher(app)

    @app.before_request
    def _kpi_mark_start():
        g._kpi_started_at = time.perf_counter()

    @app.after_request
    def _kpi_record_request(response):
        started = getattr(g, '_kpi_started_at', None)
        if started is None or _is_excluded(request.path):
            return response

        duration_ms = (time.perf_counter() - started) * 1000.0
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
        try:
            if _buffer.qsize() >= settings.FLUSH_BATCH_SIZE:
                from flask import current_app
                _flush_once(current_app._get_current_object())
        except Exception:
            pass


def persist_error(error, origen='servidor'):
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
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def run_health_check(app, persist=True) -> dict:
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