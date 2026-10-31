# ─────────────────────────────────────────────
# DRIVER MySQL
# En produccion (Render) se usa mysqlclient, que expone el modulo `MySQLdb`.
# En local/Windows ese driver necesita compilador y librerias nativas, asi que
# si `MySQLdb` no esta disponible se cae a PyMySQL (Python puro) instalando el
# mismo alias. Flask-MySQLdb y el modulo de KPI importan `MySQLdb`, por lo que
# el alias debe quedar registrado ANTES de importarlos.
# ─────────────────────────────────────────────
try:
    import MySQLdb  # noqa: F401
except ImportError:  # pragma: no cover - depende del entorno
    import pymysql
    pymysql.install_as_MySQLdb()

# Carga el .env local si existe. En Render no hay archivo .env (las variables
# llegan por el panel de entorno), por lo que esta llamada es un no-op alli.
# Es lo que permite que `python app.py` funcione en local sin exportar nada.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv no instalado: se usan las variables del sistema
    pass

import os, uuid, io, csv, json, threading, secrets, time, re, logging, traceback
from datetime import datetime, timedelta, timezone, date
from functools import wraps
from urllib.parse import quote, urlencode

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, send_file, Response, g, abort)
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

# Modulo de KPIs de negocio (ventas y almacen). Se registra mas abajo,
# despues de crear la app, porque necesita el esquema de la BD ya disponible.
from kpi import init_kpi_biz
from kpi import registro as kpi_reg


def _invalidar_cache_kpi():
    """Fuerza a que el panel refleje un cambio de venta/entrega al instante.

    El panel guarda el snapshot en cache. Se invalida desde las rutas que
    escriben ventas o seguimiento_entregas, no desde el refresco automatico.
    """
    try:
        from kpi.routes_biz import invalidar_cache
        invalidar_cache()
    except Exception:
        # Un fallo de cache nunca debe tumbar la operacion de negocio.
        pass

logging.basicConfig(level=logging.ERROR)

from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, KeepTogether)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

# ─────────────────────────────────────────────
# HORA DE PERU
# ─────────────────────────────────────────────
# Las fechas de negocio se guardan naive: lo que se escribe es lo que se lee.
# El servidor de Render corre en UTC, asi que datetime.now() ahi devuelve una
# hora 5 horas adelantada respecto a Peru. Por eso la hora de negocio se
# calcula siempre desde UTC hacia Lima (Peru no tiene horario de verano).
ZONA_LIMA = timezone(timedelta(hours=-5))


def _ahora_lima():
    """Hora actual de Peru como datetime naive (sin tzinfo)."""
    return datetime.now(timezone.utc).astimezone(ZONA_LIMA).replace(tzinfo=None)


# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────
app = Flask(__name__)

# SECRET_KEY firma la cookie de sesion. Si falta, cada proceso genera una
# distinta: con varios workers (o varias instancias) el usuario se desloguea al
# azar y los tokens CSRF dejan de valer. Por eso se avisa fuerte en el log.
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    logging.getLogger(__name__).warning(
        'SECRET_KEY no esta definida: se usa una clave aleatoria por proceso. '
        'Con varios workers las sesiones se invalidan entre si. Definela en el entorno.')
    _secret_key = os.urandom(32)
app.secret_key = _secret_key

# Detras del proxy de Render el cliente real llega en X-Forwarded-For/Proto.
# Sin esto request.remote_addr es el proxy: todos los visitantes compartirian
# IP (el bloqueo por IP castigaria a todos) y la cookie Secure no se pondria.
if os.environ.get('RENDER') or os.environ.get('TRAS_PROXY') == '1':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Cookies de sesion: fuera del alcance de JavaScript, sin viajar en peticiones
# cruzadas y solo por HTTPS cuando la app esta publicada.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=(os.environ.get(
        'SESSION_COOKIE_SECURE',
        '1' if os.environ.get('RENDER') else '0') == '1'),
    # Tope de subida: una foto de movil ronda 5-10 MB. 16 MB deja margen y
    # evita que un cliente mande un archivo gigante y agote la memoria.
    MAX_CONTENT_LENGTH=int(os.environ.get('MAX_CONTENT_MB', '16')) * 1024 * 1024,
)

bcrypt = Bcrypt(app)

# ─────────────────────────────────────────────
# CSRF PROTECTION
# ─────────────────────────────────────────────
def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']

app.jinja_env.globals['csrf_token'] = generate_csrf_token

def validate_csrf():
    token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    esperado = session.get('_csrf_token')
    if not token or not esperado:
        return False
    # compare_digest evita filtrar el token por el tiempo de comparacion.
    return secrets.compare_digest(str(token), str(esperado))


# ─────────────────────────────────────────────
# CSRF GLOBAL Y LIMITE DE PETICIONES
# ─────────────────────────────────────────────
# Todo lo que escribe (POST/PUT/PATCH/DELETE) exige token, incluidas las rutas
# que antes se olvidaban de comprobarlo. Sin esto, un sitio externo podia
# lanzar operaciones con la sesion abierta del usuario.
_METODOS_SEGUROS = ('GET', 'HEAD', 'OPTIONS', 'TRACE')
_ENDPOINTS_SIN_CSRF = frozenset({'static'})

# Limite por IP y endpoint, en ventana fija de 60 s. Es por proceso: con dos
# workers el limite efectivo se duplica, pero sigue frenando la fuerza bruta y
# las rafagas antes de que lleguen a bcrypt o a la BD.
_RATE_LOCK = threading.Lock()
_RATE = {}
_RATE_MAX_CLAVES = 20000
_LIMITES = {
    'login': (12, 60),
    'registro': (10, 60),
    'recuperar_password': (5, 60),
    'restablecer_password': (5, 60),
    'api_consultar_documento': (30, 60),
}
_LIMITE_POST_GENERAL = (120, 60)


def _rate_permitido(clave, limite, ventana):
    """True si la clave no supero `limite` en la ventana actual."""
    ahora = time.time()
    with _RATE_LOCK:
        inicio, cuenta = _RATE.get(clave, (ahora, 0))
        if ahora - inicio >= ventana:
            inicio, cuenta = ahora, 0
        cuenta += 1
        _RATE[clave] = (inicio, cuenta)
        if len(_RATE) > _RATE_MAX_CLAVES:
            for k in [k for k, (t, _) in _RATE.items() if ahora - t >= ventana]:
                _RATE.pop(k, None)
    return cuenta <= limite


def _respuesta_429(ventana):
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'ok': False,
                        'error': 'Demasiadas solicitudes. Intenta en un minuto.'}), \
            429, {'Retry-After': str(ventana)}
    return ('<!doctype html><html lang="es"><meta charset="utf-8">'
            '<h1>Demasiadas solicitudes</h1>'
            '<p>Espera un minuto e intenta de nuevo.</p>'), \
        429, {'Retry-After': str(ventana),
              'Content-Type': 'text/html; charset=utf-8'}


@app.before_request
def _limitar_peticiones():
    if request.method in _METODOS_SEGUROS:
        return None
    ip = request.remote_addr or 'desconocida'
    limite, ventana = _LIMITE_POST_GENERAL
    if not _rate_permitido('todo:%s' % ip, limite, ventana):
        return _respuesta_429(ventana)
    regla = _LIMITES.get(request.endpoint)
    if regla and not _rate_permitido('%s:%s' % (request.endpoint, ip),
                                     regla[0], regla[1]):
        return _respuesta_429(regla[1])
    return None


def _referer_local():
    ref = request.referrer
    return bool(ref) and ref.startswith(request.host_url)


# Rutas GET que escriben en la BD (borrados, carrito, checkout...). Un sitio
# externo podia dispararlas con solo conseguir que el usuario pulsara un
# enlace. Sec-Fetch-Site lo distingue; los navegadores que no lo mandan siguen
# funcionando igual.
_ENDPOINTS_GET_ESCRITURA = frozenset({
    'eliminar_producto', 'activar_producto', 'eliminar_producto_definitivo',
    'consultar', 'ocultar_venta', 'mostrar_venta', 'limpiar_historial',
    'eliminar_proveedor', 'eliminar_pedido', 'marcar_enviado',
    'eliminar_ingreso', 'eliminar_entrega', 'eliminar_salida',
    'eliminar_direccion', 'checkout', 'eliminar_carrito',
    'aumentar_cantidad', 'agregar', 'ruta_init_db',
})


@app.before_request
def _bloquear_get_cruzado():
    if request.method != 'GET':
        return None
    if request.endpoint not in _ENDPOINTS_GET_ESCRITURA:
        return None
    if (request.headers.get('Sec-Fetch-Site') or '').lower() == 'cross-site':
        abort(403)
    return None


@app.before_request
def _exigir_csrf():
    if request.method in _METODOS_SEGUROS:
        return None
    if request.endpoint in _ENDPOINTS_SIN_CSRF:
        return None
    if validate_csrf():
        return None
    if (request.path.startswith('/api/')
            or request.is_json
            or request.headers.get('X-Requested-With') == 'fetch'):
        return jsonify({'ok': False,
                        'error': 'Token CSRF invalido o vencido.'}), 400
    flash('Tu sesion vencio o el formulario no es valido. Intenta de nuevo.', 'danger')
    return redirect(request.referrer if _referer_local() else '/')


@app.after_request
def _cabeceras_seguridad(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy',
                            'geolocation=(), microphone=(), camera=()')
    # CSP minima: no toca scripts ni estilos (la app usa mucho inline), pero
    # impide enmarcar el sitio (clickjacking) y secuestrar la base del documento.
    # form-action debe permitir los dominios que usa Culqi3DS: la libreria envia
    # formularios a Cardinal (device data collection y challenge 3DS). Sin esto
    # el navegador bloquea el 3DS y el pago con tarjeta 3DS nunca termina.
    resp.headers.setdefault(
        'Content-Security-Policy',
        "frame-ancestors 'self'; base-uri 'self'; "
        "form-action 'self' https://*.culqi.com "
        "https://*.cardinalcommerce.com https://*.cardinaltrusted.com")
    if request.is_secure:
        resp.headers.setdefault('Strict-Transport-Security',
                                'max-age=31536000; includeSubDomains')
    # Paginas con datos del usuario: que no queden en la cache del navegador
    # (boton "atras" despues de cerrar sesion).
    if not request.path.startswith('/static/'):
        resp.headers.setdefault('Cache-Control', 'no-store')
    elif request.path.startswith('/static/imagenes/'):
        # Nombres con hash: se pueden cachear de forma agresiva.
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def _thumb_imagen(imagen):
    """Ruta de la miniatura para listados; el detalle usa la imagen completa."""
    if isinstance(imagen, str) and imagen.startswith('imagenes/'):
        return 'imagenes/thumbs/' + imagen[len('imagenes/'):]
    return imagen


app.jinja_env.filters['thumb'] = _thumb_imagen

# ─────────────────────────────────────────────
# MYSQL (env vars para Render)
# ─────────────────────────────────────────────
app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_PORT']        = int(os.environ.get('MYSQL_PORT', 3306))
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', '')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard')
app.config['MYSQL_CHARSET']     = os.environ.get('MYSQL_CHARSET', 'utf8mb4')
app.config['MYSQL_CONNECT_TIMEOUT'] = int(os.environ.get('MYSQL_CONNECT_TIMEOUT', 5))
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

# ─────────────────────────────────────────────
# POOL DE CONEXIONES MYSQL
# ─────────────────────────────────────────────
# flask_mysqldb abre y cierra una conexion POR REQUEST. Contra una BD remota
# cada conexion cuesta ~930 ms (TCP + handshake + auth) y cada consulta ~190 ms
# de ida y vuelta, asi que la conexion se cachea por hilo y se reutiliza.
#
# Reglas que mantiene este bloque, para no cambiar el comportamiento:
#   * Una conexion por hilo: MySQLdb no es thread-safe.
#   * Si la peticion dejo una transaccion sin commit, se hace ROLLBACK al
#     terminar (antes eso lo hacia implicitamente el close()).
#   * Si la conexion murio (wait_timeout, reinicio del servidor), se descarta
#     y se abre otra: nunca se entrega un socket muerto.
#   * SQL_TIMING=1 registra en el log cuantas consultas hizo cada request y
#     cuanto tiempo de BD consumio. Apagado por defecto.
_POOL_PING_TRAS = 60          # segundos de inactividad antes de comprobar
_POOL_MAX = int(os.environ.get('MYSQL_POOL_SIZE', 8))
_SQL_TIMING = os.environ.get('SQL_TIMING') == '1'
_DML = ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')

_pool_libre = []                              # conexiones ociosas
_pool_lock = threading.Lock()
_pool_cupo = threading.BoundedSemaphore(_POOL_MAX)   # tope total de conexiones
_mysql_conectar = type(mysql).connect.fget


class _CursorContado:
    """Cursor que delega en el real y anota lo que cuesta cada consulta."""

    __slots__ = ('_cur', '_dueno')

    def __init__(self, cur, dueno):
        self._cur = cur
        self._dueno = dueno

    def execute(self, sql, *args, **kwargs):
        inicio = time.perf_counter()
        try:
            self._dueno._preparar(sql)
            return self._cur.execute(sql, *args, **kwargs)
        finally:
            self._dueno._anotar(sql, time.perf_counter() - inicio)

    def executemany(self, sql, *args, **kwargs):
        inicio = time.perf_counter()
        try:
            self._dueno._preparar(sql)
            return self._cur.executemany(sql, *args, **kwargs)
        finally:
            self._dueno._sumar_tiempo(time.perf_counter() - inicio)

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, nombre):
        return getattr(self._cur, nombre)


class _ConexionCacheada:
    """Proxy de la conexion real que recuerda si quedo algo sin commitear."""

    __slots__ = ('_conn', 'sucia', 'en_transaccion', 'cerrada', 'ultimo_uso',
                 'consultas', 'ms_bd', 'lentas')

    def __init__(self, conn):
        self._conn = conn
        self.sucia = False
        self.en_transaccion = False
        self.cerrada = False
        self.ultimo_uso = time.time()
        self.consultas = 0
        self.ms_bd = 0.0
        self.lentas = []

    # ── API que usa la app ──
    def cursor(self, *args, **kwargs):
        return _CursorContado(self._conn.cursor(*args, **kwargs), self)

    def commit(self):
        self._conn.commit()
        self.sucia = False
        self.en_transaccion = False

    def rollback(self):
        self._conn.rollback()
        self.sucia = False
        self.en_transaccion = False

    def close(self):
        self.cerrada = True
        self._conn.close()

    def ping(self, *args, **kwargs):
        return self._conn.ping(*args, **kwargs)

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)

    # ── transacciones y metricas ──
    def _preparar(self, sql):
        """Abre la transaccion antes de la primera escritura de la peticion.

        Las conexiones viven en autocommit: una lectura no deja transaccion
        abierta, asi que no queda un snapshot viejo ni candados de metadatos
        que bloqueen un ALTER, y no hay que pagar un ROLLBACK por peticion.
        Si la peticion va a escribir, se abre una transaccion explicita para
        que las operaciones de varios pasos (venta + detalle + stock) sigan
        siendo atomicas; el commit que ya hace la app la cierra.
        """
        try:
            cabeza = sql.lstrip().split(None, 1)[0].upper()
        except Exception:
            return
        if cabeza in _DML and not self.en_transaccion:
            self._conn.begin()
            self.en_transaccion = True
            self.sucia = True

    def _marcar_escritura(self, sql):
        try:
            cabeza = sql.lstrip().split(None, 1)[0].upper()
        except Exception:
            return
        if cabeza in _DML:
            self.sucia = True

    def _sumar_tiempo(self, segundos):
        if _SQL_TIMING:
            self.consultas += 1
            self.ms_bd += segundos * 1000.0

    def _anotar(self, sql, segundos):
        self._marcar_escritura(sql)
        if _SQL_TIMING:
            ms = segundos * 1000.0
            self.consultas += 1
            self.ms_bd += ms
            self.lentas.append((ms, ' '.join(str(sql).split())[:160]))
            if len(self.lentas) > 40:
                self.lentas = sorted(self.lentas, reverse=True)[:20]


def _mysql_obtener():
    """Saca una conexion del pool (o abre una nueva si hay cupo)."""
    if not _pool_cupo.acquire(timeout=15):
        raise RuntimeError(
            'pool MySQL agotado: %d conexiones en uso (MYSQL_POOL_SIZE)'
            % _POOL_MAX)

    conn = None
    with _pool_lock:
        if _pool_libre:
            conn = _pool_libre.pop()

    if conn is not None:
        try:
            if conn.cerrada:
                raise RuntimeError('conexion cerrada')
            if conn.en_transaccion:
                conn.rollback()
            if (time.time() - conn.ultimo_uso) > _POOL_PING_TRAS:
                conn.ping(True)
            if _SQL_TIMING:
                conn.consultas, conn.ms_bd, conn.lentas = 0, 0.0, []
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    try:
        conn = _ConexionCacheada(_mysql_conectar(mysql))
        # Autocommit a nivel de conexion: ver _ConexionCacheada._preparar.
        conn.autocommit(True)
    except Exception:
        _pool_cupo.release()
        raise
    return conn


def _mysql_liberar(exc=None):
    """Devuelve la conexion al pool; descarta lo que quedo sin commit."""
    conn = g.pop('mysql_db', None)
    if conn is None:
        return
    try:
        # Solo si quedo una escritura sin confirmar: en autocommit una lectura
        # no deja transaccion abierta, asi que no hay nada que deshacer ni
        # viaje de red que pagar.
        if conn.en_transaccion:
            conn.rollback()
    except Exception:
        conn.cerrada = True

    if not conn.cerrada:
        conn.ultimo_uso = time.time()
        sobra = False
        with _pool_lock:
            if len(_pool_libre) < _POOL_MAX:
                _pool_libre.append(conn)
            else:
                sobra = True
        if not sobra:
            conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _pool_cupo.release()


# Sustituye el connect-por-request de Flask-MySQLdb por el cacheado, y su
# teardown (que cerraba la conexion) por uno que solo hace rollback.
type(mysql).connect = property(lambda self: _mysql_obtener())
app.teardown_appcontext_funcs = [
    f for f in app.teardown_appcontext_funcs
    if getattr(f, '__self__', None) is not mysql
]
app.teardown_appcontext(_mysql_liberar)

if _SQL_TIMING:
    # Logger propio: basicConfig(level=ERROR) en la linea 43 filtraria los
    # warning, y este diagnostico tiene que verse sin tocar el log global.
    _log_sql = logging.getLogger('sql_timing')
    _log_sql.setLevel(logging.INFO)
    _log_sql.warning('SQL_TIMING ACTIVO (pool MySQL=%d)', _POOL_MAX)

    @app.before_request
    def _metricas_sql_inicio():
        g._sql_inicio = time.perf_counter()

    @app.after_request
    def _metricas_sql_fin(resp):
        conn = getattr(g, 'mysql_db', None)
        inicio = getattr(g, '_sql_inicio', time.perf_counter())
        total = (time.perf_counter() - inicio) * 1000.0
        if conn is not None:
            top = sorted(conn.lentas, reverse=True)[:3]
            detalle = ' || '.join('%.0fms %s' % (ms, sql) for ms, sql in top)
            _log_sql.warning(
                'SQL_TIMING %s %s :: request %.0f ms | %d consultas | BD %.0f ms || %s',
                request.method, request.path, total, conn.consultas, conn.ms_bd, detalle)
        else:
            _log_sql.warning(
                'SQL_TIMING %s %s :: request %.0f ms | sin acceso a BD',
                request.method, request.path, total)
        return resp

# Segunda base de datos (Gestión de Almacén): productos, proveedores y para pedir
MAIN_DB = os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard')
ALMACEN_DB = os.environ.get('MYSQL_DB_ALMACEN', 'proyecto_gestion_almacen')

# Columnas de `productos` para listados y fichas. Se excluyen a proposito
# imagen_datos/imagen_mime: son LONGBLOB (varios MB por producto) y un
# `SELECT *` los descarga TODOS en cada portada/carrito. Contra una BD
# remota eso costaba ~2 s por pagina. El catalogo se sirve como archivo
# estatico desde /static/imagenes/... y las subidas nuevas desde
# /static/uploads/... con respaldo en la BD (_servir_imagen_desde_db).
COLS_PRODUCTO = ('id, nombre, descripcion, precio, stock, categoria, '
                 'imagen, estado, created_at')
# Guard: la base de almacén NUNCA debe ser la base principal. Si la variable de
# entorno apunta a la BD principal, se fuerza usar la BD de almacén dedicada.
if ALMACEN_DB == MAIN_DB:
    ALMACEN_DB = 'proyecto_gestion_almacen'

# Guardar en config para que módulos (KPIs, etc.) puedan leerla
app.config['ALMACEN_DB'] = ALMACEN_DB

# ─────────────────────────────────────────────
# FLASK-MAIL (env vars para Render)
# ─────────────────────────────────────────────
app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER',   'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', '')
app.config['MAIL_TIMEOUT'] = int(os.environ.get('MAIL_TIMEOUT', 10))
mail = Mail(app)

TOKEN = os.environ.get('APIPERU_TOKEN', '')
# Consulta DNI/RUC (APIsPERU). El token se usa SOLO en el servidor: nunca se
# manda al navegador ni se renderiza en las plantillas.
APIPERU_URL = os.environ.get('APIPERU_URL',
                             'https://dniruc.apisperu.com/api/v1').rstrip('/')
APIPERU_TIMEOUT = float(os.environ.get('APIPERU_TIMEOUT', '9'))
APIPERU_CACHE_TTL = int(os.environ.get('APIPERU_CACHE_TTL', str(24 * 60 * 60)))
# Culqi
CULQI_PUBLIC_KEY = os.environ.get('CULQI_PUBLIC_KEY', '')
CULQI_SECRET_KEY = os.environ.get('CULQI_SECRET_KEY', '')
CULQI_RSA_ID = os.environ.get('CULQI_RSA_ID', '')
CULQI_RSA_PUBLIC_KEY = os.environ.get('CULQI_RSA_PUBLIC_KEY', '')
CULQI_CURRENCY = os.environ.get('CULQI_CURRENCY', 'PEN')
# API de cargos de Culqi. Separado en variable para poder apuntar a un entorno
# de pruebas propio sin tocar el codigo.
CULQI_API_BASE = os.environ.get('CULQI_API_BASE', 'https://api.culqi.com/v2').rstrip('/')
CULQI_TIMEOUT = float(os.environ.get('CULQI_TIMEOUT', '20'))
# Culqi no acepta cargos por debajo de S/ 3.00 (300 centimos).
CULQI_MONTO_MINIMO = int(os.environ.get('CULQI_MONTO_MINIMO', '300'))
# El Checkout de Culqi acepta Yape hasta S/ 2,000 por transaccion.
CULQI_YAPE_MONTO_MAXIMO = int(os.environ.get('CULQI_YAPE_MONTO_MAXIMO', '200000'))
# Si el checkout pidio un token y el banco exige 3DS, Culqi responde con un
# error de autenticacion. Se muestra un mensaje claro en vez del generico.
CULQI_MODO_PRUEBA = CULQI_PUBLIC_KEY.startswith('pk_test_')

# ─────────────────────────────────────────────
# reCAPTCHA v2 — casilla "No soy un robot"
# ─────────────────────────────────────────────
# Protege el unico formulario publico que crea cuentas: /registro. El login se
# dejo fuera a proposito (se pidio explicitamente): ahi la fuerza bruta ya la
# frena el bloqueo por intentos + IP y el rate limiting.
#
# La SITE KEY es publica por diseno: se renderiza en el HTML y el navegador la
# necesita para dibujar el widget. La SECRET KEY solo vive en el servidor
# (.env en local, Render -> Environment en produccion) y nunca se renderiza.
#
# Google exige que el dominio este registrado en la consola de reCAPTCHA
# (google.com/recaptcha/admin). Sin eso el widget muestra "Invalid site key" y
# siteverify responde invalid-input-response, aunque las claves sean correctas.
RECAPTCHA_SITE_KEY   = os.environ.get('RECAPTCHA_SITE_KEY', '').strip()
RECAPTCHA_SECRET_KEY = os.environ.get('RECAPTCHA_SECRET_KEY', '').strip()
RECAPTCHA_VERIFY_URL = os.environ.get(
    'RECAPTCHA_VERIFY_URL',
    'https://www.google.com/recaptcha/api/siteverify').rstrip('/')
RECAPTCHA_TIMEOUT = float(os.environ.get('RECAPTCHA_TIMEOUT', '9'))

# El captcha solo esta activo con las DOS claves. Si falta alguna, el widget no
# se dibuja y la validacion se omite: un entorno sin credenciales (por ejemplo
# una maquina nueva o un despliegue de pruebas) nunca queda bloqueado por
# configuracion incompleta.
RECAPTCHA_ACTIVO = bool(RECAPTCHA_SITE_KEY and RECAPTCHA_SECRET_KEY)

# La plantilla solo necesita la site key. Se expone como global de Jinja en vez
# de un context_processor: es un valor fijo del proceso, no depende del request.
app.jinja_env.globals['recaptcha_site_key'] = RECAPTCHA_SITE_KEY

CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']

# ─────────────────────────────────────────────
# ESTADOS DE CLIENTE (P0)
# ─────────────────────────────────────────────
# Catálogo de estados de cliente.
#
# Este dict es la ÚNICA fuente de verdad. Antes existía la tabla
# `estados_cliente` en la BD, pero no se leía en ningún request (cero
# SELECT), no tenía FKs apuntando a ella y solo se creaba y se sembraba al
# arrancar: una tabla fantasma que el código rellenaba por su cuenta y que
# el admin no podía controlar. Se eliminó por completo (tabla + código).
#
# El estado de cada cliente vive en `usuarios.estado`, que guarda el código
# de este catálogo; el historial de cambios en `historial_estado_cliente`.
ESTADOS_CLIENTE = {
    'activo':                 {'nombre': 'Activo',                 'color': '#28a745'},
    'inactivo':               {'nombre': 'Inactivo',               'color': '#6c757d'},
    'bloqueado':              {'nombre': 'Bloqueado',              'color': '#dc3545'},
    'pendiente_verificacion': {'nombre': 'Pendiente verificación', 'color': '#ffc107'},
    'moroso':                 {'nombre': 'Moroso',                 'color': '#fd7e14'},
}

def estado_cliente_valido(codigo):
    return codigo in ESTADOS_CLIENTE

def color_estado_cliente(codigo):
    return ESTADOS_CLIENTE.get(codigo, {}).get('color', '#6c757d')

app.jinja_env.globals['color_estado_cliente'] = color_estado_cliente
app.jinja_env.globals['estados_cliente'] = ESTADOS_CLIENTE

# ─────────────────────────────────────────────
# MOTIVOS DE CANCELACIÓN (catálogo único; la tabla
# motivos_cancelacion es la fuente de verdad y este
# dict solo da la etiqueta para selects/badges)
# ─────────────────────────────────────────────
MOTIVOS_CANCELACION = {
    'cambio_opinion':      'Cambié de opinión',
    'mejor_precio':        'Encontré mejor precio',
    'demora_entrega':      'Demora en la entrega',
    'error_pedido':        'Error en el pedido',
    'producto_equivocado': 'Producto equivocado',
    'otro':                'Otro',
}

# Etiqueta para cancelaciones sin fila en `cancelaciones` (p.ej. datos viejos
# o un INSERT que falló). No es un motivo del catálogo: solo evita que esas
# ventas desaparezcan del reporte.
MOTIVO_SIN_REGISTRO = 'sin_motivo'

def motivo_cancelacion_valido(codigo):
    return codigo in MOTIVOS_CANCELACION

def etiqueta_motivo(codigo):
    """Texto legible de un motivo, incluyendo el caso 'sin_motivo'."""
    if codigo == MOTIVO_SIN_REGISTRO:
        return 'Sin motivo registrado'
    return MOTIVOS_CANCELACION.get(codigo, codigo or 'Otro')

app.jinja_env.globals['motivos_cancelacion'] = MOTIVOS_CANCELACION

PER_PAGE = 15

def paginate_query(cur, sql_count, sql_data, params, page, per_page=PER_PAGE):
    cur.execute(sql_count, params)
    total = cur.fetchone()['total']
    total_pages = max(1, -(-total // per_page))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    cur.execute(sql_data + " LIMIT %s OFFSET %s", params + (per_page, offset))
    items = cur.fetchall()
    return items, total, page, total_pages

# ─────────────────────────────────────────────
# LIBRO DE RECLAMACIONES
# ─────────────────────────────────────────────
# Datos del proveedor que se imprimen en la hoja. Se pueden cambiar por
# variables de entorno sin tocar el codigo.
EMPRESA_INFO = {
    'nombre': os.environ.get('EMPRESA_NOMBRE', 'MULTISERVICIOS RICHARD'),
    'ruc': os.environ.get('EMPRESA_RUC', '10450119429'),
    'domicilio': os.environ.get('EMPRESA_DOMICILIO', 'Huancayo, Perú'),
    'correo': os.environ.get('EMPRESA_CORREO', ''),
}
DIAS_RESPUESTA_RECLAMO = int(os.environ.get('DIAS_RESPUESTA_RECLAMO', '15'))
app.jinja_env.globals['empresa_info'] = EMPRESA_INFO
app.jinja_env.globals['dias_respuesta_reclamo'] = DIAS_RESPUESTA_RECLAMO


def _fecha_legible(valor, formato='%d/%m/%Y'):
    """Formatea fechas de la BD aunque el driver devuelva texto."""
    if not valor:
        return '—'
    if hasattr(valor, 'strftime'):
        return valor.strftime(formato)
    return str(valor)

app.jinja_env.filters['fecha'] = _fecha_legible
app.jinja_env.filters['fechahora'] = lambda v: _fecha_legible(v, '%d/%m/%Y %H:%M')

# ─────────────────────────────────────────────
# INIT TABLAS NUEVAS
# ─────────────────────────────────────────────
# Dias que se guarda una fila de intentos_usuario sin actividad. Pasado el
# plazo la fila se borra: la tabla no tiene que ser un archivo indefinido de
# correos que alguien intento vulnear.
DIAS_LIMPIEZA_INTENTOS = 90


def _deduplicar_intentos(cur):
    """Deja una sola fila por correo en intentos_usuario.

    El login lee con fetchone() y despues hace UPDATE ... WHERE correo=%s, que
    afecta a TODAS las filas de ese correo. Si hubiera duplicados, el contador
    leido seria uno cualquiera y el UPDATE los pisaria a todos con el mismo
    valor, queda un estado incoherente.

    De cada correo se conserva la fila mas grave (la de mas intentos) y, a
    igualdad, la mas antigua, que es la que tiene el created_at util.
    """
    cur.execute("SELECT id, correo, intentos FROM intentos_usuario ORDER BY correo, intentos DESC, id ASC")
    filas = cur.fetchall() or []
    conservar = {}
    for f in filas:
        conservar.setdefault(f['correo'], f['id'])
    sobrantes = [f['id'] for f in filas if f['id'] != conservar[f['correo']]]
    for _id in sobrantes:
        cur.execute("DELETE FROM intentos_usuario WHERE id=%s", (_id,))
    return len(sobrantes)


def _backfill_entregas(cur):
    """Crea seguimiento para ventas que aun no lo tengan.

    Es idempotente: el LEFT JOIN solo selecciona ventas sin ninguna fila en
    `seguimiento_entregas`. Se ejecuta una vez al arrancar para que las ventas
    anteriores a esta funcionalidad aparezcan en /entregas y en el KPI.
    """
    cur.execute("""
        INSERT INTO seguimiento_entregas
            (venta_id, estado, direccion_envio, fecha_entrega, notas)
        SELECT v.id,
               CASE v.estado
                   WHEN 'entregado' THEN 'entregado'
                   WHEN 'cancelado' THEN 'cancelado'
                   ELSE 'pendiente'
               END,
               v.direccion_envio,
               CASE WHEN v.estado = 'entregado' THEN v.fecha ELSE NULL END,
               'Seguimiento creado al sincronizar ventas existentes.'
        FROM ventas v
        LEFT JOIN seguimiento_entregas s ON s.venta_id = v.id
        WHERE s.id IS NULL
    """)
    return cur.rowcount


def _respaldar_nombres_ingresos(cur):
    """Rellena la foto de nombres que aun no tienen los ingresos antiguos.

    El ingreso es un libro: el nombre del proveedor y del producto se guarda al
    registrar y no debe cambiar si despues se renombra o se borra esa fila.
    Mientras no haya foto (filas anteriores a esta regla) se toma el nombre
    actual, solo si la fila del proveedor/producto todavia existe. Es idempotente.
    """
    for sql in (
        f"""UPDATE {ALMACEN_DB}.ingresos i
            JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
            SET i.proveedor_nombre=p.nombre,
                i.proveedor_documento=CONCAT_WS(' ', p.tipo_documento, p.numero_documento)
            WHERE i.proveedor_nombre IS NULL OR i.proveedor_nombre=''""",
        f"""UPDATE {ALMACEN_DB}.detalle_ingreso di
            JOIN {ALMACEN_DB}.productos pr ON di.producto_id=pr.id
            SET di.producto_nombre=pr.nombre
            WHERE di.producto_nombre IS NULL OR di.producto_nombre=''""",
    ):
        try:
            cur.execute(sql)
        except Exception:
            # Si el alter todavia no puso la columna, se intenta en el próximo
            # arranque; no debe tumbar el resto de la inicializacion.
            pass


def init_db():
    global ALMACEN_DB
    try:
        _init_db_inner()
    except Exception as e:
        # Nunca propagar: si falla la creación de tablas, la app debe seguir
        app.logger.warning('init_db error interno (no crítico): %s', e)


def _init_db_inner():
    global ALMACEN_DB
    # Todo el cuerpo en un try/except global para nunca propagar
    try:
        # ── Base de datos principal ─────────────────────────────────────
        cur = mysql.connection.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id       INT AUTO_INCREMENT PRIMARY KEY,
                correo   VARCHAR(200) NOT NULL UNIQUE,
                password VARCHAR(255) NOT NULL,
                rol      VARCHAR(20) DEFAULT 'cliente',
                estado   VARCHAR(20) DEFAULT 'activo',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for sql in (
            "ALTER TABLE usuarios ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'",
            "ALTER TABLE usuarios ADD COLUMN recuperacion_token VARCHAR(255)",
            "ALTER TABLE usuarios ADD COLUMN recuperacion_expira DATETIME",
            "ALTER TABLE usuarios ADD COLUMN nombre VARCHAR(100)",
            "ALTER TABLE usuarios ADD COLUMN apellido VARCHAR(50)",
            "ALTER TABLE usuarios ADD COLUMN telefono VARCHAR(15)",
            "ALTER TABLE usuarios ADD COLUMN documento_tipo VARCHAR(5)",
            "ALTER TABLE usuarios ADD COLUMN documento_numero VARCHAR(15)",
            # La tabla real no tenia created_at (la creo una version previa), y
            # /perfil la seleccionaba -> el 500 que rompia la tuerca. Se agrega.
            "ALTER TABLE usuarios ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        # Libro de Reclamaciones (Ley 29571 y D.S. 011-2011-PCM). Vive en la BD
        # principal: es un registro legal, independiente del catalogo. `token`
        # permite que el consumidor vea su constancia sin exponer la de otros
        # (el codigo correlativo es adivinable; el token no).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reclamaciones (
                id                        INT AUTO_INCREMENT PRIMARY KEY,
                codigo                    VARCHAR(30) UNIQUE,
                token                     VARCHAR(64) UNIQUE,
                fecha                     DATETIME DEFAULT CURRENT_TIMESTAMP,
                tipo                      VARCHAR(20) NOT NULL DEFAULT 'reclamo',
                consumidor_nombre         VARCHAR(200) NOT NULL,
                consumidor_documento_tipo VARCHAR(20) DEFAULT 'DNI',
                consumidor_documento      VARCHAR(20),
                consumidor_domicilio      VARCHAR(300),
                consumidor_telefono       VARCHAR(20),
                consumidor_correo         VARCHAR(200),
                bien_tipo                 VARCHAR(20) DEFAULT 'producto',
                bien_descripcion          TEXT,
                bien_monto                DECIMAL(10,2),
                detalle                   TEXT NOT NULL,
                pedido                    TEXT,
                estado                    VARCHAR(20) DEFAULT 'pendiente',
                respuesta                 TEXT,
                fecha_respuesta           DATETIME,
                respondido_por            VARCHAR(150),
                fecha_limite              DATETIME,
                created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX ix_reclamaciones_estado (estado),
                INDEX ix_reclamaciones_fecha (fecha)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS carrito (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                usuario_id  VARCHAR(200) NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for sql in (
            "ALTER TABLE carrito ADD COLUMN cantidad INT DEFAULT 1",
            "ALTER TABLE carrito DROP FOREIGN KEY carrito_ibfk_1",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ventas (
                id         INT AUTO_INCREMENT PRIMARY KEY,
                cliente_id INT NOT NULL,
                total      DECIMAL(10,2),
                documento  VARCHAR(20),
                nombre     VARCHAR(200),
                estado     VARCHAR(20) DEFAULT 'en espera',
                fecha      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ¿La columna de pago ya existía? Si es la primera vez que se crea,
        # las ventas historicas quedan como pagadas (el flujo antiguo las daba
        # por cobradas). En arranques posteriores NO se toca nada, para no
        # marcar como pagadas las transferencias pendientes.
        cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='ventas'
                       AND COLUMN_NAME='estado_pago'""")
        _fila_pago = cur.fetchone()
        _tenia_estado_pago = bool(_fila_pago and _fila_pago['n'])
        for sql in (
            "ALTER TABLE ventas ADD COLUMN documento VARCHAR(20)",
            "ALTER TABLE ventas ADD COLUMN nombre VARCHAR(200)",
            "ALTER TABLE ventas ADD COLUMN estado VARCHAR(20) DEFAULT 'en espera'",
            "ALTER TABLE ventas ADD COLUMN metodo_pago VARCHAR(50)",
            "ALTER TABLE ventas ADD COLUMN direccion_envio TEXT",
            "ALTER TABLE ventas ADD COLUMN apellido VARCHAR(50)",
            "ALTER TABLE ventas ADD COLUMN telefono VARCHAR(15)",
            "ALTER TABLE ventas ADD COLUMN correo VARCHAR(100)",
            "ALTER TABLE ventas ADD COLUMN notas_entrega TEXT",
            "ALTER TABLE ventas ADD COLUMN comprobante_pago VARCHAR(255)",
            "ALTER TABLE ventas ADD COLUMN doc_tipo VARCHAR(10)",
            # 1 = oculta en paneles admin (no borra de BD). 0 = visible.
            "ALTER TABLE ventas ADD COLUMN oculta TINYINT DEFAULT 0",
            # Pago con Culqi: trazabilidad del cobro. `estado_pago` es distinto
            # de `estado` (que sigue el ciclo de entrega: en espera/entregado).
            "ALTER TABLE ventas ADD COLUMN estado_pago VARCHAR(20) DEFAULT 'pendiente'",
            "ALTER TABLE ventas ADD COLUMN culqi_charge_id VARCHAR(64)",
            "ALTER TABLE ventas ADD COLUMN culqi_token VARCHAR(64)",
            "ALTER TABLE ventas ADD COLUMN culqi_respuesta TEXT",
            "ALTER TABLE ventas ADD COLUMN pagado_en DATETIME NULL",
            # Un token y un cargo de Culqi no pueden asociarse a dos ventas.
            # Evita cobros/ventas duplicadas por doble clic o reintentos.
            "ALTER TABLE ventas ADD UNIQUE INDEX ux_ventas_culqi_token (culqi_token)",
            "ALTER TABLE ventas ADD UNIQUE INDEX ux_ventas_culqi_charge (culqi_charge_id)",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        if not _tenia_estado_pago:
            try:
                cur.execute("""UPDATE ventas SET estado_pago='pagado',
                               pagado_en=COALESCE(pagado_en, fecha)
                               WHERE estado_pago IS NULL OR estado_pago='pendiente'""")
            except Exception:
                pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS detalle_venta (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                venta_id    INT NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT NOT NULL,
                precio      DECIMAL(10,2) NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bloqueos_ip (
                id                  INT AUTO_INCREMENT PRIMARY KEY,
                ip                  VARCHAR(50) NOT NULL,
                usuarios_diferentes INT DEFAULT 1,
                bloqueado_hasta     DATETIME,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS intentos_usuario (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                correo          VARCHAR(200) NOT NULL,
                -- DEFAULT 0: la fila solo nace al llegar al umbral y mientras
                -- no se bloquea, el contador llega en 0.
                intentos        INT DEFAULT 0,
                bloqueado_hasta DATETIME,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for sql in (
            # Momento del ultimo fallo: es lo que permite que la rafaga venza.
            "ALTER TABLE intentos_usuario ADD COLUMN ultimo_fallo DATETIME",
            # Pico historico de fallos. No baja nunca, para no perder el dato de
            # "esta cuenta llego a N" cuando el contador activo se reinicia.
            "ALTER TABLE intentos_usuario ADD COLUMN max_intentos INT DEFAULT 0",
            # Indice de la limpieza periodica.
            "ALTER TABLE intentos_usuario ADD INDEX ix_intentos_ultimo (ultimo_fallo)",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        # El codigo del login asume UNA fila por correo: hace fetchone() y un
        # UPDATE ... WHERE correo=%s que pisa todas las filas que coincidan. Con
        # duplicados el resultado depende del orden que devuelva MySQL. Se
        # deduplica conservando la fila mas grave antes de poner el UNIQUE.
        try:
            _deduplicar_intentos(cur)
            cur.execute("ALTER TABLE intentos_usuario ADD UNIQUE KEY uq_intentos_correo (correo)")
        except Exception:
            pass

        # Filas cuya rafaga ya no dice nada: un atacante que prueba correos
        # inventados deja una por cada 3 intentos y la tabla crece sin freno.
        try:
            cur.execute(
                "DELETE FROM intentos_usuario "
                "WHERE (ultimo_fallo IS NOT NULL AND ultimo_fallo < NOW() - INTERVAL %s DAY) "
                "   OR (ultimo_fallo IS NULL AND created_at < NOW() - INTERVAL %s DAY)",
                (DIAS_LIMPIEZA_INTENTOS, DIAS_LIMPIEZA_INTENTOS))
        except Exception:
            pass

        # ── P0: Control de estado de clientes ──────────────────────────
        # Historial de cambios de estado. Idempotente para Render/Alwaysdata.
        #
        # El catalogo de estados vive en el dict ESTADOS_CLIENTE (arriba).
        # La tabla `estados_cliente` se elimino: no se leia en ningun request
        # y el app la repoblaba sola al arrancar. El estado real de cada
        # cliente va en `usuarios.estado`.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS historial_estado_cliente (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                usuario_id      INT NOT NULL,
                estado_anterior VARCHAR(30),
                estado_nuevo    VARCHAR(30) NOT NULL,
                motivo          TEXT,
                admin_id        INT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX ix_hist_usuario (usuario_id)
            )
        """)
        # Normalizar valores nulos o desconocidos a 'activo'
        try:
            cur.execute("UPDATE usuarios SET estado='activo' WHERE estado IS NULL OR estado=''")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE usuarios ADD INDEX ix_usuarios_estado (estado)")
        except Exception:
            pass

        # ── Cancelación de pedidos: catálogo de motivos + registro ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS motivos_cancelacion (
                codigo VARCHAR(30) PRIMARY KEY,
                texto  VARCHAR(150) NOT NULL,
                activo TINYINT DEFAULT 1
            )
        """)
        for _cod, _txt in (
            ('cambio_opinion', 'Cambié de opinión'),
            ('mejor_precio', 'Encontré mejor precio'),
            ('demora_entrega', 'Demora en la entrega'),
            ('error_pedido', 'Error en el pedido'),
            ('producto_equivocado', 'Producto equivocado'),
            ('otro', 'Otro'),
        ):
            try:
                cur.execute("INSERT IGNORE INTO motivos_cancelacion (codigo, texto) VALUES (%s,%s)",
                            (_cod, _txt))
            except Exception:
                pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cancelaciones (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                venta_id        INT NOT NULL UNIQUE,
                motivo_codigo   VARCHAR(30) NOT NULL DEFAULT 'otro',
                motivo_detalle  TEXT,
                cancelado_por   VARCHAR(10) NOT NULL DEFAULT 'cliente',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX ix_cancel_motivo (motivo_codigo)
            )
        """)
        # Normalizar estados legacy de ventas al vocabulario válido
        # (en espera/entregado/cancelado). 'pendiente' pertenece a
        # seguimiento_entregas y en ventas equivale a 'en espera'.
        try:
            cur.execute("UPDATE ventas SET estado='en espera' "
                        "WHERE estado IS NULL OR estado NOT IN ('en espera','entregado','cancelado')")
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS seguimiento_entregas (
                id               INT AUTO_INCREMENT PRIMARY KEY,
                venta_id         INT NOT NULL,
                estado           VARCHAR(30) DEFAULT 'pendiente',
                direccion_envio  VARCHAR(400),
                fecha_estimada   DATE,
                fecha_entrega    DATETIME,
                notas            TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS entrega_productos (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                entrega_id  INT NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT NOT NULL DEFAULT 1
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS direcciones (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                usuario_id      VARCHAR(100) NOT NULL,
                direccion       VARCHAR(300) NOT NULL,
                distrito        VARCHAR(150),
                referencia      VARCHAR(300),
                predeterminada  TINYINT DEFAULT 0,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Cancelación de pedidos: catálogo de motivos + registro ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS motivos_cancelacion (
                codigo VARCHAR(30) PRIMARY KEY,
                texto  VARCHAR(150) NOT NULL,
                activo TINYINT DEFAULT 1
            )
        """)
        for _cod, _txt in (
            ('cambio_opinion', 'Cambié de opinión'),
            ('mejor_precio', 'Encontré mejor precio'),
            ('demora_entrega', 'Demora en la entrega'),
            ('error_pedido', 'Error en el pedido'),
            ('producto_equivocado', 'Producto equivocado'),
            ('otro', 'Otro'),
        ):
            try:
                cur.execute("INSERT IGNORE INTO motivos_cancelacion (codigo, texto) VALUES (%s,%s)",
                            (_cod, _txt))
            except Exception:
                pass
        # La tabla del motivo de cancelacion es `cancelaciones` (plural), la
        # que usan TODAS las consultas y la que lee el modulo de KPIs. Antes
        # se creaba tambien `cancelacion` (singular) y el motivo se guards
        # ahi: una tabla para nadie, con los motivos de cancelacion
        # invisibles para el reporte y para los indicadores.

        # ── FKs corruptas (BD principal) ────────────────────────────────
        # Estas claves foraneas NO las define la app: quedaron apuntando el
        # 'id' de una tabla al 'id' de OTRA sin relacion (probablemente al
        # importar/restaurar datos). Bloquean inserts legitimos (p. ej.
        # registrar un cliente) y el codigo nunca dependio de ellas.
        # Se eliminan; si ya no existen el ALTER falla y se ignora.
        for sql in (
            "ALTER TABLE clientes DROP FOREIGN KEY clientes_ibfk_1",
            "ALTER TABLE bloqueos_ip DROP FOREIGN KEY bloqueos_ip_ibfk_1",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        mysql.connection.commit()
        app.logger.info("[init_db] Tablas BD principal OK")

        # Sincroniza ventas antiguas con /entregas. No crea tablas ni cambia
        # estados: solo inserta el seguimiento que falte.
        try:
            creados = _backfill_entregas(cur)
            mysql.connection.commit()
            if creados:
                app.logger.info(
                    "[init_db] Seguimiento creado para %s venta(s) existente(s)",
                    creados)
        except Exception as e:
            app.logger.warning(
                "[init_db] No se pudo sincronizar seguimiento de ventas: %s", e)

        # ── Base de datos de Gestión de Almacén ─────────────────────────
        cur = mysql.connection.cursor()
        try:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {ALMACEN_DB} CHARACTER SET utf8mb4")
        except Exception:
            app.logger.warning("No se pudo crear BD '%s', usando principal", ALMACEN_DB)
            ALMACEN_DB = MAIN_DB
            app.config['ALMACEN_DB'] = ALMACEN_DB

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.productos (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                nombre      VARCHAR(200) NOT NULL,
                descripcion TEXT,
                precio      DECIMAL(10,2) NOT NULL,
                stock       INT DEFAULT 0,
                categoria   VARCHAR(100),
                imagen      VARCHAR(300),
                imagen_datos LONGBLOB,
                imagen_mime VARCHAR(100),
                estado      VARCHAR(20) DEFAULT 'activo',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cur.execute(f"ALTER TABLE {ALMACEN_DB}.productos ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        except Exception:
            pass
        # Imagen persistente: el archivo en static/uploads vive en un disco
        # efimero (Render lo borra al reiniciar y ademas esta en .gitignore),
        # por eso la imagen se veia unos minutos y luego desaparecia. Estos
        # campos guardan los bytes en la BD, que si es persistente.
        for sql in (
            f"ALTER TABLE {ALMACEN_DB}.productos ADD COLUMN imagen_datos LONGBLOB",
            f"ALTER TABLE {ALMACEN_DB}.productos ADD COLUMN imagen_mime VARCHAR(100)",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        # Galeria de productos: hasta 4 imagenes por producto. La principal se
        # sigue guardando en productos.imagen/imagen_datos/imagen_mime (asi no
        # se rompen listados ni carrito) y las demas viven aqui, con sus bytes
        # en la BD por la misma razon que la principal (disco efimero en Render).
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.productos_imagenes (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                producto_id  INT NOT NULL,
                imagen       VARCHAR(300),
                imagen_datos LONGBLOB,
                imagen_mime  VARCHAR(100),
                orden        INT DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX ix_producto_imagenes_producto (producto_id)
            )
        """)

        # Un proveedor puede ser persona (DNI) o empresa (RUC). En vez de tener
        # dos columnas donde una siempre queda vacia, se guarda el tipo y el
        # numero. `nombre` se mantiene como nombre completo ya armado: lo usan
        # el buscador, el orden alfabetico, los ingresos y los mensajes de
        # WhatsApp, y asi no hay que concatenar en cada consulta.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.proveedores (
                id                INT AUTO_INCREMENT PRIMARY KEY,
                nombre            VARCHAR(200) NOT NULL,
                tipo_documento    VARCHAR(3) NOT NULL DEFAULT 'DNI',
                numero_documento  VARCHAR(11),
                nombres           VARCHAR(120),
                apellido_paterno  VARCHAR(60),
                apellido_materno  VARCHAR(60),
                celular           VARCHAR(30),
                direccion         VARCHAR(300),
                categoria         VARCHAR(100),
                notas             TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.productos_para_pedir (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                producto_id     INT NOT NULL,
                cantidad_pedido INT DEFAULT 1,
                proveedor_id    INT,
                estado          VARCHAR(20) DEFAULT 'pendiente',
                fecha           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.ingresos (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                proveedor_id    INT,
                notas           TEXT,
                fecha           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                estado          VARCHAR(20) DEFAULT 'vigente',
                proveedor_nombre    VARCHAR(200),
                proveedor_documento VARCHAR(40),
                motivo_anulacion    VARCHAR(255),
                anulado_en          DATETIME NULL,
                anulado_por         VARCHAR(120)
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.detalle_ingreso (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                ingreso_id  INT NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT NOT NULL,
                precio_compra DECIMAL(10,2) DEFAULT 0,
                precio_venta  DECIMAL(10,2) DEFAULT 0,
                producto_nombre VARCHAR(200)
            )
        """)
        # Migracion: MySQL no tiene "ADD COLUMN IF NOT EXISTS", asi que cada
        # ALTER se intenta y se ignora si la columna ya existe.
        #   precio_venta  : el precio definido al ingresar (para la boleta).
        #   *_nombre/_documento : foto del nombre al momento del ingreso. El
        #     libro de ingresos no puede cambiar si despues se renombra o se
        #     borra el proveedor o el producto.
        #   estado/motivo/anulado_* : el ingreso se anula, nunca se borra.
        for sql in (
            f"ALTER TABLE {ALMACEN_DB}.detalle_ingreso "
            f"ADD COLUMN precio_venta DECIMAL(10,2) DEFAULT 0",
            f"ALTER TABLE {ALMACEN_DB}.detalle_ingreso "
            f"ADD COLUMN producto_nombre VARCHAR(200)",
            f"ALTER TABLE {ALMACEN_DB}.ingresos "
            f"ADD COLUMN estado VARCHAR(20) DEFAULT 'vigente'",
            f"ALTER TABLE {ALMACEN_DB}.ingresos "
            f"ADD COLUMN proveedor_nombre VARCHAR(200)",
            f"ALTER TABLE {ALMACEN_DB}.ingresos "
            f"ADD COLUMN proveedor_documento VARCHAR(40)",
            f"ALTER TABLE {ALMACEN_DB}.ingresos "
            f"ADD COLUMN motivo_anulacion VARCHAR(255)",
            f"ALTER TABLE {ALMACEN_DB}.ingresos "
            f"ADD COLUMN anulado_en DATETIME NULL",
            f"ALTER TABLE {ALMACEN_DB}.ingresos "
            f"ADD COLUMN anulado_por VARCHAR(120)",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass
        _respaldar_nombres_ingresos(cur)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.salidas (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                venta_id    INT,
                notas       TEXT,
                fecha       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.detalle_salida (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                salida_id   INT NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT NOT NULL
            )
        """)

        # ── FKs corruptas (BD almacén) ──────────────────────────────────
        # Ver nota en la BD principal: apuntan el 'id' al 'id' de otra tabla
        # sin relacion y hoy bloquean crear proveedores, ingresos y salidas.
        for sql in (
            f"ALTER TABLE {ALMACEN_DB}.proveedores DROP FOREIGN KEY proveedores_ibfk_1",
            f"ALTER TABLE {ALMACEN_DB}.ingresos DROP FOREIGN KEY ingresos_ibfk_1",
            f"ALTER TABLE {ALMACEN_DB}.detalle_salida DROP FOREIGN KEY detalle_salida_ibfk_1",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass

        # Migración única: copiar datos de BD principal si almacén está vacía
        for tabla in ('productos', 'proveedores', 'productos_para_pedir'):
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.{tabla}")
                n_al = cur.fetchone()['n']
                cur.execute(f"SELECT COUNT(*) AS n FROM {tabla}")
                n_orig = cur.fetchone()['n']
                if n_orig and not n_al:
                    cur.execute(f"INSERT INTO {ALMACEN_DB}.{tabla} SELECT * FROM {tabla}")
            except Exception:
                pass

        mysql.connection.commit()
        app.logger.info("[init_db] Tablas BD almacén OK")

    except Exception as e:
        app.logger.warning("init_db error (no crítico): %s", e)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def obtener_ip():
    return request.remote_addr

def obtener_usuario():
    if 'user_id' in session:
        return session['user_id']
    if 'guest_id' not in session:
        session['guest_id'] = str(uuid.uuid4())
    return session['guest_id']


def _password_valida(password):
    """Reglas de contraseña antes de pasarla a bcrypt.

    bcrypt 5 rechaza (ValueError) las cadenas de mas de 72 bytes, asi que se
    valida aqui: si no, un registro con una clave larga terminaba en error 500.
    """
    if len(password) < 8:
        return False, 'La contraseña debe tener al menos 8 caracteres.'
    if len(password.encode('utf-8')) > 72:
        return False, 'La contraseña no puede superar los 72 caracteres.'
    return True, ''


def puede_ver_venta(venta):
    """Un cliente solo ve sus ventas; un administrador ve todas."""
    if session.get('rol') in ('admin', 'administrador'):
        return True
    if not venta or session.get('user_id') is None:
        return False
    try:
        return int(venta.get('cliente_id')) == int(session['user_id'])
    except (TypeError, ValueError):
        return False


class _SinStock(Exception):
    """Se lanza cuando el stock cambio entre la validacion y el descuento."""

def generar_boleta_pdf(venta_id):
    """Genera PDF en memoria y lo devuelve como bytes."""
    cur = mysql.connection.cursor()
    cur.execute("SELECT documento, doc_tipo, nombre, apellido, total, fecha FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta:
        return None, None

    cur.execute(f"""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """, (venta_id,))
    productos = cur.fetchall()
    cur.close()

    total = sum(p['cantidad'] * p['precio'] for p in productos)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf)
    styles = getSampleStyleSheet()
    elems = []

    elems.append(Paragraph("<b>MULTISERVICIOS RICHARD</b>", styles['Title']))
    elems.append(Paragraph("RUC: 10450119429", styles['Normal']))
    elems.append(Spacer(1, 0.3*inch))
    elems.append(Paragraph(f"<b>Boleta N°:</b> {venta_id}", styles['Normal']))
    elems.append(Paragraph(f"<b>Cliente:</b> {((venta.get('nombre') or '') + ' ' + (venta.get('apellido') or '')).strip() or 'Sin nombre'}", styles['Normal']))
    doc_tipo = (venta.get('doc_tipo') or '').lower()
    if doc_tipo not in ('dni', 'ruc'):
        doc_tipo = 'ruc' if len((venta.get('documento') or '')) == 11 else 'dni'
    elems.append(Paragraph(f"<b>{doc_tipo.upper()}:</b> {venta.get('documento') or '-'}", styles['Normal']))
    elems.append(Paragraph(f"<b>Fecha:</b> {venta.get('fecha')}", styles['Normal']))
    elems.append(Spacer(1, 0.3*inch))

    data = [["Producto","Cant.","Precio","Subtotal"]]
    for p in productos:
        sub = p['cantidad'] * p['precio']
        data.append([p['nombre'], p['cantidad'], f"S/ {p['precio']:.2f}", f"S/ {sub:.2f}"])
    data.append(["","","TOTAL", f"S/ {total:.2f}"])

    t = Table(data, colWidths=[200,60,80,80])
    t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0), colors.lightgrey),
        ('GRID',(0,0),(-1,-1),1,colors.black),
        ('ALIGN',(1,1),(-1,-1),'CENTER'),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 0.5*inch))
    elems.append(Paragraph("Gracias por su compra", styles['Normal']))
    doc.build(elems)
    buf.seek(0)
    return buf.read(), total

def enviar_boleta_cliente(correo_cliente, venta_id):
    """Genera la boleta y la envía al correo sin bloquear la respuesta del checkout.
    El PDF se genera al momento (lectura rápida) y el envío SMTP corre en segundo plano."""
    try:
        pdf_bytes, _ = generar_boleta_pdf(venta_id)
        if not pdf_bytes:
            return False
        msg = Message(
            subject=f'Tu boleta #{venta_id} - Multiservicios Richard',
            recipients=[correo_cliente],
            body=f'Gracias por tu compra. Adjuntamos tu boleta de venta N° {venta_id}.\n\nMultiservicios Richard'
        )
        msg.attach(f'boleta_{venta_id}.pdf', 'application/pdf', pdf_bytes)

        def _enviar():
            with app.app_context():
                mail.send(msg)

        threading.Thread(target=_enviar, daemon=True).start()
        return True
    except Exception as e:
        print(f"[email_boleta] {e}")
        return False

def verificar_stock_bajo(cur, producto_id):
    """Si stock <= 1 auto-agrega a productos_para_pedir.
       Si stock <= 0 oculta el producto (estado='inactivo')."""
    try:
        cur.execute(f"SELECT nombre, stock, categoria FROM {ALMACEN_DB}.productos WHERE id=%s", (producto_id,))
        p = cur.fetchone()
        if not p:
            return
        if p['stock'] <= 0:
            cur.execute(f"UPDATE {ALMACEN_DB}.productos SET estado='inactivo' WHERE id=%s", (producto_id,))
        if p['stock'] > 1:
            return
        # ¿Ya existe pendiente?
        cur.execute(f"""
            SELECT id FROM {ALMACEN_DB}.productos_para_pedir
            WHERE producto_id=%s AND estado='pendiente'
        """, (producto_id,))
        if cur.fetchone():
            return
        # Proveedor por categoría
        cur.execute(f"""
            SELECT id, nombre, celular
            FROM {ALMACEN_DB}.proveedores WHERE categoria=%s LIMIT 1
        """, (p['categoria'],))
        proveedor = cur.fetchone()
        proveedor_id = proveedor['id'] if proveedor else None

        cur.execute(f"""
            INSERT INTO {ALMACEN_DB}.productos_para_pedir (producto_id, cantidad_pedido, proveedor_id)
            VALUES (%s, 1, %s)
        """, (producto_id, proveedor_id))
    except Exception as e:
        print(f"[stock_bajo] {e}")

# ─────────────────────────────────────────────
# CANCELACIÓN DE PEDIDOS: motivos y stock
# ─────────────────────────────────────────────

MOTIVOS_CANCELACION = {
    'cambio_opinion': 'Cambié de opinión',
    'mejor_precio': 'Encontré mejor precio',
    'demora_entrega': 'Demora en la entrega',
    'error_pedido': 'Error en el pedido',
    'producto_equivocado': 'Producto equivocado',
    'otro': 'Otro',
}

def motivo_cancelacion_valido(codigo):
    return codigo in MOTIVOS_CANCELACION

app.jinja_env.globals['motivos_cancelacion'] = MOTIVOS_CANCELACION
app.jinja_env.globals['motivo_cancelacion_valido'] = motivo_cancelacion_valido

def restaurar_stock_venta(cur, venta_id):
    """Devuelve al almacén el stock de cada artículo de la venta cancelada."""
    cur.execute(f"SELECT producto_id, cantidad FROM detalle_venta WHERE venta_id=%s", (venta_id,))
    lineas = cur.fetchall() or []
    for it in lineas:
        cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock+%s WHERE id=%s",
                    (it['cantidad'], it['producto_id']))
    return len(lineas)

def registrar_cancelacion(cur, venta_id, motivo_codigo, motivo_detalle, cancelado_por):
    """Registra el motivo de cancelación (idempotente por UNIQUE de venta_id)."""
    if not motivo_cancelacion_valido(motivo_codigo):
        motivo_codigo = 'otro'
    try:
        cur.execute(
            "INSERT IGNORE INTO cancelaciones (venta_id, motivo_codigo, motivo_detalle, cancelado_por)"
            " VALUES (%s,%s,%s,%s)",
            (venta_id, motivo_codigo, (motivo_detalle or None), cancelado_por))
    except Exception:
        pass

def whatsapp_url(celular, mensaje):
    """Genera URL de WhatsApp con mensaje prefill."""
    numero = ''.join(filter(str.isdigit, celular or ''))
    if not numero.startswith('51'):
        numero = '51' + numero
    return f"https://wa.me/{numero}?text={quote(mensaje)}"

# ─────────────────────────────────────────────
# CONTEXT PROCESSOR
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# CACHE DEL CONTADOR DEL CARRITO
# ─────────────────────────────────────────────
# `cantidad_carrito` es un context processor: se ejecuta en TODAS las paginas
# renderizadas y hacia una consulta (~190 ms con la BD remota), incluso para
# invitados que nunca tienen carrito (obtener_usuario les crea un guest_id).
# El numero solo cambia cuando algo escribe en `carrito`, asi que se guarda
# unos segundos y se invalida en cada operacion que toque la tabla.
_CARRITO_TTL = 30
_carrito_cache = {}
_carrito_lock = threading.Lock()


def _carrito_invalidar(*usuarios):
    """Olvida el contador cacheado de uno o varios usuarios."""
    with _carrito_lock:
        for usuario in usuarios:
            if usuario is not None:
                _carrito_cache.pop(str(usuario), None)


def _carrito_purgar(ahora):
    """Quita entradas vencidas para que la cache no crezca sin limite.

    Se llama con `_carrito_lock` ya tomado."""
    if len(_carrito_cache) <= 500:
        return
    vencidas = [k for k, (momento, _) in _carrito_cache.items()
                if ahora - momento >= _CARRITO_TTL]
    for clave in vencidas:
        _carrito_cache.pop(clave, None)
    if len(_carrito_cache) > 500:
        sobrantes = len(_carrito_cache) - 500
        for clave in sorted(_carrito_cache,
                            key=lambda k: _carrito_cache[k][0])[:sobrantes]:
            _carrito_cache.pop(clave, None)


@app.context_processor
def cantidad_carrito():
    try:
        usuario = obtener_usuario()
        clave = str(usuario)
        ahora = time.time()
        with _carrito_lock:
            guardado = _carrito_cache.get(clave)
            if guardado is not None and (ahora - guardado[0]) < _CARRITO_TTL:
                return dict(cantidad_carrito=guardado[1])
        cur = mysql.connection.cursor()
        cur.execute("SELECT SUM(cantidad) AS total FROM carrito WHERE usuario_id=%s", (usuario,))
        res = cur.fetchone()
        total = int(res['total']) if res and res['total'] else 0
        with _carrito_lock:
            _carrito_cache[clave] = (ahora, total)
            _carrito_purgar(ahora)
        return dict(cantidad_carrito=total)
    except Exception:
        return dict(cantidad_carrito=0)


@app.context_processor
def datos_usuario_nav():
    """Expone `usuario_nombre` (primer nombre + primer apellido) al navbar.

    El navbar mostraba el correo; ahora muestra el nombre corto. Si la cuenta
    no tiene nombre (cuentas antiguas) se cae al usuario del correo."""
    correo = session.get('correo')
    if not correo:
        return {}
    nombre   = (session.get('nombre') or '').strip()
    apellido = (session.get('apellido') or '').strip()
    # Logins viejos: la sesion no traia el nombre todavia -> se lee de la BD.
    if not nombre and session.get('user_id'):
        try:
            cur = mysql.connection.cursor()
            cur.execute("SELECT nombre, apellido FROM usuarios WHERE id=%s",
                        (session['user_id'],))
            fila = cur.fetchone()
            cur.close()
            if fila:
                nombre   = (fila.get('nombre') or '').strip()
                apellido = (fila.get('apellido') or '').strip()
                session['nombre']   = nombre
                session['apellido'] = apellido
        except Exception:
            pass
    primer_nombre   = nombre.split()[0].upper() if nombre else ''
    primer_apellido = apellido.split()[0].upper() if apellido else ''
    display = (primer_nombre + ' ' + primer_apellido).strip()
    if not display:
        display = correo.split('@')[0].upper()
    return dict(usuario_nombre=display, usuario_correo=correo)

# ─────────────────────────────────────────────
# TEST / INIT
# ─────────────────────────────────────────────
@app.route('/test_db')
def test_db():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return "Acceso denegado", 403
    try:
        cur = mysql.connection.cursor()
        cur.execute("SHOW TABLES;")
        return str(cur.fetchall())
    except Exception as e:
        return f"ERROR: {e}"

@app.route('/init-db')
def ruta_init_db():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return "Acceso denegado"
    init_db()
    flash('Tablas creadas/verificadas correctamente.', 'success')
    return redirect('/dashboard')

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
# Reglas del bloqueo por intentos fallidos:
#
#   - El intento 1 y el 2 NO se registran en la base. Se cuentan "a la sombra"
#     en una cookie firmada (INTENTOS_SOMBRA), que el navegador no puede
#     editar sin romper la firma de SECRET_KEY.
#   - Al llegar a UMBRAL_INTENTOS la fila nace en intentos_usuario con todos
#     sus campos (id, correo, intentos, bloqueado_hasta). Esa tabla es el
#     registro consultable de quien intento entrar de mas.
#   - Al llegar al 5.o fallo se bloquea, y cada fallo posterior alarga el
#     castigo (ver ESCALERA_BLOQUEO) hasta un tope de 24 horas.
#   - Al vencer un bloqueo se concede UN intento mas. El contador NO se
#     reinicia: por eso la escalada puede seguir subiendo.
#   - Un login correcto NO borra la fila ni baja el contador: queda el
#     historial. Por eso el castigo es acumulativo por correo.
#
# Tiempo de vida de los fallos:
#
#   - ultimo_fallo marca cuando fue el ultimo fallo. Si la rafaga envejece mas
#     que la ventana, el contador vuelve a 0: los fallos viejos no pesan.
#   - La ventana es el MAYOR valor entre VENTANA_BASE_MIN y la duracion del
#     ultimo bloqueo aplicado. Si fuera un valor fijo, un atacante que espera
#     lo necesario reiniciaria su contador y la escalera no serviria de nada:
#     un bloqueo de 24 h le costaria 30 minutos reales.
#   - max_intentos guarda el pico historico y nunca baja, asi que el dato de
#     "esta cuenta llego a N" no se pierde cuando el contador se reinicia.
UMBRAL_INTENTOS  = 3
INTENTOS_SOMBRA  = 'intentos_sombra'

# Numero de fallo a partir del cual empieza a aplicarse la escalera de bloqueo.
PRIMER_BLOQUEO_FALLO = 5

# Base de la ventana de decaimiento. No puede bajar de la duracion del primer
# bloqueo (15 min) o la escalera se romperia en el primer peldano.
VENTANA_BASE_MIN = 30

# Fallo -> minutos de bloqueo. El primer bloqueo es el del 5.o fallo; a partir
# de ahi cada intento fallido nuevo dobla el castigo, hasta 24 horas.
ESCALERA_BLOQUEO = {
    5:   15,        # 5.o  fallo -> 15 minutos
    6:   30,        # 6.o  fallo -> 30 minutos
    7:   60,        # 7.o  fallo -> 1 hora
    8:   120,       # 8.o  fallo -> 2 horas
    9:   240,       # 9.o  fallo -> 4 horas
    10:  480,       # 10.o fallo -> 8 horas
    11:  960,       # 11.o fallo -> 16 horas
    12:  1440,      # 12.o fallo -> 24 horas (techo alcanzado)
}
BLOQUEO_TOPE_MIN = 1440   # 24 horas: ningun fallo pasa de aqui


def minutos_bloqueo(intentos):
    """Minutos de castigo para un numero de fallo dado.

    Del 12.o fallo en adelante se mantiene el tope de 24 horas por cada intento
    fallido, tal como se pidio.
    """
    return min(ESCALERA_BLOQUEO.get(intentos, BLOQUEO_TOPE_MIN), BLOQUEO_TOPE_MIN)


def ventana_decaimiento(intentos):
    """Minutos que un contador sobrevive sin actividad antes de volver a 0.

    Es el mayor valor entre la base y la duracion del bloqueo de ese nivel de
    fallos. Asi la escalada no se puede saltar esperando:

        5.o fallo  -> bloqueo 15 min -> ventana 30 min  (gana la base)
        9.o fallo  -> bloqueo  4 h   -> ventana  4 h
        12.o fallo -> bloqueo 24 h   -> ventana 24 h

    Para intentos por debajo del primer bloqueo no hay castigo todavia, asi que
    solo aplica la base.
    """
    if intentos < PRIMER_BLOQUEO_FALLO:
        return VENTANA_BASE_MIN
    return max(VENTANA_BASE_MIN, minutos_bloqueo(intentos))


def _leer_sombra(sombra, correo, ahora=None):
    """Numero de fallos que lleva el conteo a la sombra de este correo.

    La cookie guarda {'n': intentos, 't': epoch}. Si la entrada es mas vieja que
    la ventana, la rafaga se dio por terminada y el conteo arranca en 0. Tambien
    se acepta el formato anterior (un entero suelto), que no trae fecha: en ese
    caso se respeta tal cual, para no perdonar fallos a quien ya venia contando.
    """
    entrada = sombra.get(correo)
    if entrada is None:
        return 0
    if isinstance(entrada, dict):
        n = entrada.get('n', 0)
        marca = entrada.get('t')
        if not marca:
            return n
        ahora = ahora or datetime.now()
        if (ahora - datetime.fromtimestamp(marca)) > timedelta(minutes=VENTANA_BASE_MIN):
            return 0
        return n
    # Formato viejo: entero sin fecha.
    return int(entrada)


def rafaga_vencida(fila, ahora=None):
    """True si la rafaga de fallos de esta fila ya no debe seguir pesando.

    Devuelve False cuando no hay fecha de ultimo fallo: son filas anteriores a
    la columna ultimo_fallo, y reiniciarlas todas de golpe al desplegar seria
    dejar entrar a quien ya estaba castigado. Para esas filas no hay decaimiento
    hasta queRegistren un fallo nuevo.
    """
    if not fila:
        return False
    ultimo = fila.get('ultimo_fallo')
    if not ultimo:
        return False
    ahora = ahora or datetime.now()
    ventana = timedelta(minutes=ventana_decaimiento(fila.get('intentos') or 0))
    return (ahora - ultimo) > ventana


def humanizar_bloqueo(timedelta_restante):
    """Convierte el tiempo restante en texto legible.

    Importa no usar `.seconds` a secas: en un timedelta de mas de un dia, el
    campo `.seconds` vale lo que sobra del dia, no el total. Un bloqueo de 24
    horas con `.seconds` diria "Intenta en 0m 0s".
    """
    total = int(timedelta_restante.total_seconds())
    if total < 0:
        total = 0
    if total < 60:
        return f'{total}s'
    if total < 3600:
        return f'{total // 60}m {total % 60}s'
    if total < 86400:
        horas = total // 3600
        resto = (total % 3600) // 60
        return f'{horas}h' if not resto else f'{horas}h {resto}m'
    return f'{total // 86400}d'


# ─────────────────────────────────────────────
# VALIDACION DE reCAPTCHA v2
# ─────────────────────────────────────────────
# Codigos que devuelve Google en `error-codes`, traducidos a lo que el usuario
# puede hacer al respecto. Un mensaje tecnico ("invalid-input-response") no le
# sirve de nada a quien solo queria entrar a su cuenta.
_RECAPTCHA_ERRORES = {
    'missing-input-secret':   'El captcha no esta bien configurado en el servidor.',
    'invalid-input-secret':   'El captcha no esta bien configurado en el servidor.',
    'missing-input-response': 'Marca la casilla "No soy un robot" para continuar.',
    'invalid-input-response': 'La verificacion del captcha fallo o expiro. Vuelve a marcarla.',
    'bad-request':            'La solicitud del captcha no es valida. Recarga la pagina.',
    'timeout-or-duplicate':   'El captcha expiro o ya se uso. Vuelve a marcarlo.',
}


def _recaptcha_valido(token, ip):
    """Valida el token de reCAPTCHA v2 contra Google.

    Devuelve `(ok, mensaje)`. `mensaje` es None si la verificacion paso; si no,
    es el texto que se muestra al usuario con flash.

    Se llama ANTES de tocar la base de datos y antes de bcrypt: un bot no debe
    consumir consultas ni ciclos de hashing. El token es de un solo uso, asi que
    esta funcion debe llamarse una sola vez por peticion.
    """
    if not RECAPTCHA_ACTIVO:
        return True, None
    if not token:
        return False, 'Marca la casilla "No soy un robot" para continuar.'

    try:
        respuesta = requests.post(
            RECAPTCHA_VERIFY_URL,
            data={'secret': RECAPTCHA_SECRET_KEY, 'response': token,
                  'remoteip': ip},
            timeout=RECAPTCHA_TIMEOUT,
        )
        datos = respuesta.json()
    except Exception:
        # Sin respuesta de Google no hay forma de distinguir a un humano de un
        # bot: se rechaza (fail-closed) y se pide reintentar. Se registra el
        # error completo porque casi siempre es red o DNS, no el usuario.
        app.logger.error('reCAPTCHA: no se pudo consultar a Google', exc_info=True)
        return False, 'No se pudo verificar el captcha. Intenta de nuevo en unos segundos.'

    if datos.get('success'):
        # Google devuelve el hostname donde se resolvio el desafio. Si no
        # coincide con el nuestro, el token se obtuvo en otra pagina (o se
        # reutilizo) y no vale para esta peticion.
        hostname = (datos.get('hostname') or '').lower()
        propio = (request.host or '').split(':')[0].lower()
        if hostname and propio and hostname != propio:
            app.logger.warning('reCAPTCHA: hostname %s no coincide con %s',
                               hostname, propio)
            return False, 'La verificacion del captcha fue rechazada. Recarga la pagina.'
        return True, None

    codigos = datos.get('error-codes') or []
    app.logger.warning('reCAPTCHA rechazado: %s', ','.join(codigos) or 'sin codigo')
    return False, _RECAPTCHA_ERRORES.get(
        codigos[0] if codigos else '',
        'La verificacion del captcha fallo. Intenta de nuevo.')


@app.route('/login', methods=['GET','POST'])
@app.route('/Iniciar Sesion', methods=['GET','POST'])
def login():
    ip = obtener_ip()
    if request.method == 'POST':
        correo  = request.form['correo']
        password = request.form['password']
        cur = mysql.connection.cursor()

        cur.execute("SELECT * FROM bloqueos_ip WHERE ip=%s", (ip,))
        bloqueo_ip = cur.fetchone()
        if bloqueo_ip and bloqueo_ip['bloqueado_hasta']:
            ahora = datetime.now()
            if ahora < bloqueo_ip['bloqueado_hasta']:
                restante = bloqueo_ip['bloqueado_hasta'] - ahora
                flash(f"IP bloqueada. Intenta en {humanizar_bloqueo(restante)}", 'danger')
                return redirect('/login')
            else:
                cur.execute("DELETE FROM bloqueos_ip WHERE ip=%s", (ip,))
                mysql.connection.commit()

        # ── Conteo a la sombra (intentos 1 y 2) ──
        # Vive en la cookie de sesion, que Flask firma con SECRET_KEY. No se
        # escribe nada en la base hasta pasar el umbral.
        #
        # Cada entrada lleva su propia marca de tiempo {'n': 2, 't': <epoch>}
        # porque esta rafaga tambien tiene que vencer: 2 fallos, una pausa
        # larga y un fallo mas no pueden sumar como si fuera la misma ráfaga.
        # Se acepta el formato viejo (entero sin fecha) para no romper las
        # cookies de sesion que ya estan en el navegador.
        sombra = session.get(INTENTOS_SOMBRA, {})
        if not isinstance(sombra, dict):
            sombra = {}

        cur.execute("SELECT * FROM intentos_usuario WHERE correo=%s", (correo,))
        bloqueo_usuario = cur.fetchone()

        if bloqueo_usuario:
            if bloqueo_usuario['bloqueado_hasta']:
                ahora = datetime.now()
                if ahora < bloqueo_usuario['bloqueado_hasta']:
                    restante = bloqueo_usuario['bloqueado_hasta'] - ahora
                    flash(f"Usuario bloqueado. Intenta en {humanizar_bloqueo(restante)}", 'danger')
                    return redirect('/login')
                # El bloqueo caduco: se concede UN intento mas. Solo se levanta
                # el bloqueo, el contador NO se reinicia, para que si este
                # intento tambien falla la escalada siga subiendo.
                cur.execute("UPDATE intentos_usuario SET bloqueado_hasta=NULL WHERE correo=%s", (correo,))
                mysql.connection.commit()
                bloqueo_usuario = dict(bloqueo_usuario, bloqueado_hasta=None)

            # Decaimiento: si la rafaga es mas vieja que su ventana, los fallos
            # anteriores dejan de contar. max_intentos conserva el pico, asi que
            # el historial no se pierde aunque el contador vuelva a cero.
            if rafaga_vencida(bloqueo_usuario):
                cur.execute("UPDATE intentos_usuario SET intentos=0, bloqueado_hasta=NULL WHERE correo=%s",
                            (correo,))
                mysql.connection.commit()
                bloqueo_usuario = dict(bloqueo_usuario, intentos=0, bloqueado_hasta=None)

        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        usuario = cur.fetchone()

        # P0: cuentas inactivas/bloqueadas no entran aunque la clave sea correcta.
        if usuario and (usuario.get('estado') in ('bloqueado', 'inactivo')):
            flash('Tu cuenta está %s. Contacta al administrador.' % usuario.get('estado'), 'danger')
            cur.close()
            return redirect('/login')

        clave_ok = False
        if usuario:
            # Mas de 72 bytes hace que bcrypt lance ValueError: se trata como
            # clave invalida en vez de devolver un 500 ante una peticion ajena.
            if len(password.encode('utf-8')) <= 72:
                try:
                    clave_ok = bcrypt.check_password_hash(usuario['password'], password)
                except (ValueError, TypeError):
                    clave_ok = False
        if usuario and clave_ok:
            # La fila NO se borra y el contador NO se baja: queda el historial de
            # los fallos de esta cuenta para poder revisarlo.
            sombra.pop(correo, None)
            session[INTENTOS_SOMBRA] = sombra
            session['user_id'] = usuario['id']
            session['correo']   = usuario['correo']
            session['rol']      = usuario['rol'].lower()
            # Nombre/apellido para mostrar en el navbar (solo primer nombre y
            # primer apellido). Pueden ser NULL en cuentas antiguas.
            session['nombre']   = (usuario.get('nombre') or '').strip()
            session['apellido'] = (usuario.get('apellido') or '').strip()
            flash('Bienvenido', 'success')
            if session['rol'] in ['admin','administrador']:
                # NO llamar a init_db() aqui. Ya se ejecuta una sola vez al
                # arrancar (_try_init_db_once) y, si fallo, el fallback de
                # before_request (_ensure_db_fallback) lo reintenta antes de
                # llegar a esta vista. Repetirlo en cada login costaba ~90
                # sentencias x ~190 ms de ida y vuelta = ~16 s de espera.
                return redirect('/dashboard')
            return redirect('/')

        # ── Credenciales incorrectas: contar y decidir ──
        #
        # El numero de fallo se toma del contador a la sombra si la fila aun no
        # existe, y de la fila en cuanto ya existe. Como la fila nunca se borra
        # ni reinicia su contador, el total es acumulativo para ese correo.
        ahora = datetime.now()
        if bloqueo_usuario:
            intentos = (bloqueo_usuario['intentos'] or 0) + 1
        else:
            intentos = _leer_sombra(sombra, correo, ahora) + 1

        # Duracion del castigo para este fallo, segun la escalera.
        castigo = minutos_bloqueo(intentos) if intentos >= PRIMER_BLOQUEO_FALLO else 0
        # El pico historico nunca baja, aunque el contador se reinicie por
        # decaimiento. Asi "esta cuenta llego a 12" sigue siendo legible.
        # Se lee con .get() porque la columna la agrega un ALTER en init_db:
        # si alguien entra antes de que se aplique, no debe reventar el login.
        pico = max(intentos, (bloqueo_usuario.get('max_intentos') or 0) if bloqueo_usuario else 0)

        if bloqueo_usuario:
            if castigo:
                bloqueo_hasta = ahora + timedelta(minutes=castigo)
                cur.execute("UPDATE intentos_usuario SET intentos=%s, bloqueado_hasta=%s, "
                            "ultimo_fallo=%s, max_intentos=%s WHERE correo=%s",
                            (intentos, bloqueo_hasta, ahora, pico, correo))
                flash(f'Usuario bloqueado por {humanizar_bloqueo(timedelta(minutes=castigo))}.', 'danger')
                if bloqueo_ip:
                    usuarios_dif = bloqueo_ip['usuarios_diferentes'] + 1
                    if usuarios_dif >= 2:
                        cur.execute("UPDATE bloqueos_ip SET usuarios_diferentes=%s, bloqueado_hasta=%s WHERE ip=%s",
                                    (usuarios_dif, datetime.now()+timedelta(minutes=10), ip))
                        flash('IP bloqueada por actividad sospechosa.', 'danger')
                    else:
                        cur.execute("UPDATE bloqueos_ip SET usuarios_diferentes=%s WHERE ip=%s",
                                    (usuarios_dif, ip))
                else:
                    cur.execute("INSERT INTO bloqueos_ip(ip, usuarios_diferentes) VALUES(%s,1)", (ip,))
            else:
                cur.execute("UPDATE intentos_usuario SET intentos=%s, ultimo_fallo=%s, max_intentos=%s "
                            "WHERE correo=%s", (intentos, ahora, pico, correo))
                sombra.pop(correo, None)
                flash(f'Credenciales incorrectas. Te quedan {PRIMER_BLOQUEO_FALLO - intentos} '
                      f'intento(s) antes del bloqueo.', 'warning')
        elif intentos >= UMBRAL_INTENTOS:
            # Primera vez que la fila aparece: la cuenta ya llega al umbral.
            cur.execute("INSERT INTO intentos_usuario(correo, intentos, ultimo_fallo, max_intentos) "
                        "VALUES(%s,%s,%s,%s)", (correo, intentos, ahora, pico))
            sombra.pop(correo, None)
            if castigo:
                bloqueo_hasta = ahora + timedelta(minutes=castigo)
                cur.execute("UPDATE intentos_usuario SET bloqueado_hasta=%s WHERE correo=%s",
                            (bloqueo_hasta, correo))
                flash(f'Usuario bloqueado por {humanizar_bloqueo(timedelta(minutes=castigo))}.', 'danger')
            else:
                flash(f'Credenciales incorrectas. Te quedan {PRIMER_BLOQUEO_FALLO - intentos} '
                      f'intento(s) antes del bloqueo.', 'warning')
        else:
            # Aun bajo el umbral: se cuenta en la sombra, sin tocar la base.
            sombra[correo] = {'n': intentos, 't': ahora.timestamp()}
            flash(f'Credenciales incorrectas. Te quedan {PRIMER_BLOQUEO_FALLO - intentos} '
                  f'intento(s) antes del bloqueo.', 'warning')

        session[INTENTOS_SOMBRA] = sombra
        mysql.connection.commit()
        return redirect('/login')
    return render_template('Inicio.html')

@app.route('/logout')
@app.route('/Cerrar Sesion')
def logout():
    session.clear()
    return redirect('/')

@app.route('/registro', methods=['GET','POST'])
def registro():
    if request.method == 'POST':
        # reCAPTCHA v2: se valida antes de leer el formulario y antes de la BD.
        # El registro es el formulario que crea cuentas: sin captcha, cualquiera
        # puede llenar la tabla usuarios con registros automaticos.
        captcha_ok, captcha_msg = _recaptcha_valido(
            request.form.get('g-recaptcha-response'), obtener_ip())
        if not captcha_ok:
            flash(captcha_msg, 'danger')
            return redirect('/registro')

        correo           = request.form['correo'].strip()
        password         = request.form['password']
        confirmar        = request.form['confirmar']
        nombres          = request.form.get('nombres', '').strip()
        apellidos        = request.form.get('apellidos', '').strip()
        documento_tipo   = request.form.get('documento_tipo', 'dni')
        documento_numero = request.form.get('documento_numero', '').strip()
        telefono         = request.form.get('telefono', '').strip()
        if password != confirmar:
            flash('Las contraseñas no coinciden.', 'danger')
            return redirect('/registro')
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", correo):
            flash('Ingresa un correo electrónico válido.', 'danger')
            return redirect('/registro')
        ok, msg = _password_valida(password)
        if not ok:
            flash(msg, 'danger')
            return redirect('/registro')
        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        if cur.fetchone():
            flash('Ya existe una cuenta con ese correo.', 'danger')
            return redirect('/registro')
        h = bcrypt.generate_password_hash(password).decode('utf-8')
        cur.execute("INSERT INTO usuarios (correo, password, rol, documento_tipo, documento_numero, nombre, apellido, telefono) VALUES (%s,%s,'cliente',%s,%s,%s,%s,%s)",
                    (correo, h, documento_tipo, documento_numero, nombres, apellidos, telefono))
        mysql.connection.commit()
        flash('¡Registro exitoso! Ya puedes iniciar sesión.', 'success')
        return redirect('/login')
    return render_template('registro.html')

# ─────────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────────
@app.route('/dashboard')
def dashboard():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()

    # Una venta cancelada no cuenta como venta: ni en el conteo ni en los ingresos.
    cur.execute("SELECT COUNT(*) AS total FROM ventas WHERE COALESCE(estado,'') <> 'cancelado'")
    total_ventas = cur.fetchone()['total'] or 0

    # Ingresos totales = SOLO ventas ENTREGADAS
    cur.execute("SELECT COALESCE(SUM(total), 0) AS total FROM ventas WHERE estado='entregado'")
    ingresos_totales = float(cur.fetchone()['total'] or 0)

    # Monto en espera = ventas con estado 'en espera'
    cur.execute("SELECT COALESCE(SUM(total), 0) AS total FROM ventas WHERE estado='en espera'")
    monto_en_espera = float(cur.fetchone()['total'] or 0)

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos WHERE estado='activo'")
    productos_activos = cur.fetchone()['total'] or 0

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos WHERE estado='activo' AND stock <= 5")
    stock_bajo = cur.fetchone()['total'] or 0

    cur.execute("SELECT COUNT(*) AS total FROM usuarios")
    total_usuarios = cur.fetchone()['total'] or 0

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos_para_pedir WHERE estado='pendiente'")
    pedidos_pendientes = cur.fetchone()['total'] or 0

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.proveedores")
    total_proveedores = cur.fetchone()['total'] or 0

    cur.execute("SELECT COUNT(*) AS total FROM ventas WHERE estado='en espera'")
    ventas_pendientes = cur.fetchone()['total'] or 0

    cur.execute("SELECT COUNT(*) AS total FROM reclamaciones WHERE estado='pendiente'")
    reclamaciones_pendientes = cur.fetchone()['total'] or 0

    cur.execute("""
        SELECT v.id, v.total, v.fecha, v.estado, v.nombre, v.documento
        FROM ventas v ORDER BY v.fecha DESC LIMIT 5
    """)
    ultimas_ventas = cur.fetchall()

    cur.execute(f"""
        SELECT id, nombre, stock, categoria, imagen
        FROM {ALMACEN_DB}.productos
        WHERE estado='activo' AND stock <= 5
        ORDER BY stock ASC LIMIT 10
    """)
    productos_stock_bajo = cur.fetchall()

    cur.close()
    return render_template('dashboard.html',
                           total_ventas=total_ventas,
                           ingresos_totales=ingresos_totales,
                           monto_en_espera=monto_en_espera,
                           productos_activos=productos_activos,
                           stock_bajo=stock_bajo,
                           total_usuarios=total_usuarios,
                           pedidos_pendientes=pedidos_pendientes,
                           total_proveedores=total_proveedores,
                           ventas_pendientes=ventas_pendientes,
                           reclamaciones_pendientes=reclamaciones_pendientes,
                           ultimas_ventas=ultimas_ventas,
                           productos_stock_bajo=productos_stock_bajo)

@app.route('/admin')
def admin():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return "Acceso denegado"
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()

    where = "WHERE p.nombre LIKE %s"
    params = [f'%{buscar}%']
    sql_count = f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos p {where}"
    sql_data = f"""SELECT p.* FROM {ALMACEN_DB}.productos p {where}
                   ORDER BY p.id DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos_para_pedir WHERE estado='pendiente'")
    r = cur.fetchone()
    pedidos_pendientes = r['total'] if r else 0

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.proveedores")
    r2 = cur.fetchone()
    total_proveedores = r2['total'] if r2 else 0

    cur.close()
    return render_template('admin.html',
                           productos=items, buscar=buscar,
                           page=page, total_pages=total_pages,
                           total=total, has_prev=page > 1, has_next=page < total_pages,
                           pedidos_pendientes=pedidos_pendientes,
                           total_proveedores=total_proveedores)

# ─────────────────────────────────────────────
# PRODUCTOS CRUD
# ─────────────────────────────────────────────
# Tipo real de la imagen por sus primeros bytes. No se confia en el nombre ni
# en el Content-Type que declara el navegador: un .html subido como "imagen"
# se serviria desde el mismo dominio y podria ejecutar JavaScript.
_FIRMAS_IMAGEN = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)
_EXT_IMAGEN = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/avif': '.avif',
    'image/heic': '.heic',
}
_MIMES_IMAGEN = frozenset(_EXT_IMAGEN)


def _mime_imagen(datos):
    """Tipo real de la imagen, o None si no es una de las admitidas."""
    if not datos:
        return None
    for firma, mime in _FIRMAS_IMAGEN:
        if datos.startswith(firma):
            return mime
    if datos[:4] == b'RIFF' and datos[8:12] == b'WEBP':
        return 'image/webp'
    if datos[4:8] == b'ftyp':
        marca = datos[8:12]
        if marca in (b'avif', b'avis'):
            return 'image/avif'
        if marca in (b'heic', b'heix', b'hevc', b'hevx', b'mif1'):
            return 'image/heic'
    return None


def _procesar_imagen_producto(imagen):
    """Procesa una imagen subida y devuelve (ruta_relativa, bytes, mime).

    Los bytes se guardan ademas en la BD (columnas imagen_datos/imagen_mime)
    porque el disco de Render es efimero: el archivo se veia unos minutos y
    luego desaparecia. El archivo en disco queda como cache del worker y la
    ruta relativa mantiene compatibilidad con plantillas existentes."""
    if not imagen or not imagen.filename:
        return None, None, None
    datos = imagen.read()
    mime = _mime_imagen(datos)
    if not mime:
        raise ValueError('El archivo no es una imagen valida (JPG, PNG, GIF, WEBP o AVIF).')
    base = os.path.splitext(secure_filename(imagen.filename))[0][:60] or 'imagen'
    fn = secrets.token_hex(8) + '_' + base + _EXT_IMAGEN[mime]
    try:
        with open(os.path.join(app.config['UPLOAD_FOLDER'], fn), 'wb') as f:
            f.write(datos)
    except Exception:
        pass
    return 'uploads/' + fn, datos, mime


MAX_IMAGENES_PRODUCTO = 4


def _archivos_imagenes_producto(nombre_campo='imagenes', nombre_unico='imagen'):
    """Archivos de imagen subidos para un producto (de 1 a 4).

    Acepta el campo multiple `imagenes` y, por compatibilidad con formularios
    antiguos, el campo individual `imagen`."""
    archivos = [f for f in request.files.getlist(nombre_campo) if f and f.filename]
    if not archivos:
        unico = request.files.get(nombre_unico)
        if unico and unico.filename:
            archivos = [unico]
    return archivos


def _procesar_imagenes_producto(archivos):
    """Valida y procesa los archivos. Devuelve [(ruta, bytes, mime), ...]."""
    if not archivos:
        raise ValueError('Debes subir al menos una imagen del producto.')
    if len(archivos) > MAX_IMAGENES_PRODUCTO:
        raise ValueError(
            f'Puedes subir como maximo {MAX_IMAGENES_PRODUCTO} imagenes por producto.'
        )
    return [_procesar_imagen_producto(f) for f in archivos]


def _imagenes_extra(cur, producto_ids):
    """Mapa {producto_id: [rutas]} con las imagenes adicionales de cada producto."""
    ids = [int(i) for i in producto_ids if i is not None]
    if not ids:
        return {}
    marcas = ','.join(['%s'] * len(ids))
    cur.execute(
        f"SELECT producto_id, imagen FROM {ALMACEN_DB}.productos_imagenes "
        f"WHERE producto_id IN ({marcas}) AND imagen IS NOT NULL "
        f"ORDER BY producto_id, orden, id",
        tuple(ids),
    )
    mapa = {}
    for fila in cur.fetchall():
        mapa.setdefault(fila['producto_id'], []).append(fila['imagen'])
    return mapa


def _filas_extra(cur, producto_id):
    """Filas de las imagenes adicionales de un producto (id, imagen, orden)."""
    cur.execute(
        f"SELECT id, imagen, orden FROM {ALMACEN_DB}.productos_imagenes "
        f"WHERE producto_id=%s ORDER BY orden, id",
        (producto_id,),
    )
    return cur.fetchall()


def _insertar_extras(cur, producto_id, procesadas, orden_inicial=1):
    """Guarda las imagenes adicionales (todas menos la principal)."""
    for i, (ruta, datos, mime) in enumerate(procesadas, start=orden_inicial):
        cur.execute(
            f"INSERT INTO {ALMACEN_DB}.productos_imagenes "
            f"(producto_id, imagen, imagen_datos, imagen_mime, orden) "
            f"VALUES (%s,%s,%s,%s,%s)",
            (producto_id, ruta, datos, mime, i),
        )


@app.route('/agregar_producto', methods=['POST'])
def agregar_producto():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido. Intenta de nuevo.', 'danger')
        return redirect('/admin')
    nombre      = request.form['nombre']
    descripcion = request.form['descripcion']
    try:
        precio      = float(request.form['precio'])
        stock       = int(request.form['stock'])
    except (ValueError, TypeError):
        flash('Precio o stock con valor inválido.', 'danger')
        return redirect('/admin')
    categoria   = request.form.get('categoria', 'Otros')
    if precio < 0:
        flash('No se permiten precios negativos', 'danger')
        return redirect('/admin')
    try:
        procesadas = _procesar_imagenes_producto(_archivos_imagenes_producto())
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect('/admin')

    # La primera imagen es la principal (compatibilidad con listados/carrito);
    # las siguientes se guardan como galeria del producto.
    imagen_db, imagen_datos, imagen_mime = procesadas[0]
    extras = procesadas[1:]

    cur = mysql.connection.cursor()
    cur.execute(f"""
        INSERT INTO {ALMACEN_DB}.productos (nombre, descripcion, precio, stock, categoria, imagen, imagen_datos, imagen_mime)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (nombre, descripcion, precio, stock, categoria, imagen_db, imagen_datos, imagen_mime))
    nuevo_id = cur.lastrowid
    if extras:
        _insertar_extras(cur, nuevo_id, extras, orden_inicial=1)
    mysql.connection.commit()
    verificar_stock_bajo(cur, nuevo_id)
    mysql.connection.commit()
    return redirect('/admin')

@app.route('/editar_producto/<int:id>', methods=['GET','POST'])
def editar_producto(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/editar_producto/{id}')
        nombre      = request.form['nombre']
        descripcion = request.form['descripcion']
        try:
            precio      = float(request.form['precio'])
            stock       = int(request.form['stock'])
        except (ValueError, TypeError):
            flash('Precio o stock con valor inválido.', 'danger')
            return redirect(f'/editar_producto/{id}')
        categoria   = request.form['categoria']

        if precio < 0:
            flash('No se permiten precios negativos.', 'danger')
            return redirect(f'/editar_producto/{id}')
        if stock < 0:
            flash('No se permiten valores negativos en el stock.', 'danger')
            return redirect(f'/editar_producto/{id}')

        # ── Galeria: que se conserva, que se borra y que se agrega ──
        cur.execute(f"SELECT imagen FROM {ALMACEN_DB}.productos WHERE id=%s", (id,))
        actual = cur.fetchone()
        if not actual:
            cur.close()
            flash('Producto no encontrado.', 'danger')
            return redirect('/admin')
        extras_actuales = _filas_extra(cur, id)

        eliminar_ids = {int(v) for v in request.form.getlist('eliminar_imagen')
                        if str(v).isdigit()}
        quitar_principal = request.form.get('eliminar_principal') == '1'
        conservar = [f for f in extras_actuales if f['id'] not in eliminar_ids]

        archivos_nuevos = _archivos_imagenes_producto()
        try:
            archivo_principal = request.files.get('imagen')
            principal = None
            if archivo_principal and archivo_principal.filename:
                principal = _procesar_imagen_producto(archivo_principal)
            nuevos = _procesar_imagenes_producto(archivos_nuevos) if archivos_nuevos else []
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(f'/editar_producto/{id}')

        # Plan final antes de escribir nada en la BD:
        #   principal = imagen nueva que reemplaza a la principal
        #   mantener  = la principal actual se queda como esta
        #   si no hay principal, se promueve la primera imagen disponible.
        mantener = principal is None and bool(actual['imagen']) and not quitar_principal
        promover_fila = None
        if principal is None and not mantener:
            if conservar:
                promover_fila = conservar.pop(0)
            elif nuevos:
                principal = nuevos.pop(0)

        total = (1 if (principal is not None or mantener or promover_fila) else 0) \
            + len(conservar) + len(nuevos)
        if total > MAX_IMAGENES_PRODUCTO:
            flash(
                f'Un producto admite como maximo {MAX_IMAGENES_PRODUCTO} imagenes '
                f'(con estos cambios quedarian {total}). Elimina alguna antes de guardar.',
                'danger',
            )
            return redirect(f'/editar_producto/{id}')

        if promover_fila is not None:
            cur.execute(
                f"SELECT imagen_datos, imagen_mime FROM {ALMACEN_DB}.productos_imagenes "
                f"WHERE id=%s AND producto_id=%s",
                (promover_fila['id'], id),
            )
            blob = cur.fetchone() or {}
            principal = (promover_fila['imagen'], blob.get('imagen_datos'),
                         blob.get('imagen_mime'))
            cur.execute(
                f"DELETE FROM {ALMACEN_DB}.productos_imagenes WHERE id=%s AND producto_id=%s",
                (promover_fila['id'], id),
            )

        if principal is not None:
            imagen_db, imagen_datos, imagen_mime = principal
            cur.execute(f"""
                UPDATE {ALMACEN_DB}.productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s, imagen=%s, imagen_datos=%s, imagen_mime=%s WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, imagen_db, imagen_datos, imagen_mime, id))
        elif mantener:
            cur.execute(f"""
                UPDATE {ALMACEN_DB}.productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, id))
        else:
            cur.execute(f"""
                UPDATE {ALMACEN_DB}.productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s, imagen=NULL, imagen_datos=NULL, imagen_mime=NULL WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, id))

        for fila in extras_actuales:
            if fila['id'] in eliminar_ids:
                cur.execute(
                    f"DELETE FROM {ALMACEN_DB}.productos_imagenes "
                    f"WHERE id=%s AND producto_id=%s",
                    (fila['id'], id),
                )

        if nuevos:
            orden_base = max([int(f['orden'] or 0) for f in conservar], default=0) + 1
            _insertar_extras(cur, id, nuevos, orden_inicial=orden_base)

        mysql.connection.commit()
        verificar_stock_bajo(cur, id)
        mysql.connection.commit()
        cur.close()
        flash('Producto actualizado correctamente.', 'success')
        return redirect('/admin')

    cur.execute(f"SELECT {COLS_PRODUCTO} FROM {ALMACEN_DB}.productos WHERE id=%s", (id,))
    producto = cur.fetchone()
    if not producto:
        cur.close()
        flash('Producto no encontrado.', 'danger')
        return redirect('/admin')
    extras = _filas_extra(cur, id)
    cur.close()
    return render_template('editar_producto.html', producto=producto,
                           categorias=CATEGORIAS, extras=extras,
                           max_imagenes=MAX_IMAGENES_PRODUCTO)

@app.route('/eliminar_producto/<int:id>')
def eliminar_producto(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"UPDATE {ALMACEN_DB}.productos SET estado='inactivo' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    return redirect('/admin')

@app.route('/activar_producto/<int:id>')
def activar_producto(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"UPDATE {ALMACEN_DB}.productos SET estado='activo' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    return redirect('/admin')

@app.route('/eliminar_producto_definitivo/<int:id>')
def eliminar_producto_definitivo(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"DELETE FROM {ALMACEN_DB}.productos_imagenes WHERE producto_id=%s", (id,))
    cur.execute(f"DELETE FROM {ALMACEN_DB}.productos WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Producto eliminado permanentemente.', 'success')
    return redirect('/admin')

# ─────────────────────────────────────────────
# CONSULTAR DNI/RUC
# ─────────────────────────────────────────────
@app.route('/consultar/<tipo>/<numero>')
def consultar(tipo, numero):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return jsonify({'error': 'Acceso denegado'}), 403
    venta_id = request.args.get('venta_id')
    if not venta_id:
        return jsonify({'error': 'venta_id no recibido'})
    if tipo not in ['dni','ruc']:
        return jsonify({'error': 'Tipo inválido'})
    url = f"https://dniruc.apisperu.com/api/v1/{tipo}/{numero}?token={TOKEN}"
    try:
        data = requests.get(url, timeout=APIPERU_TIMEOUT).json()
        if 'error' in data:
            return jsonify(data)
        cur = mysql.connection.cursor()
        if tipo == 'dni':
            nombre = f"{data.get('nombres','')} {data.get('apellidoPaterno','')} {data.get('apellidoMaterno','')}"
        else:
            nombre = data.get('razonSocial','')
        cur.execute("UPDATE ventas SET documento=%s, nombre=%s WHERE id=%s", (numero, nombre, venta_id))
        mysql.connection.commit()
        cur.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)})

# ─────────────────────────────────────────────
# CONSULTA DNI/RUC PARA EL CHECKOUT (APIsPERU)
# ─────────────────────────────────────────────
# Caché en memoria: un DNI/RUC no cambia de un día para otro. Evita pagar dos
# veces la misma consulta y hace instantáneo el autocompletado al reintentar.
_CONSULTA_CACHE = {}
_CONSULTA_CACHE_MAX = 500
_CONSULTA_LOCK = threading.Lock()


def _texto_api(valor, maximo):
    """Normaliza un texto devuelto por APIsPERU (sin saltos ni espacios dobles)."""
    texto = ' '.join(str(valor or '').split())
    return texto[:maximo].upper()


def _consultar_documento_api(tipo, numero):
    """Consulta DNI/RUC en APIsPERU.

    Devuelve (datos, error). `datos` ya viene normalizado para el checkout y
    `error` es un mensaje listo para mostrar al cliente. El token de APIsPERU
    no sale nunca de esta función.
    """
    if not TOKEN:
        return None, ('La consulta automática de DNI/RUC no está configurada. '
                      'Puedes escribir tus datos a mano.')

    clave = (tipo, numero)
    ahora = time.time()
    with _CONSULTA_LOCK:
        guardado = _CONSULTA_CACHE.get(clave)
        if guardado and ahora - guardado[0] < APIPERU_CACHE_TTL:
            return dict(guardado[1]), None

    try:
        resp = requests.get(f'{APIPERU_URL}/{tipo}/{numero}',
                            params={'token': TOKEN}, timeout=APIPERU_TIMEOUT)
    except requests.exceptions.Timeout:
        return None, ('El servicio de consulta tardó demasiado. '
                      'Puedes escribir tus datos a mano.')
    except requests.exceptions.RequestException:
        return None, ('No se pudo conectar con el servicio de consulta. '
                      'Puedes escribir tus datos a mano.')

    if resp.status_code in (401, 403):
        # Token vencido o inválido. No se filtra el detalle del proveedor.
        print('[consultar_documento] APIsPERU rechazó el token (%s)'
              % resp.status_code)
        return None, ('La consulta automática no está disponible ahora. '
                      'Puedes escribir tus datos a mano.')
    if resp.status_code == 404:
        return None, ('El %s no existe o no está disponible. Revisa el número.'
                      % tipo.upper())
    if resp.status_code != 200:
        return None, ('El servicio de consulta no respondió bien. '
                      'Puedes escribir tus datos a mano.')

    try:
        api = resp.json()
    except ValueError:
        return None, ('El servicio de consulta devolvió una respuesta inválida. '
                      'Puedes escribir tus datos a mano.')
    if not isinstance(api, dict):
        return None, ('El servicio de consulta devolvió una respuesta inválida. '
                      'Puedes escribir tus datos a mano.')

    mensaje_api = str(api.get('message') or api.get('error') or '').strip()
    if api.get('success') is False or api.get('error'):
        return None, mensaje_api or ('No se encontró el documento. '
                                     'Revisa el número.')

    if tipo == 'dni':
        datos = {
            'nombres': _texto_api(api.get('nombres'), 100),
            'apellido_paterno': _texto_api(api.get('apellidoPaterno'), 50),
            'apellido_materno': _texto_api(api.get('apellidoMaterno'), 50),
            'estado': _texto_api(api.get('estado'), 30),
        }
        if not datos['nombres'] or not datos['apellido_paterno']:
            return None, (mensaje_api or 'El DNI no devolvió nombres. '
                          'Revisa el número o escribe tus datos a mano.')
    else:
        datos = {
            'razon_social': _texto_api(api.get('razonSocial'), 150),
            'direccion': _texto_api(api.get('direccion'), 200),
            'estado': _texto_api(api.get('estado'), 30),
            'condicion': _texto_api(api.get('condicion'), 30),
        }
        if not datos['razon_social']:
            return None, (mensaje_api or 'El RUC no devolvió la razón social. '
                          'Revisa el número o escribe tus datos a mano.')

    # Solo se guardan las consultas exitosas: un fallo temporal no debe quedar
    # cacheado durante 24 horas.
    with _CONSULTA_LOCK:
        if len(_CONSULTA_CACHE) >= _CONSULTA_CACHE_MAX:
            vencidos = [k for k, (t, _) in _CONSULTA_CACHE.items()
                        if ahora - t >= APIPERU_CACHE_TTL]
            for k in vencidos:
                _CONSULTA_CACHE.pop(k, None)
            if len(_CONSULTA_CACHE) >= _CONSULTA_CACHE_MAX:
                _CONSULTA_CACHE.pop(next(iter(_CONSULTA_CACHE)))
        _CONSULTA_CACHE[clave] = (ahora, datos)
    return dict(datos), None


@app.route('/api/consultar-documento', methods=['POST'])
def api_consultar_documento():
    """Autocompleta el checkout con los datos de RENIEC/SUNAT."""
    if 'user_id' not in session:
        return jsonify({'ok': False,
                        'error': 'Inicia sesión para consultar el documento.'}), 401

    data = request.get_json(silent=True) or {}
    tipo = str(data.get('tipo', '')).strip().lower()
    numero = re.sub(r'\D', '', str(data.get('numero', '')))
    if tipo not in ('dni', 'ruc'):
        return jsonify({'ok': False,
                        'error': 'Tipo de documento inválido.'}), 400
    esperado = 8 if tipo == 'dni' else 11
    if len(numero) != esperado:
        return jsonify({'ok': False,
                        'error': 'El %s debe tener %d dígitos.'
                                 % (tipo.upper(), esperado)}), 400

    # Límite por usuario además del límite por IP: protege la cuota del plan.
    if not _rate_permitido('doc:%s' % session['user_id'], 20, 60):
        return jsonify({'ok': False,
                        'error': 'Hiciste muchas consultas seguidas. '
                                 'Espera un minuto e intenta de nuevo.'}), 429

    datos, error = _consultar_documento_api(tipo, numero)
    if error:
        return jsonify({'ok': False, 'error': error})
    return jsonify({'ok': True, 'tipo': tipo, 'numero': numero, **datos})

# ─────────────────────────────────────────────
# TIENDA / CARRITO
# ─────────────────────────────────────────────
@app.route('/')
def index():
    buscar   = request.args.get('buscar','')
    categoria = request.args.get('categoria','Todos')
    cur = mysql.connection.cursor()
    sql = f"SELECT {COLS_PRODUCTO} FROM {ALMACEN_DB}.productos WHERE nombre LIKE %s AND estado='activo'"
    vals = [f'%{buscar}%']
    if categoria != 'Todos':
        sql += ' AND categoria=%s'
        vals.append(categoria)
    cur.execute(sql, tuple(vals))
    productos = cur.fetchall()
    extras = _imagenes_extra(cur, [p['id'] for p in productos])
    for p in productos:
        p['imagenes_extra'] = extras.get(p['id'], [])
    cur.close()
    # KPI: lo que el cliente escribe en el buscador. Sirve para saber que
    # busca la gente (y que no tenemos) y cuanto convierte el buscador.
    kpi_reg.registrar_busqueda(buscar, len(productos))
    return render_template('index.html', productos=productos, categorias=CATEGORIAS)

@app.route('/carrito')
def ver_carrito():
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute(f"""
        SELECT c.id, c.producto_id, p.nombre, p.precio, c.cantidad, p.stock
        FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id
        WHERE c.usuario_id=%s
    """, (usuario,))
    productos = cur.fetchall()
    total = sum(p['precio'] * p['cantidad'] for p in productos)

    # Productos disponibles para sugerir en el carrito (igual que la portada),
    # excluyendo los que ya están en el carrito. Máx. 12 → fila 3x4.
    en_carrito = [p['producto_id'] for p in productos]
    sugeridos = []
    if en_carrito:
        placeholders = ','.join(['%s'] * len(en_carrito))
        cur.execute(f"""
            SELECT {COLS_PRODUCTO} FROM {ALMACEN_DB}.productos
            WHERE estado='activo' AND stock>0 AND id NOT IN ({placeholders})
            ORDER BY id DESC LIMIT 12
        """, tuple(en_carrito))
    else:
        cur.execute(f"""
            SELECT {COLS_PRODUCTO} FROM {ALMACEN_DB}.productos
            WHERE estado='activo' AND stock>0
            ORDER BY id DESC LIMIT 12
        """)
    sugeridos = cur.fetchall()
    cur.close()
    return render_template('carrito.html', productos=productos, total=total, sugeridos=sugeridos)


# Tope de unidades por linea de carrito. Si el stock es menor, manda el stock:
# el maximo real es min(stock, CANTIDAD_MAXIMA).
CANTIDAD_MAXIMA = 500


def _total_carrito(usuario):
    cur = mysql.connection.cursor()
    cur.execute(
        f"SELECT SUM(c.cantidad * p.precio) AS total FROM carrito c "
        f"JOIN {ALMACEN_DB}.productos p ON c.producto_id = p.id "
        f"WHERE c.usuario_id = %s",
        (usuario,),
    )
    t = cur.fetchone()['total']
    cur.close()
    return round(float(t or 0), 2)


@app.route('/actualizar-cantidad/<int:id_producto>', methods=['POST'])
def actualizar_cantidad(id_producto):
    usuario = obtener_usuario()
    if not request.is_json:
        return jsonify({'error': 'Request debe ser JSON'}), 400
    accion = request.json.get('accion')

    # 'fijar' = edicion directa del numero (optimizacion)
    if accion == 'fijar':
        try:
            cantidad = int(request.json.get('cantidad'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Cantidad invalida'}), 400
    elif accion in ('aumentar', 'reducir'):
        cantidad = None   # se calcula mas abajo
    else:
        return jsonify({'error': 'Accion invalida'}), 400

    cur = mysql.connection.cursor()
    cur.execute(
        f"SELECT c.cantidad, p.precio, p.stock FROM carrito c "
        f"JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id "
        f"WHERE c.producto_id=%s AND c.usuario_id=%s",
        (id_producto, usuario),
    )
    fila = cur.fetchone()
    if not fila:
        cur.close()
        return jsonify({'eliminado': True, 'total': _total_carrito(usuario)})

    actual = int(fila['cantidad'])
    stock = int(fila['stock'] if fila['stock'] is not None else 0)

    if accion == 'aumentar':
        nueva = actual + 1
    elif accion == 'reducir':
        nueva = actual - 1
    else:
        nueva = cantidad

    # Validaciones de limites.
    # El maximo real es el MENOR entre el stock disponible y el tope de 500:
    # si hay 100 unidades no se puede pedir 101; si hay 600, el tope es 500.
    if nueva > CANTIDAD_MAXIMA:
        cur.close()
        return jsonify({
            'error': True,
            'motivo': 'maximo',
            'maximo': CANTIDAD_MAXIMA,
            'stock': stock,
            'mensaje': f'No puedes comprar mas de {CANTIDAD_MAXIMA} unidades de un mismo producto.',
        }), 400

    if nueva > stock:
        cur.close()
        return jsonify({
            'error': True,
            'motivo': 'stock',
            'maximo': stock,
            'stock': stock,
            'mensaje': f'Stock insuficiente: solo hay {stock} unidades disponibles.',
        }), 400

    if nueva <= 0:
        cur.execute(
            "DELETE FROM carrito WHERE producto_id=%s AND usuario_id=%s",
            (id_producto, usuario),
        )
    else:
        cur.execute(
            "UPDATE carrito SET cantidad=%s WHERE producto_id=%s AND usuario_id=%s",
            (nueva, id_producto, usuario),
        )
    mysql.connection.commit()
    _carrito_invalidar(usuario)

    subtotal = round(nueva * float(fila['precio']), 2) if nueva > 0 else 0.0
    total = _total_carrito(usuario)
    maximo = min(stock, CANTIDAD_MAXIMA)
    cur.close()
    if nueva <= 0:
        return jsonify({
            'eliminado': True,
            'total': total,
            'maximo': maximo,
            'stock': stock,
        })
    return jsonify({
        'eliminado': False,
        'cantidad': nueva,
        'subtotal': subtotal,
        'total': total,
        'maximo': maximo,
        'stock': stock,
    })


# ─────────────────────────────────────────────
# BOLETA
# ─────────────────────────────────────────────
@app.route('/boleta')
def boleta():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("SELECT id FROM ventas WHERE cliente_id=%s ORDER BY fecha DESC LIMIT 1", (user_id,))
    ultima = cur.fetchone()
    cur.close()
    venta_id = ultima['id'] if ultima else None
    return render_template('boleta.html', venta_id=venta_id)

@app.route('/boleta/<int:venta_id>')
def boleta_form(venta_id):
    if 'user_id' not in session and not _es_admin():
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, cliente_id FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    cur.close()
    if not venta or not puede_ver_venta(venta):
        flash('No tienes acceso a esa boleta.', 'danger')
        return redirect('/')
    return render_template('boleta_form.html', venta_id=venta_id)

@app.route('/preview_boleta')
def preview_boleta():
    if 'user_id' not in session and ('rol' not in session or session['rol'] not in ['admin','administrador']):
        return redirect('/login')
    venta_id = request.args.get('venta_id')
    doc      = request.args.get('doc') or ''
    nombre   = request.args.get('nombre') or ''
    try:
        venta_id = int(venta_id)
    except (TypeError, ValueError):
        flash('Boleta no válida.', 'danger')
        return redirect('/')
    cur = mysql.connection.cursor()
    cur.execute("""SELECT id, cliente_id, documento, doc_tipo, nombre, apellido, telefono, correo,
                          total, fecha, metodo_pago, direccion_envio, notas_entrega
                   FROM ventas WHERE id=%s""", (venta_id,))
    venta = cur.fetchone()
    if not venta or not puede_ver_venta(venta):
        cur.close()
        flash('Venta no encontrada.', 'danger')
        return redirect('/')
    cur.execute(f"""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """, (venta_id,))
    productos = cur.fetchall()
    cur.close()
    total = sum(p['cantidad'] * p['precio'] for p in productos)
    # Datos del cliente desde la venta (los parámetros son solo respaldo)
    if not doc:
        doc = venta.get('documento') or ''
    if not nombre:
        nombre = ((venta.get('nombre') or '') + ' ' + (venta.get('apellido') or '')).strip()
    return render_template('preview_boleta.html', productos=productos, total=total,
                           doc=doc, nombre=nombre, venta=venta, venta_id=venta_id)

# ─────────────────────────────────────────────
# HISTORIAL
# ─────────────────────────────────────────────
@app.route('/historial')
def historial():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT v.id, v.total, v.fecha, v.estado, c.motivo_codigo, c.motivo_detalle
        FROM ventas v LEFT JOIN cancelaciones c ON c.venta_id=v.id
        WHERE v.cliente_id=%s ORDER BY v.fecha DESC
    """, (user_id,))
    ventas = cur.fetchall()
    hist = []
    for v in ventas:
        cur.execute(f"""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['id'],))
        hist.append({'id':v['id'],'total':v['total'],'fecha':v['fecha'],
                     'estado':v['estado'],
                     'motivo_codigo':v.get('motivo_codigo'),
                     'motivo_detalle':v.get('motivo_detalle'),
                     'productos':cur.fetchall()})
    cur.close()
    return render_template('historial.html', historial=hist)

@app.route('/historial-compras')
def historial_compras():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    buscar = request.args.get('buscar','')
    motivo_filtro = request.args.get('motivo','').strip()
    mostrar_ocultas = request.args.get('ocultas', '0') == '1'
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    # TOTAL GENERAL = SOLO ventas ENTREGADAS
    cur.execute("SELECT SUM(total) AS gran_total FROM ventas WHERE COALESCE(oculta,0)=0 AND estado='entregado'")
    res = cur.fetchone()
    gran_total = res['gran_total'] or 0

    # Monto en espera = ventas con estado 'en espera' (no ocultas)
    cur.execute("SELECT SUM(total) AS monto_espera FROM ventas WHERE COALESCE(oculta,0)=0 AND estado='en espera'")
    res = cur.fetchone()
    monto_en_espera = res['monto_espera'] or 0
    join_cancel = "LEFT JOIN cancelaciones c ON c.venta_id=v.id"
    ocultas_cond = "" if mostrar_ocultas else "AND COALESCE(v.oculta,0)=0"
    where = f"WHERE u.correo LIKE %s {ocultas_cond}"
    params = [f'%{buscar}%']
    if motivo_cancelacion_valido(motivo_filtro):
        where += " AND c.motivo_codigo=%s"
        params.append(motivo_filtro)
    sql_count = f"SELECT COUNT(*) AS total FROM ventas v JOIN usuarios u ON v.cliente_id=u.id {join_cancel} {where}"
    sql_data = f"""SELECT v.id, u.correo, u.id AS cliente_id, v.total, v.fecha, v.documento, v.nombre AS titular, v.estado,
        COALESCE(v.oculta,0) AS oculta, c.motivo_codigo, c.motivo_detalle, c.cancelado_por
        FROM ventas v JOIN usuarios u ON v.cliente_id=u.id {join_cancel}
        {where} ORDER BY v.fecha DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)
    historial = []
    for v in items:
        cur.execute(f"""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['id'],))
        historial.append({**v, 'productos': cur.fetchall()})
    n_ocultas = 0
    try:
        cur.execute("SELECT COUNT(*) AS n FROM ventas WHERE oculta=1")
        n_ocultas = cur.fetchone()['n'] or 0
    except Exception:
        pass
    try:
        cur.execute("SELECT motivo_codigo, COUNT(*) AS n FROM cancelaciones GROUP BY motivo_codigo")
        conteo_motivos = {r['motivo_codigo']: r['n'] for r in cur.fetchall()}
    except Exception:
        conteo_motivos = {}
    cur.close()
    return render_template('historial_compras_admin.html', historial=historial, buscar=buscar,
                           motivo_filtro=motivo_filtro, conteo_motivos=conteo_motivos,
                           mostrar_ocultas=mostrar_ocultas, n_ocultas=n_ocultas,
                           gran_total=gran_total, monto_en_espera=monto_en_espera,
                           page=page, total_pages=total_pages,
                           total=total, has_prev=page > 1, has_next=page < total_pages)


# ═══════════════════════════════════════════════════════════════════
# REPORTE DE VENTAS CANCELADAS (panel admin)
# ═══════════════════════════════════════════════════════════════════
# No hay tabla nueva: todo se agrega en vivo desde `ventas` (total, estado,
# fecha) + `cancelaciones` (motivo, quién) + `detalle_venta` (productos).
# Una tabla física duplicaría datos que ya existen y podría desincronizarse.
_PRESETS_PERIODO = ('hoy', '7d', '30d', 'mes', 'todo', 'personalizado')


def _parse_fecha(valor):
    """Acepta dd/mm/aaaa o aaaa-mm-dd. Devuelve datetime o None."""
    valor = (valor or '').strip()
    if not valor:
        return None
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(valor, fmt)
        except (ValueError, TypeError):
            continue
    return None


def resolver_periodo_cancelaciones(args):
    """Rango [desde, hasta) para el reporte, con presets tipo KPI.

    'todo' arranca en 1970 y termina ahora: permite ver el historial completo
    sin tener que inventar una fecha de inicio.

    El `hasta` se calcula con holgura (un dia extra) porque el reloj del
    servidor MySQL puede ir adelantado respecto al local; sin ese margen las
    ventas canceladas de hoy quedarian fuera del rango.
    """
    hoy = datetime.now()
    inicio_dia = hoy.replace(hour=0, minute=0, second=0, microsecond=0)
    preset = (args.get('preset') or '30d').strip()
    if preset not in _PRESETS_PERIODO:
        preset = '30d'

    fin = hoy + timedelta(days=1)  # holgura por desfase de zona horaria
    if preset == 'hoy':
        desde, hasta = inicio_dia, fin
    elif preset == '7d':
        desde, hasta = hoy - timedelta(days=7), fin
    elif preset == '30d':
        desde, hasta = hoy - timedelta(days=30), fin
    elif preset == 'mes':
        desde, hasta = datetime(hoy.year, hoy.month, 1), fin
    elif preset == 'todo':
        desde, hasta = datetime(1970, 1, 1), fin
    else:  # personalizado
        desde = _parse_fecha(args.get('desde')) or (hoy - timedelta(days=30))
        hasta_f = _parse_fecha(args.get('hasta')) or hoy
        desde = desde.replace(hour=0, minute=0, second=0, microsecond=0)
        # El dia "hasta" es inclusivo: se suma un dia y medio de holgura.
        hasta = (hasta_f + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)

    if desde > hasta:
        desde, hasta = hasta, desde
    return {'preset': preset, 'desde': desde, 'hasta': hasta}


def _where_cancelaciones(periodo, motivo, quien, buscar):
    """Construye el WHERE del reporte y sus parametros.

    El filtro de estado es 'cancelado' (regla transversal: una venta cancelada
    no es una venta). LEFT JOIN a `cancelaciones` para no perder las ventas
    canceladas que no tengan motivo registrado.
    """
    where = ["COALESCE(v.estado,'') = 'cancelado'",
             "v.fecha >= %s", "v.fecha < %s"]
    params = [periodo['desde'], periodo['hasta']]
    if motivo_cancelacion_valido(motivo):
        where.append("COALESCE(c.motivo_codigo,'sin_motivo') = %s")
        params.append(motivo)
    elif motivo == MOTIVO_SIN_REGISTRO:
        where.append("COALESCE(c.motivo_codigo,'sin_motivo') = %s")
        params.append(MOTIVO_SIN_REGISTRO)
    if quien in ('cliente', 'admin'):
        where.append("COALESCE(c.cancelado_por,'cliente') = %s")
        params.append(quien)
    if buscar:
        where.append("(u.correo LIKE %s OR v.documento LIKE %s OR v.nombre LIKE %s)")
        like = f'%{buscar}%'
        params.extend([like, like, like])
    return " AND ".join(where), params


def datos_reporte_cancelaciones(periodo, motivo='', quien='', buscar=''):
    """Consulta todos los agregados del reporte en una sola pasada.

    Devuelve dict con: resumen, por_motivo, por_producto, detalle.
    Es tolerante a fallos: si algo rompe, devuelve estructura vacia para que
    la pagina muestre 'sin datos' en vez de un 500.
    """
    vacio = {
        'resumen': {'monto_cancelado': 0.0, 'n_canceladas': 0, 'ticket_promedio': 0.0,
                    'monto_vendido': 0.0, 'pct_cancelado': 0.0},
        'por_motivo': [], 'por_producto': [], 'detalle': [],
    }
    try:
        cur = mysql.connection.cursor()
    except Exception:
        return vacio

    try:
        where, params = _where_cancelaciones(periodo, motivo, quien, buscar)
        # LEFT JOIN a usuarios: una venta cancelada de un cliente eliminado debe
        # seguir apareciendo en el reporte (no se pierde por el join).
        base = (f"FROM ventas v "
                f"LEFT JOIN usuarios u ON u.id = v.cliente_id "
                f"LEFT JOIN cancelaciones c ON c.venta_id = v.id "
                f"WHERE {where}")
        # Para productos el JOIN a detalle_venta va ANTES del WHERE (SQL no
        # admite un JOIN despues de WHERE).
        base_productos = (f"FROM ventas v "
                          f"JOIN detalle_venta d ON d.venta_id = v.id "
                          f"LEFT JOIN usuarios u ON u.id = v.cliente_id "
                          f"LEFT JOIN cancelaciones c ON c.venta_id = v.id "
                          f"LEFT JOIN {ALMACEN_DB}.productos p ON p.id = d.producto_id "
                          f"WHERE {where}")

        # ── Resumen general ──
        cur.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(v.total),0) AS monto {base}", tuple(params))
        fila = cur.fetchone() or {}
        n_canceladas = int(fila.get('n') or 0)
        monto_cancelado = float(fila.get('monto') or 0.0)

        # Monto total vendido del periodo (válidas + canceladas) para el %.
        try:
            cur.execute(
                "SELECT COALESCE(SUM(total),0) AS monto FROM ventas "
                "WHERE fecha >= %s AND fecha < %s",
                (periodo['desde'], periodo['hasta']))
            r = cur.fetchone() or {}
            monto_vendido = float(r.get('monto') or 0.0)
        except Exception:
            monto_vendido = 0.0

        pct_cancelado = (monto_cancelado / monto_vendido * 100) if monto_vendido else 0.0
        ticket_promedio = (monto_cancelado / n_canceladas) if n_canceladas else 0.0

        # ── Por motivo ──
        cur.execute(
            f"""SELECT COALESCE(c.motivo_codigo,'sin_motivo') AS motivo,
                       COUNT(*) AS n,
                       COALESCE(SUM(v.total),0) AS monto
                {base}
                GROUP BY motivo ORDER BY monto DESC""",
            tuple(params))
        por_motivo = []
        for r in cur.fetchall():
            m = float(r['monto'] or 0.0)
            por_motivo.append({
                'codigo': r['motivo'],
                'etiqueta': etiqueta_motivo(r['motivo']),
                'n': int(r['n'] or 0),
                'monto': m,
                'pct': (m / monto_cancelado * 100) if monto_cancelado else 0.0,
            })

        # ── Productos mas cancelados ──
        cur.execute(
            f"""SELECT d.producto_id,
                       COALESCE(p.nombre, CONCAT('Producto #', d.producto_id)) AS nombre,
                       SUM(d.cantidad) AS unidades,
                       COALESCE(SUM(d.cantidad * d.precio),0) AS monto,
                       COUNT(DISTINCT v.id) AS veces
                {base_productos}
                GROUP BY d.producto_id, nombre
                ORDER BY unidades DESC, monto DESC
                LIMIT 50""",
            tuple(params))
        por_producto = [{
            'producto_id': r['producto_id'],
            'nombre': r['nombre'],
            'unidades': int(r['unidades'] or 0),
            'monto': float(r['monto'] or 0.0),
            'veces': int(r['veces'] or 0),
        } for r in cur.fetchall()]

        # ── Detalle de ventas canceladas ──
        cur.execute(
            f"""SELECT v.id, v.total, v.fecha, v.documento, v.nombre AS titular,
                       COALESCE(u.correo, '(cliente eliminado)') AS correo,
                       COALESCE(c.motivo_codigo,'sin_motivo') AS motivo_codigo,
                       c.motivo_detalle, COALESCE(c.cancelado_por,'cliente') AS cancelado_por,
                       c.created_at AS cancelado_en
                {base}
                ORDER BY v.fecha DESC
                LIMIT 300""",
            tuple(params))
        detalle = []
        for r in cur.fetchall():
            detalle.append({
                'id': r['id'],
                'total': float(r['total'] or 0.0),
                'fecha': r['fecha'],
                'documento': r['documento'],
                'titular': r['titular'],
                'correo': r['correo'],
                'motivo_codigo': r['motivo_codigo'],
                'motivo_etiqueta': etiqueta_motivo(r['motivo_codigo']),
                'motivo_detalle': r['motivo_detalle'],
                'cancelado_por': r['cancelado_por'],
                'cancelado_en': r['cancelado_en'],
            })

        return {
            'resumen': {
                'monto_cancelado': round(monto_cancelado, 2),
                'n_canceladas': n_canceladas,
                'ticket_promedio': round(ticket_promedio, 2),
                'monto_vendido': round(monto_vendido, 2),
                'pct_cancelado': round(pct_cancelado, 2),
            },
            'por_motivo': por_motivo,
            'por_producto': por_producto,
            'detalle': detalle,
        }
    except Exception as exc:
        app.logger.warning('Reporte cancelaciones fallo: %s', exc)
        return vacio
    finally:
        cur.close()


def _es_admin():
    return 'rol' in session and session['rol'] in ['admin', 'administrador']


@app.route('/reporte-cancelaciones')
def reporte_cancelaciones():
    if not _es_admin():
        return redirect('/login')
    periodo = resolver_periodo_cancelaciones(request.args)
    motivo = (request.args.get('motivo') or '').strip()
    quien = (request.args.get('quien') or '').strip()
    buscar = (request.args.get('buscar') or '').strip()
    datos = datos_reporte_cancelaciones(periodo, motivo, quien, buscar)
    # Querystring para que los botones de exportar respeten los filtros activos.
    querystring = urlencode({
        'preset': periodo['preset'],
        'desde': periodo['desde'].strftime('%Y-%m-%d'),
        'hasta': periodo['hasta'].strftime('%Y-%m-%d'),
        'motivo': motivo,
        'quien': quien,
        'buscar': buscar,
    })
    return render_template('reporte_cancelaciones.html',
                           **datos, periodo=periodo, motivo_filtro=motivo,
                           quien_filtro=quien, buscar=buscar,
                           motivo_sin_registro=MOTIVO_SIN_REGISTRO,
                           querystring=querystring)


@app.route('/reporte-cancelaciones.csv')
def reporte_cancelaciones_csv():
    """Exporta el detalle de ventas canceladas a CSV (Excel-friendly)."""
    if not _es_admin():
        return redirect('/login')
    periodo = resolver_periodo_cancelaciones(request.args)
    motivo = (request.args.get('motivo') or '').strip()
    quien = (request.args.get('quien') or '').strip()
    buscar = (request.args.get('buscar') or '').strip()
    datos = datos_reporte_cancelaciones(periodo, motivo, quien, buscar)

    buf = io.StringIO()
    buf.write('\ufeff')  # BOM: Excel abre el UTF-8 con tildes correctamente
    w = csv.writer(buf, delimiter=';')
    w.writerow(['Reporte de ventas canceladas - Multiservicios Richard'])
    w.writerow([f"Periodo: {periodo['desde'].strftime('%d/%m/%Y')} a {periodo['hasta'].strftime('%d/%m/%Y')}"])
    w.writerow([])
    r = datos['resumen']
    w.writerow(['Monto total cancelado', f"S/ {r['monto_cancelado']:.2f}"])
    w.writerow(['Ventas canceladas', r['n_canceladas']])
    w.writerow(['Ticket promedio cancelado', f"S/ {r['ticket_promedio']:.2f}"])
    w.writerow(['% del monto vendido', f"{r['pct_cancelado']:.2f}%"])
    w.writerow([])
    w.writerow(['--- POR MOTIVO ---'])
    w.writerow(['Motivo', 'N ventas', 'Monto', '% del monto'])
    for m in datos['por_motivo']:
        w.writerow([m['etiqueta'], m['n'], f"{m['monto']:.2f}", f"{m['pct']:.2f}%"])
    w.writerow([])
    w.writerow(['--- PRODUCTOS MAS CANCELADOS ---'])
    w.writerow(['Producto', 'Unidades', 'Monto', 'Veces en pedidos cancelados'])
    for p in datos['por_producto']:
        w.writerow([p['nombre'], p['unidades'], f"{p['monto']:.2f}", p['veces']])
    w.writerow([])
    w.writerow(['--- DETALLE ---'])
    w.writerow(['Venta', 'Fecha', 'Cliente', 'Documento', 'Total', 'Motivo', 'Cancelado por', 'Detalle'])
    for d in datos['detalle']:
        w.writerow([d['id'], d['fecha'], d['correo'], d['documento'],
                    f"{d['total']:.2f}", d['motivo_etiqueta'],
                    d['cancelado_por'], (d['motivo_detalle'] or '')])

    resp = Response(buf.getvalue(), mimetype='text/csv')
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    nombre = f"cancelaciones_{periodo['desde'].strftime('%Y%m%d')}_{periodo['hasta'].strftime('%Y%m%d')}.csv"
    resp.headers['Content-Disposition'] = f'attachment; filename={nombre}'
    return resp


@app.route('/reporte-cancelaciones.pdf')
def reporte_cancelaciones_pdf():
    """Exporta el reporte de cancelaciones a PDF (sustento formal imprimible)."""
    if not _es_admin():
        return redirect('/login')
    periodo = resolver_periodo_cancelaciones(request.args)
    motivo = (request.args.get('motivo') or '').strip()
    quien = (request.args.get('quien') or '').strip()
    buscar = (request.args.get('buscar') or '').strip()
    datos = datos_reporte_cancelaciones(periodo, motivo, quien, buscar)
    r = datos['resumen']

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf)
    styles = getSampleStyleSheet()
    elems = []
    elems.append(Paragraph("<b>MULTISERVICIOS RICHARD</b>", styles['Title']))
    elems.append(Paragraph("Reporte de ventas canceladas", styles['Normal']))
    elems.append(Paragraph(
        f"Periodo: {periodo['desde'].strftime('%d/%m/%Y')} a {periodo['hasta'].strftime('%d/%m/%Y')}",
        styles['Normal']))
    elems.append(Spacer(1, 0.3 * inch))

    resumen_data = [
        ["Monto total cancelado", f"S/ {r['monto_cancelado']:.2f}"],
        ["Ventas canceladas", str(r['n_canceladas'])],
        ["Ticket promedio cancelado", f"S/ {r['ticket_promedio']:.2f}"],
        ["% del monto vendido", f"{r['pct_cancelado']:.2f}%"],
    ]
    t0 = Table(resumen_data, colWidths=[220, 130])
    t0.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
    ]))
    elems.append(t0)
    elems.append(Spacer(1, 0.3 * inch))

    elems.append(Paragraph("<b>Por motivo</b>", styles['Heading3']))
    data = [["Motivo", "N ventas", "Monto", "% monto"]]
    for m in datos['por_motivo']:
        data.append([m['etiqueta'], str(m['n']), f"S/ {m['monto']:.2f}", f"{m['pct']:.2f}%"])
    t1 = Table(data, colWidths=[200, 60, 80, 70])
    t1.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
    ]))
    elems.append(t1)
    elems.append(Spacer(1, 0.3 * inch))

    elems.append(Paragraph("<b>Productos mas cancelados</b>", styles['Heading3']))
    data = [["Producto", "Unidades", "Monto", "Veces"]]
    for p in datos['por_producto'][:30]:
        data.append([p['nombre'][:45], str(p['unidades']), f"S/ {p['monto']:.2f}", str(p['veces'])])
    t2 = Table(data, colWidths=[230, 60, 80, 50])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
    ]))
    elems.append(t2)

    doc.build(elems)
    buf.seek(0)
    nombre = f"cancelaciones_{periodo['desde'].strftime('%Y%m%d')}_{periodo['hasta'].strftime('%Y%m%d')}.pdf"
    return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=nombre)


@app.route('/historial-compras/estado/<int:venta_id>', methods=['POST'])
def actualizar_estado_venta(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    estado = request.form.get('estado', 'en espera')
    if estado not in ['entregado', 'en espera', 'cancelado']:
        estado = 'en espera'
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, estado, total FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta:
        cur.close()
        flash('Venta no encontrada.', 'danger')
        return redirect('/historial-compras')
    anterior = venta['estado']
    # Cancelado es terminal: ni se reabre ni se edita su motivo
    if anterior == 'cancelado':
        cur.close()
        flash(f'Venta #{venta_id} ya está cancelada. Motivo no editable.', 'warning')
        return redirect('/historial-compras')
    cur.execute("UPDATE ventas SET estado=%s WHERE id=%s", (estado, venta_id))
    # Replicar al seguimiento de entregas si existe (para que se comuniquen)
    if estado == 'entregado':
        cur.execute("UPDATE seguimiento_entregas SET estado='entregado', fecha_entrega=NOW() WHERE venta_id=%s", (venta_id,))
    else:
        seg_estado = {'en espera': 'pendiente', 'cancelado': 'cancelado'}.get(estado, 'pendiente')
        cur.execute("UPDATE seguimiento_entregas SET estado=%s WHERE venta_id=%s", (seg_estado, venta_id))
    # Al cancelar como admin: devolver stock, reducir monto y registrar motivo
    if estado == 'cancelado' and anterior != 'cancelado':
        cur.execute(f"SELECT COALESCE(SUM(d.cantidad * d.precio), 0) AS monto_articulos "
                    f"FROM detalle_venta d WHERE d.venta_id=%s", (venta_id,))
        fila = cur.fetchone()
        monto_articulos = float(fila['monto_articulos'] or 0) if fila else 0.0
        nuevo_total = max(0.0, float(venta['total'] or 0) - monto_articulos)
        cur.execute("UPDATE ventas SET total=%s WHERE id=%s", (nuevo_total, venta_id))
        restaurar_stock_venta(cur, venta_id)
        registrar_cancelacion(cur, venta_id, 'otro', 'Cancelado por administrador', 'admin')
    mysql.connection.commit()
    _invalidar_cache_kpi()
    cur.close()
    flash(f'Venta #{venta_id} actualizada a "{estado}".', 'success')
    return redirect('/historial-compras')

@app.route('/historial-compras/ocultar/<int:venta_id>')
def ocultar_venta(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("UPDATE ventas SET oculta=1 WHERE id=%s", (venta_id,))
    mysql.connection.commit()
    cur.close()
    flash(f'Venta #{venta_id} oculta del panel. Sigue en la base de datos.', 'success')
    return redirect('/historial-compras')

@app.route('/historial-compras/mostrar/<int:venta_id>')
def mostrar_venta(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("UPDATE ventas SET oculta=0 WHERE id=%s", (venta_id,))
    mysql.connection.commit()
    cur.close()
    flash(f'Venta #{venta_id} visible de nuevo.', 'success')
    return redirect('/historial-compras')

@app.route('/historial-compras/limpiar')
def limpiar_historial():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM detalle_venta")
    cur.execute("DELETE FROM ventas")
    mysql.connection.commit()
    cur.close()
    flash('Todo el historial de compras ha sido eliminado.', 'success')
    return redirect('/historial-compras')

# ─────────────────────────────────────────────
# PERMISOS
# ─────────────────────────────────────────────
@app.route('/permisos', methods=['GET','POST'])
def permisos():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    es_superadmin = session.get('correo') == 'admin@mail.com'
    cur = mysql.connection.cursor()
    # Compatibilidad: crea la columna 'estado' si aún no existe
    try:
        cur.execute("ALTER TABLE usuarios ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        mysql.connection.commit()
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/permisos')
        if not es_superadmin:
            flash('Solo el administrador principal (admin@mail.com) puede cambiar roles.', 'danger')
            cur.close()
            return redirect('/permisos')
        user_id   = request.form.get('user_id')
        nuevo_rol = request.form.get('rol')
        if nuevo_rol in ['admin','cliente']:
            cur.execute("UPDATE usuarios SET rol=%s WHERE id=%s", (nuevo_rol, user_id))
            mysql.connection.commit()
            flash('Permisos actualizados correctamente.', 'success')
        else:
            flash('Rol no válido.', 'danger')
        cur.close()
        return redirect('/permisos')
    buscar = request.args.get('buscar','')
    page = int(request.args.get('page', 1))
    sql_count = "SELECT COUNT(*) AS total FROM usuarios WHERE correo LIKE %s"
    sql_data = """SELECT id, correo, rol, estado FROM usuarios WHERE correo LIKE %s
        ORDER BY CASE rol WHEN 'admin' THEN 0 WHEN 'administrador' THEN 1 ELSE 2 END, correo ASC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, (f'%{buscar}%',), page)
    cur.close()
    return render_template('permisos.html', usuarios=items, buscar=buscar,
                           es_superadmin=es_superadmin, page=page, total_pages=total_pages,
                           total=total, has_prev=page > 1, has_next=page < total_pages)

# ─────────────────────────────────────────────
# P1: CLIENTES — control de estado (admin)
# ─────────────────────────────────────────────
def _es_admin():
    return session.get('rol') in ['admin', 'administrador']

def _es_superadmin():
    return session.get('correo') == 'admin@mail.com'

@app.route('/clientes')
def clientes():
    if not _es_admin():
        return redirect('/login')
    buscar = request.args.get('buscar', '').strip()
    filtro = request.args.get('filtro', 'todos').strip()
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    where = "WHERE (u.correo LIKE %s OR COALESCE(u.nombre,'') LIKE %s OR COALESCE(u.apellido,'') LIKE %s)"
    params = [f'%{buscar}%', f'%{buscar}%', f'%{buscar}%']
    if filtro in ESTADOS_CLIENTE:
        where += " AND COALESCE(u.estado,'activo')=%s"
        params.append(filtro)
    sql_count = f"SELECT COUNT(*) AS total FROM usuarios u {where}"
    sql_data = f"""SELECT u.id, u.correo, u.nombre, u.apellido, u.telefono, u.rol,
        COALESCE(u.estado,'activo') AS estado, u.created_at,
        (SELECT COUNT(*) FROM ventas v WHERE v.cliente_id=u.id AND COALESCE(v.estado,'') <> 'cancelado') AS num_compras,
        (SELECT COALESCE(SUM(v2.total),0) FROM ventas v2 WHERE v2.cliente_id=u.id AND COALESCE(v2.estado,'') <> 'cancelado') AS total_compras
        FROM usuarios u {where} ORDER BY u.created_at DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)
    # Conteos por estado para las píldoras de filtro
    conteos = {}
    try:
        cur.execute("SELECT COALESCE(estado,'activo') AS e, COUNT(*) AS n FROM usuarios GROUP BY COALESCE(estado,'activo')")
        for r in cur.fetchall():
            conteos[r['e']] = r['n']
    except Exception:
        pass
    cur.close()
    return render_template('clientes.html', clientes=items, buscar=buscar, filtro=filtro,
                           conteos=conteos, page=page, total_pages=total_pages,
                           total=total, has_prev=page > 1, has_next=page < total_pages)

@app.route('/clientes/<int:user_id>/estado', methods=['POST'])
def cambiar_estado_cliente(user_id):
    if not _es_admin():
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect('/clientes')
    nuevo = (request.form.get('estado') or '').strip()
    motivo = (request.form.get('motivo') or '').strip()[:300]
    if not estado_cliente_valido(nuevo):
        flash('Estado no válido.', 'danger')
        return redirect('/clientes')
    if user_id == session.get('user_id') and nuevo in ('bloqueado', 'inactivo'):
        flash('No puedes bloquearte a ti mismo.', 'danger')
        return redirect('/clientes')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, correo, rol, COALESCE(estado,'activo') AS estado FROM usuarios WHERE id=%s", (user_id,))
    objetivo = cur.fetchone()
    if not objetivo:
        cur.close()
        flash('Cliente no encontrado.', 'danger')
        return redirect('/clientes')
    # Solo el superadmin puede cambiar el estado de otros admins
    if objetivo['rol'] in ('admin', 'administrador') and not _es_superadmin():
        cur.close()
        flash('Solo admin@mail.com puede cambiar el estado de otro administrador.', 'danger')
        return redirect('/clientes')
    anterior = objetivo['estado']
    if anterior == nuevo:
        cur.close()
        flash('El cliente ya está en ese estado.', 'info')
        return redirect('/clientes')
    cur.execute("UPDATE usuarios SET estado=%s WHERE id=%s", (nuevo, user_id))
    try:
        cur.execute(
            "INSERT INTO historial_estado_cliente (usuario_id, estado_anterior, estado_nuevo, motivo, admin_id)"
            " VALUES (%s,%s,%s,%s,%s)",
            (user_id, anterior, nuevo, motivo or None, session.get('user_id')))
    except Exception:
        pass
    mysql.connection.commit()
    cur.close()
    flash(f"Cliente {objetivo['correo']}: {anterior} → {nuevo}.", 'success')
    return redirect('/clientes')

@app.route('/clientes/<int:user_id>/historial')
def historial_estado_cliente(user_id):
    if not _es_admin():
        return jsonify({'ok': False}), 403
    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            SELECT h.id, h.estado_anterior, h.estado_nuevo, h.motivo, h.created_at,
                   a.correo AS admin_correo
            FROM historial_estado_cliente h
            LEFT JOIN usuarios a ON h.admin_id=a.id
            WHERE h.usuario_id=%s ORDER BY h.created_at DESC LIMIT 50
        """, (user_id,))
        filas = cur.fetchall()
    except Exception:
        filas = []
    cur.close()
    return jsonify({'ok': True, 'historial': [
        {'id': f['id'], 'anterior': f['estado_anterior'], 'nuevo': f['estado_nuevo'],
         'motivo': f['motivo'], 'admin': f.get('admin_correo'),
         'fecha': str(f['created_at'])} for f in filas
    ]})

# ─────────────────────────────────────────────
# PROVEEDORES
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# VALIDACION DE PROVEEDORES
# ─────────────────────────────────────────────
# Solo letras (con tildes/ñ) y espacios simples en nombres y apellidos;
# documento y celular solo digitos. La razon social de una empresa si admite
# digitos y signos (S.A.C., E.I.R.L., &), por eso tiene su propia regla.
# La validacion del navegador ayuda, pero estas comprobaciones son la
# autoridad: nadie puede saltarselas con un POST manual.
_RE_NOMBRE_PERSONA = re.compile(
    r'^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*$'
)
_RE_RAZON_SOCIAL = re.compile(
    r"^[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ&.\-'’,() ]+$"
)
# Tambien la usa la validacion del Libro de Reclamaciones.
_RE_CORREO = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_TIPOS_DOCUMENTO = {'dni': 8, 'ruc': 11}


def _validar_proveedor(form):
    """Valida y normaliza los datos del proveedor. Devuelve (datos, error).

    DNI -> nombres + apellido paterno + apellido materno (los tres obligatorios).
    RUC -> razon social (una empresa no tiene apellidos).
    En ambos casos se guarda `nombre` ya armado, que es lo que muestra el resto
    de la aplicacion. Todos los campos son obligatorios salvo las notas.
    """
    tipo      = (form.get('tipo_documento') or 'dni').strip().lower()
    numero    = re.sub(r'\D', '', form.get('numero_documento') or '')
    celular   = (form.get('celular') or '').strip()
    direccion = (form.get('direccion') or '').strip()
    categoria = (form.get('categoria') or '').strip()
    notas     = (form.get('notas') or '').strip()

    if tipo not in _TIPOS_DOCUMENTO:
        return None, 'Selecciona un tipo de documento válido (DNI o RUC).'
    esperado = _TIPOS_DOCUMENTO[tipo]
    if len(numero) != esperado:
        return None, f'El {tipo.upper()} debe tener exactamente {esperado} números.'

    if tipo == 'dni':
        nombres   = ' '.join((form.get('nombres') or '').split())
        ap_paterno = ' '.join((form.get('apellido_paterno') or '').split())
        ap_materno = ' '.join((form.get('apellido_materno') or '').split())
        for etiqueta, valor in (('nombres', nombres),
                                ('apellido paterno', ap_paterno),
                                ('apellido materno', ap_materno)):
            if not valor:
                return None, f'El campo {etiqueta} es obligatorio.'
            if not _RE_NOMBRE_PERSONA.match(valor) or sum(c.isalpha() for c in valor) < 2:
                return None, (f'El campo {etiqueta} solo puede contener letras y '
                              'espacios (sin números ni signos).')
        nombre = f'{nombres} {ap_paterno} {ap_materno}'[:200]
    else:
        nombres    = ' '.join((form.get('razon_social') or '').split())
        ap_paterno = ''
        ap_materno = ''
        if not nombres:
            return None, 'La razón social es obligatoria.'
        if not _RE_RAZON_SOCIAL.match(nombres) or sum(c.isalpha() for c in nombres) < 3:
            return None, ('La razón social solo puede contener letras, números y '
                          'los signos . & - \' , ( ).')
        nombres = nombres[:120]
        nombre  = nombres[:200]

    if not celular:
        return None, 'El celular es obligatorio.'
    if not re.fullmatch(r'\d{9}', celular):
        return None, 'El celular debe tener exactamente 9 números (solo dígitos).'
    if not direccion:
        return None, 'La dirección es obligatoria.'
    if not categoria:
        return None, 'La categoría es obligatoria.'

    return {
        'tipo_documento': tipo.upper(),
        'numero_documento': numero,
        'nombres': nombres,
        'apellido_paterno': ap_paterno,
        'apellido_materno': ap_materno,
        'nombre': nombre,
        'celular': celular,
        'direccion': direccion,
        'categoria': categoria,
        'notas': notas,
    }, None


def _proveedor_duplicado(cur, datos, excluir_id=None):
    """True si ya existe un proveedor con el mismo documento y categoria.

    Un mismo proveedor puede estar en varias categorias (una fila por
    categoria), pero no dos veces en la misma. Al editar se excluye la propia
    fila, para que guardar sin cambiar la categoria siga funcionando.
    """
    sql = f"""
        SELECT id FROM {ALMACEN_DB}.proveedores
        WHERE tipo_documento=%s AND numero_documento=%s AND categoria=%s
    """
    params = [datos['tipo_documento'], datos['numero_documento'], datos['categoria']]
    if excluir_id is not None:
        sql += " AND id<>%s"
        params.append(excluir_id)
    cur.execute(sql + " LIMIT 1", tuple(params))
    return cur.fetchone() is not None


@app.route('/proveedores', methods=['GET','POST'])
def proveedores():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/proveedores')
        datos, error = _validar_proveedor(request.form)
        if error:
            flash(error, 'danger')
            return redirect('/proveedores')
        if _proveedor_duplicado(cur, datos):
            flash('Ya hay un proveedor agregado con este nombre y categoría.', 'danger')
            return redirect('/proveedores')
        cur.execute(f"""
            INSERT INTO {ALMACEN_DB}.proveedores
                (nombre, tipo_documento, numero_documento, nombres,
                 apellido_paterno, apellido_materno, celular, direccion,
                 categoria, notas)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (datos['nombre'], datos['tipo_documento'], datos['numero_documento'],
              datos['nombres'], datos['apellido_paterno'], datos['apellido_materno'],
              datos['celular'], datos['direccion'], datos['categoria'], datos['notas']))
        mysql.connection.commit()
        flash(f'Proveedor "{datos["nombre"]}" agregado correctamente.', 'success')
        return redirect('/proveedores')

    buscar = request.args.get('buscar','')
    cat_filtro = request.args.get('categoria','')
    page = int(request.args.get('page', 1))
    where = "WHERE nombre LIKE %s"
    vals = [f'%{buscar}%']
    if cat_filtro:
        where += " AND categoria=%s"
        vals.append(cat_filtro)
    sql_count = f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.proveedores {where}"
    sql_data = f"SELECT * FROM {ALMACEN_DB}.proveedores {where} ORDER BY nombre ASC"
    # 10 por pagina solo en proveedores; el resto del panel sigue en PER_PAGE.
    items, total, page, total_pages = paginate_query(
        cur, sql_count, sql_data, tuple(vals), page, per_page=10)
    cur.close()
    return render_template('proveedores.html', proveedores=items,
                           categorias=CATEGORIAS, buscar=buscar, cat_filtro=cat_filtro,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages)

@app.route('/proveedores/editar/<int:id>', methods=['GET','POST'])
def editar_proveedor(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    # Se busca primero: si el id no existe, ni el formulario ni un POST
    # manual tienen sentido. Antes se mostraba el formulario vacio de alta.
    cur.execute(f"SELECT * FROM {ALMACEN_DB}.proveedores WHERE id=%s", (id,))
    p = cur.fetchone()
    if not p:
        cur.close()
        flash('Proveedor no encontrado.', 'danger')
        return redirect('/proveedores')
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/proveedores/editar/{id}')
        datos, error = _validar_proveedor(request.form)
        if error:
            flash(error, 'danger')
            return redirect(f'/proveedores/editar/{id}')
        # Solo se bloquea si el documento o la categoria cambian a una
        # combinacion que ya existe. Guardar sin tocar la categoria sigue
        # funcionando aunque haya filas repetidas de antes de esta regla.
        sin_cambio = (
            (p['tipo_documento'] or '').upper() == datos['tipo_documento']
            and (p['numero_documento'] or '') == datos['numero_documento']
            and (p['categoria'] or '') == datos['categoria'])
        if not sin_cambio and _proveedor_duplicado(cur, datos, excluir_id=id):
            flash('Ya hay un proveedor agregado con este nombre y categoría.', 'danger')
            return redirect(f'/proveedores/editar/{id}')
        cur.execute(f"""
            UPDATE {ALMACEN_DB}.proveedores SET
                nombre=%s, tipo_documento=%s, numero_documento=%s, nombres=%s,
                apellido_paterno=%s, apellido_materno=%s, celular=%s,
                direccion=%s, categoria=%s, notas=%s
            WHERE id=%s
        """, (datos['nombre'], datos['tipo_documento'], datos['numero_documento'],
              datos['nombres'], datos['apellido_paterno'], datos['apellido_materno'],
              datos['celular'], datos['direccion'], datos['categoria'], datos['notas'], id))
        mysql.connection.commit()
        cur.close()
        flash('Proveedor actualizado.', 'success')
        return redirect('/proveedores')
    cur.close()
    return render_template('proveedores.html', editar=p, categorias=CATEGORIAS,
                           proveedores=[], buscar='', cat_filtro='')

@app.route('/proveedores/eliminar/<int:id>')
def eliminar_proveedor(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"DELETE FROM {ALMACEN_DB}.proveedores WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Proveedor eliminado.', 'success')
    return redirect('/proveedores')

# ─────────────────────────────────────────────
# LIBRO DE RECLAMACIONES
# ─────────────────────────────────────────────
def _dias_para_limite(fecha_limite):
    """Dias que faltan para el plazo de respuesta (negativo si ya vencio)."""
    if not fecha_limite:
        return None
    return (fecha_limite.date() - datetime.now().date()).days


def _validar_reclamacion(form):
    """Valida la hoja de reclamacion. Devuelve (datos, errores)."""
    datos = {
        'tipo': (form.get('tipo') or 'reclamo').strip().lower(),
        'consumidor_nombre': ' '.join((form.get('consumidor_nombre') or '').split()),
        'consumidor_documento_tipo': (form.get('consumidor_documento_tipo') or 'DNI').strip().upper(),
        'consumidor_documento': (form.get('consumidor_documento') or '').strip(),
        'consumidor_domicilio': (form.get('consumidor_domicilio') or '').strip(),
        'consumidor_telefono': (form.get('consumidor_telefono') or '').strip(),
        'consumidor_correo': (form.get('consumidor_correo') or '').strip(),
        'bien_tipo': 'producto',
        'bien_descripcion': (form.get('bien_descripcion') or '').strip(),
        'bien_monto': (form.get('bien_monto') or '').strip(),
        'detalle': (form.get('detalle') or '').strip(),
        'pedido': (form.get('pedido') or '').strip(),
    }
    errores = []

    if datos['tipo'] not in ('reclamo', 'queja'):
        errores.append('Selecciona si tu caso es un reclamo o una queja.')

    nombre = datos['consumidor_nombre']
    if (not nombre or not _RE_NOMBRE_PERSONA.match(nombre)
            or sum(c.isalpha() for c in nombre) < 4):
        errores.append('El nombre completo debe tener al menos 4 letras '
                       '(solo letras y espacios).')

    tipo_doc = datos['consumidor_documento_tipo']
    doc = datos['consumidor_documento']
    if tipo_doc not in ('DNI', 'CE', 'PASAPORTE'):
        errores.append('Selecciona un tipo de documento válido.')
    elif tipo_doc == 'DNI':
        if not re.fullmatch(r'\d{8}', doc):
            errores.append('El DNI debe tener exactamente 8 números.')
    elif not re.fullmatch(r'[A-Za-z0-9]{6,12}', doc):
        errores.append('El carné o pasaporte debe tener entre 6 y 12 letras o números.')

    if len(datos['consumidor_domicilio']) < 5:
        errores.append('Ingresa tu domicilio completo (mínimo 5 caracteres).')

    tel = datos['consumidor_telefono']
    if tel and not re.fullmatch(r'\d{6,15}', tel):
        errores.append('El teléfono solo debe contener números (entre 6 y 15 dígitos).')

    correo = datos['consumidor_correo']
    if correo and not _RE_CORREO.match(correo):
        errores.append('El correo electrónico debe incluir @ y un dominio válido '
                       '(ejemplo@correo.com).')
    if not tel and not correo:
        errores.append('Ingresa al menos un teléfono o un correo electrónico de contacto.')

    if len(datos['bien_descripcion']) < 3:
        errores.append('Describe el producto comprado (mínimo 3 caracteres).')

    monto = None
    if datos['bien_monto']:
        try:
            monto = round(float(datos['bien_monto'].replace(',', '.')), 2)
            if monto < 0:
                raise ValueError
        except ValueError:
            errores.append('El monto debe ser un número mayor o igual a 0.')
    datos['bien_monto'] = monto

    if len(datos['detalle']) < 15:
        errores.append('Cuéntanos con detalle lo ocurrido (mínimo 15 caracteres).')
    if len(datos['pedido']) < 5:
        errores.append('Indica qué solución o pedido concreto esperas (mínimo 5 caracteres).')

    return datos, errores


@app.route('/libro-de-reclamaciones', methods=['GET', 'POST'])
def libro_reclamaciones():
    """Formulario publico del Libro de Reclamaciones."""
    valores = {}
    if request.method == 'POST':
        if not validate_csrf():
            flash('Tu sesión venció. Vuelve a enviar el formulario.', 'danger')
            return redirect('/libro-de-reclamaciones')

        datos, errores = _validar_reclamacion(request.form)
        if errores:
            for e in errores:
                flash(e, 'danger')
            valores = request.form.to_dict()
            return render_template('libro_reclamaciones.html', valores=valores), 400

        token = secrets.token_urlsafe(24)
        fecha = datetime.now()
        fecha_limite = fecha + timedelta(days=DIAS_RESPUESTA_RECLAMO)
        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO reclamaciones
              (codigo, token, fecha, tipo, consumidor_nombre,
               consumidor_documento_tipo, consumidor_documento,
               consumidor_domicilio, consumidor_telefono, consumidor_correo,
               bien_tipo, bien_descripcion, bien_monto, detalle, pedido,
               estado, fecha_limite)
            VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pendiente',%s)
        """, (
            token, fecha, datos['tipo'], datos['consumidor_nombre'],
            datos['consumidor_documento_tipo'], datos['consumidor_documento'],
            datos['consumidor_domicilio'], datos['consumidor_telefono'],
            datos['consumidor_correo'], datos['bien_tipo'],
            datos['bien_descripcion'], datos['bien_monto'], datos['detalle'],
            datos['pedido'], fecha_limite,
        ))
        rid = cur.lastrowid
        codigo = f'LR-{fecha.year}-{rid:06d}'
        cur.execute("UPDATE reclamaciones SET codigo=%s WHERE id=%s", (codigo, rid))
        mysql.connection.commit()
        cur.close()
        return redirect(url_for('constancia_reclamacion', token=token))

    return render_template('libro_reclamaciones.html', valores=valores)


@app.route('/libro-de-reclamaciones/constancia/<token>')
def constancia_reclamacion(token):
    """Copia para el consumidor. Solo accesible con el token del envio."""
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM reclamaciones WHERE token=%s", (token,))
    r = cur.fetchone()
    cur.close()
    if not r:
        flash('No se encontró la hoja de reclamación solicitada.', 'danger')
        return redirect('/libro-de-reclamaciones')
    return render_template('reclamacion_constancia.html', r=r,
                           dias=_dias_para_limite(r['fecha_limite']))


@app.route('/reclamaciones')
def reclamaciones():
    """Bandeja de reclamaciones para el administrador."""
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')

    estado = request.args.get('estado', '').strip()
    buscar = request.args.get('buscar', '').strip()
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()

    where, params = ["1=1"], []
    if buscar:
        where.append("(codigo LIKE %s OR consumidor_nombre LIKE %s OR consumidor_documento LIKE %s)")
        like = f'%{buscar}%'
        params.extend([like, like, like])
    if estado == 'pendiente':
        where.append("estado='pendiente' AND fecha_limite >= NOW()")
    elif estado == 'vencido':
        where.append("estado='pendiente' AND fecha_limite < NOW()")
    elif estado == 'respondido':
        where.append("estado='respondido'")
    where_sql = ' AND '.join(where)

    sql_count = f"SELECT COUNT(*) AS total FROM reclamaciones WHERE {where_sql}"
    sql_data = f"SELECT * FROM reclamaciones WHERE {where_sql} ORDER BY fecha DESC"
    items, total, page, total_pages = paginate_query(
        cur, sql_count, sql_data, tuple(params), page)

    for r in items:
        r['dias_restantes'] = _dias_para_limite(r['fecha_limite'])
        r['vencido'] = r['estado'] == 'pendiente' and (r['dias_restantes'] or 0) < 0

    cur.execute("SELECT COUNT(*) AS n FROM reclamaciones")
    total_general = cur.fetchone()['n'] or 0
    cur.execute("SELECT COUNT(*) AS n FROM reclamaciones WHERE estado='pendiente' AND fecha_limite >= NOW()")
    pendientes = cur.fetchone()['n'] or 0
    cur.execute("SELECT COUNT(*) AS n FROM reclamaciones WHERE estado='pendiente' AND fecha_limite < NOW()")
    vencidas = cur.fetchone()['n'] or 0
    cur.execute("SELECT COUNT(*) AS n FROM reclamaciones WHERE estado='respondido'")
    respondidas = cur.fetchone()['n'] or 0
    cur.close()

    return render_template('reclamaciones.html', reclamaciones=items,
                           estado=estado, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           total_general=total_general, pendientes=pendientes,
                           vencidas=vencidas, respondidas=respondidas)


@app.route('/reclamaciones/<int:id>')
def detalle_reclamacion(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM reclamaciones WHERE id=%s", (id,))
    r = cur.fetchone()
    cur.close()
    if not r:
        flash('Reclamación no encontrada.', 'danger')
        return redirect('/reclamaciones')
    return render_template('reclamacion_detalle.html', r=r,
                           dias=_dias_para_limite(r['fecha_limite']))


@app.route('/reclamaciones/<int:id>/responder', methods=['POST'])
def responder_reclamacion(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect(f'/reclamaciones/{id}')

    respuesta = (request.form.get('respuesta') or '').strip()
    responsable = ' '.join((request.form.get('respondido_por') or '').split())
    if len(respuesta) < 10:
        flash('La respuesta debe explicar las acciones adoptadas (mínimo 10 caracteres).', 'danger')
        return redirect(f'/reclamaciones/{id}')
    if not responsable:
        flash('Indica el nombre del responsable que responde.', 'danger')
        return redirect(f'/reclamaciones/{id}')

    cur = mysql.connection.cursor()
    cur.execute("SELECT id FROM reclamaciones WHERE id=%s", (id,))
    if not cur.fetchone():
        cur.close()
        flash('Reclamación no encontrada.', 'danger')
        return redirect('/reclamaciones')
    cur.execute("""
        UPDATE reclamaciones
        SET respuesta=%s, respondido_por=%s, fecha_respuesta=NOW(), estado='respondido'
        WHERE id=%s
    """, (respuesta, responsable, id))
    mysql.connection.commit()
    cur.close()
    flash('Respuesta registrada. El plazo de atención queda cerrado.', 'success')
    return redirect(f'/reclamaciones/{id}')


@app.route('/reclamaciones/<int:id>/imprimir')
def imprimir_reclamacion(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM reclamaciones WHERE id=%s", (id,))
    r = cur.fetchone()
    cur.close()
    if not r:
        flash('Reclamación no encontrada.', 'danger')
        return redirect('/reclamaciones')
    return render_template('reclamacion_imprimir.html', r=r,
                           dias=_dias_para_limite(r['fecha_limite']))

# ─────────────────────────────────────────────
# PRODUCTOS PARA PEDIR
# ─────────────────────────────────────────────
@app.route('/productos-para-pedir')
def productos_para_pedir():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"""
        SELECT pp.id, pp.cantidad_pedido, pp.fecha, pp.estado,
               p.nombre AS producto_nombre, p.stock AS stock_actual,
               p.categoria,
               pr.id AS proveedor_id, pr.nombre AS proveedor_nombre,
               pr.celular AS proveedor_celular
        FROM {ALMACEN_DB}.productos_para_pedir pp
        JOIN {ALMACEN_DB}.productos p ON pp.producto_id=p.id
        LEFT JOIN {ALMACEN_DB}.proveedores pr ON pp.proveedor_id=pr.id
        WHERE pp.estado='pendiente'
        ORDER BY pp.fecha DESC
    """)
    pedidos = cur.fetchall()

    # Agrupar por proveedor para generar mensajes WhatsApp
    proveedores_dict = {}
    for ped in pedidos:
        pid = ped['proveedor_id'] or 'sin_proveedor'
        if pid not in proveedores_dict:
            proveedores_dict[pid] = {
                'proveedor_nombre': ped['proveedor_nombre'] or 'Sin proveedor',
                'proveedor_celular': ped['proveedor_celular'] or '',
                'productos': []
            }
        proveedores_dict[pid]['productos'].append(ped)

    # Generar URLs de WhatsApp
    for pid, data in proveedores_dict.items():
        if data['proveedor_celular']:
            lista = "\n".join([
                f"- {p['producto_nombre']} x{p['cantidad_pedido']} (Stock actual: {p['stock_actual']})"
                for p in data['productos']
            ])
            msg = f"Hola {data['proveedor_nombre']}, necesitamos reponer los siguientes productos:\n\n{lista}\n\nGracias - Multiservicios Richard"
            data['whatsapp_url'] = whatsapp_url(data['proveedor_celular'], msg)
        else:
            data['whatsapp_url'] = None

    cur.close()
    return render_template('productos_para_pedir.html',
                           pedidos=pedidos,
                           proveedores_pedidos=proveedores_dict)

@app.route('/productos-para-pedir/eliminar/<int:id>')
def eliminar_pedido(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"UPDATE {ALMACEN_DB}.productos_para_pedir SET estado='cancelado' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Pedido eliminado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/actualizar/<int:id>', methods=['POST'])
def actualizar_pedido(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    try:
        cantidad = int(request.form.get('cantidad', 1))
    except (ValueError, TypeError):
        cantidad = 1
    if cantidad < 1:
        cantidad = 1
    cur = mysql.connection.cursor()
    cur.execute(f"UPDATE {ALMACEN_DB}.productos_para_pedir SET cantidad_pedido=%s WHERE id=%s", (cantidad, id))
    mysql.connection.commit()
    cur.close()
    flash('Cantidad actualizada.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/marcar-enviado/<int:id>')
def marcar_enviado(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"UPDATE {ALMACEN_DB}.productos_para_pedir SET estado='enviado' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Pedido marcado como enviado.', 'success')
    return redirect('/productos-para-pedir')

# ─────────────────────────────────────────────
# PRO14: INGRESOS DE PRODUCTOS (Comprobante de ingreso)
# ─────────────────────────────────────────────
@app.route('/ingresos')
def ingresos():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    
    # Filtros
    buscar_proveedor = request.args.get('buscar_proveedor', '').strip()
    buscar_producto = request.args.get('buscar_producto', '').strip()
    fecha_desde = request.args.get('fecha_desde', '').strip()
    fecha_hasta = request.args.get('fecha_hasta', '').strip()
    estado = (request.args.get('estado') or 'todos').strip().lower()
    if estado not in ('todos', 'vigentes', 'anulados'):
        estado = 'todos'
    page = int(request.args.get('page', 1))
    
    cur = mysql.connection.cursor()
    
    # WHERE dinámico
    where_parts = []
    params = []
    
    if buscar_proveedor:
        # Se busca sobre la foto guardada en el ingreso; el nombre actual del
        # proveedor solo es el respaldo de las filas antiguas.
        where_parts.append("COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) LIKE %s")
        params.append(f'%{buscar_proveedor}%')
    if buscar_producto:
        where_parts.append("EXISTS (SELECT 1 FROM {0}.detalle_ingreso di2 "
                           "LEFT JOIN {0}.productos pr ON di2.producto_id=pr.id "
                           "WHERE di2.ingreso_id=i.id AND "
                           "COALESCE(NULLIF(di2.producto_nombre,''), pr.nombre) LIKE %s)"
                           .format(ALMACEN_DB))
        params.append(f'%{buscar_producto}%')
    if fecha_desde:
        where_parts.append("DATE(i.fecha) >= %s")
        params.append(fecha_desde)
    if fecha_hasta:
        where_parts.append("DATE(i.fecha) <= %s")
        params.append(fecha_hasta)
    if estado == 'vigentes':
        where_parts.append("COALESCE(i.estado,'vigente') <> 'anulado'")
    elif estado == 'anulados':
        where_parts.append("i.estado = 'anulado'")
    
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    
    # Consulta con agregados: categoría, productos distintos, cantidad total.
    # El nombre del proveedor sale de la foto del ingreso (proveedor_nombre).
    sql_count = f"""SELECT COUNT(*) AS total FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        {where_sql}"""
    
    sql_data = f"""SELECT i.id, i.fecha, i.notas,
               COALESCE(i.estado,'vigente') AS estado,
               i.motivo_anulacion, i.anulado_en, i.anulado_por,
               COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
               COALESCE(NULLIF(i.proveedor_documento,''),
                        CONCAT_WS(' ', p.tipo_documento, p.numero_documento)) AS proveedor_documento,
               p.categoria AS proveedor_categoria,
               COUNT(DISTINCT di.producto_id) AS productos_distintos,
               COALESCE(SUM(di.cantidad), 0) AS total_cantidad
        FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        LEFT JOIN {ALMACEN_DB}.detalle_ingreso di ON di.ingreso_id=i.id
        {where_sql}
        GROUP BY i.id, i.fecha, i.notas, i.estado, i.motivo_anulacion, i.anulado_en,
                 i.anulado_por, i.proveedor_nombre, p.nombre, p.tipo_documento,
                 p.numero_documento, p.categoria
        ORDER BY i.fecha DESC"""
    
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)
    anios = _anios_ingresos(cur)
    periodos = _mapa_periodos_ingresos(cur)
    cur.close()
    
    return render_template('ingresos.html', 
                           ingresos=items,
                           buscar_proveedor=buscar_proveedor,
                           buscar_producto=buscar_producto,
                           fecha_desde=fecha_desde,
                           fecha_hasta=fecha_hasta,
                           estado=estado,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages,
                           meses=MESES_ES, anios=anios, periodos=periodos,
                           motivos=MOTIVOS_ANULACION_INGRESO,
                           mes_actual=_ahora_lima().month,
                           anio_actual=_ahora_lima().year,
                           dia_actual=_ahora_lima().day)

@app.route('/registrar-ingreso', methods=['GET','POST'])
def registrar_ingreso():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')

    def _cargar_catalogos():
        """Productos y proveedores ya normalizados para el selector buscador."""
        cur = mysql.connection.cursor()
        cur.execute(f"SELECT id, nombre, stock, precio FROM {ALMACEN_DB}.productos "
                    f"ORDER BY nombre")
        prod = [{'id': r['id'], 'nombre': r['nombre'],
                 'stock': int(r['stock'] or 0), 'precio': float(r['precio'] or 0)}
                for r in cur.fetchall()]
        # Un mismo proveedor puede estar en varias categorias (una fila por
        # categoria). En el selector del ingreso solo interesa la persona, por
        # eso se muestra una vez por nombre y se enlaza la primera fila.
        cur.execute(f"SELECT MIN(id) AS id, nombre FROM {ALMACEN_DB}.proveedores "
                    f"GROUP BY nombre ORDER BY nombre")
        prov = [{'id': r['id'], 'nombre': r['nombre']} for r in cur.fetchall()]
        cur.close()
        return prod, prov

    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/registrar-ingreso')

        proveedor_id = (request.form.get('proveedor_id') or '').strip()
        notas = request.form.get('notas', '').strip()
        producto_ids = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')
        precios_compra = request.form.getlist('precio_compra[]')
        precios_venta = request.form.getlist('precio_venta[]')

        errores = []
        filas = []
        vistos = set()

        if not proveedor_id:
            errores.append('Debes seleccionar un proveedor.')

        total_filas = max(len(producto_ids), len(cantidades),
                          len(precios_compra), len(precios_venta))
        for i in range(total_filas):
            pid_raw = (producto_ids[i] if i < len(producto_ids) else '').strip()
            cant_raw = (cantidades[i] if i < len(cantidades) else '').strip()
            pc_raw = (precios_compra[i] if i < len(precios_compra) else '').strip()
            pv_raw = (precios_venta[i] if i < len(precios_venta) else '').strip()

            # Ignora filas completamente vacias (por ejemplo, la fila base
            # cuando el usuario solo agrego filas nuevas).
            if not any([pid_raw, cant_raw, pc_raw, pv_raw]):
                continue

            n = i + 1
            fallos = []
            pid = cant = pc = pv = None

            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                fallos.append('producto inválido')
            try:
                cant = int(cant_raw)
                if cant <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                fallos.append('cantidad debe ser mayor a 0')
            try:
                pc = float(pc_raw)
                if pc < 0:
                    raise ValueError
            except (TypeError, ValueError):
                fallos.append('precio de compra inválido')
            try:
                pv = float(pv_raw)
                if pv <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                fallos.append('precio de venta debe ser mayor a 0')
            if pid is not None and pid in vistos:
                fallos.append('producto repetido en el ingreso')

            if fallos:
                errores.append(f'Fila {n}: ' + ', '.join(fallos) + '.')
                continue

            vistos.add(pid)
            filas.append({'producto_id': pid, 'cantidad': cant,
                          'precio_compra': pc, 'precio_venta': pv})

        # "Sin productos" solo si no hubo errores por fila (esos ya explican el
        # motivo). Asi, si faltan varias cosas, se reportan todas.
        if not filas and not any(e.startswith('Fila ') for e in errores):
            errores.append('Agrega al menos un producto al ingreso.')

        # El proveedor debe existir si se envio. Del mismo dato sale la foto de
        # nombre y documento que se guarda en el ingreso.
        prov_nombre = prov_documento = ''
        if not errores and proveedor_id:
            cur = mysql.connection.cursor()
            cur.execute(f"""SELECT nombre, tipo_documento, numero_documento
                            FROM {ALMACEN_DB}.proveedores WHERE id=%s""",
                        (proveedor_id,))
            prov = cur.fetchone()
            cur.close()
            if not prov:
                errores.append('El proveedor seleccionado no existe.')
            else:
                prov_nombre = prov['nombre'] or ''
                if prov['numero_documento']:
                    prov_documento = f"{prov['tipo_documento'] or ''} " \
                                     f"{prov['numero_documento']}".strip()

        if not errores:
            cur = mysql.connection.cursor()
            try:
                # Los nombres de proveedor y producto se guardan como foto del
                # momento del registro: si despues se renombra o se borra esa
                # fila, el ingreso se sigue leyendo igual que ese dia.
                ids = [f['producto_id'] for f in filas]
                nombres = {}
                if ids:
                    cur.execute(f"SELECT id, nombre FROM {ALMACEN_DB}.productos "
                                f"WHERE id IN ({','.join(['%s'] * len(ids))})", tuple(ids))
                    nombres = {r['id']: r['nombre'] for r in cur.fetchall()}

                # Se fija la fecha desde Python (hora de Peru, no la del servidor).
                cur.execute(
                    f"INSERT INTO {ALMACEN_DB}.ingresos "
                    f"(proveedor_id, notas, fecha, estado, proveedor_nombre, proveedor_documento) "
                    f"VALUES (%s,%s,%s,'vigente',%s,%s)",
                    (proveedor_id, notas, _ahora_lima(), prov_nombre, prov_documento))
                ingreso_id = cur.lastrowid

                for f in filas:
                    cur.execute(
                        f"INSERT INTO {ALMACEN_DB}.detalle_ingreso "
                        f"(ingreso_id, producto_id, producto_nombre, cantidad, "
                        f" precio_compra, precio_venta) "
                        f"VALUES (%s,%s,%s,%s,%s,%s)",
                        (ingreso_id, f['producto_id'], nombres.get(f['producto_id'], ''),
                         f['cantidad'], f['precio_compra'], f['precio_venta']))
                    # El ingreso repone stock y actualiza el precio de venta.
                    # Si el producto estaba inactivo por quedarse sin stock,
                    # vuelve a estar disponible en la tienda.
                    cur.execute(
                        f"UPDATE {ALMACEN_DB}.productos "
                        f"SET stock=stock+%s, precio=%s, estado='activo' WHERE id=%s",
                        (f['cantidad'], f['precio_venta'], f['producto_id']))

                mysql.connection.commit()
                cur.close()
                flash(f'Ingreso #{ingreso_id} registrado correctamente.', 'success')
                return redirect(f'/comprobante-ingreso/{ingreso_id}')
            except Exception as exc:
                mysql.connection.rollback()
                cur.close()
                app.logger.error('registrar_ingreso fallo: %s', exc)
                errores.append('No se pudo registrar el ingreso. Intenta de nuevo.')

        for e in errores:
            flash(e, 'danger')

        # Se devuelve el formulario con lo que el usuario ya habia escrito.
        productos, proveedores = _cargar_catalogos()
        valores = {
            'proveedor_id': proveedor_id,
            'notas': notas,
            'filas': [{
                'producto_id': (producto_ids[i] if i < len(producto_ids) else ''),
                'cantidad':    (cantidades[i] if i < len(cantidades) else ''),
                'precio_compra': (precios_compra[i] if i < len(precios_compra) else ''),
                'precio_venta':  (precios_venta[i] if i < len(precios_venta) else ''),
            } for i in range(total_filas)],
        }
        return render_template('registrar_ingreso.html', productos=productos,
                               proveedores=proveedores, valores=valores)

    productos, proveedores = _cargar_catalogos()
    return render_template('registrar_ingreso.html', productos=productos,
                           proveedores=proveedores)

@app.route('/comprobante-ingreso/<int:id>')
def comprobante_ingreso(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    # El nombre del proveedor y de cada producto salen primero de la foto
    # guardada en el ingreso; el join solo es el respaldo de las filas antiguas.
    cur.execute("""
        SELECT i.id, i.fecha, i.notas, COALESCE(i.estado,'vigente') AS estado,
               i.motivo_anulacion, i.anulado_en, i.anulado_por,
               COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
               COALESCE(NULLIF(i.proveedor_documento,''),
                        CONCAT_WS(' ', p.tipo_documento, p.numero_documento)) AS proveedor_documento,
               p.celular AS proveedor_celular,
               p.categoria AS proveedor_categoria
        FROM {0}.ingresos i
        LEFT JOIN {0}.proveedores p ON i.proveedor_id=p.id
        WHERE i.id=%s
    """.format(ALMACEN_DB), (id,))
    ingreso = cur.fetchone()
    if not ingreso:
        flash('Ingreso no encontrado.', 'danger')
        return redirect('/ingresos')
    cur.execute("""
        SELECT di.cantidad, di.precio_compra, di.precio_venta,
               COALESCE(NULLIF(di.producto_nombre,''), pr.nombre) AS producto_nombre,
               pr.stock AS stock_actual
        FROM {0}.detalle_ingreso di
        LEFT JOIN {0}.productos pr ON di.producto_id=pr.id
        WHERE di.ingreso_id=%s
        ORDER BY producto_nombre
    """.format(ALMACEN_DB), (id,))
    items = cur.fetchall()
    total_compra = sum(i['cantidad'] * float(i['precio_compra'] or 0) for i in items)
    total_venta = sum(i['cantidad'] * float(i['precio_venta'] or 0) for i in items)
    cur.close()
    return render_template('comprobante_ingreso.html', ingreso=ingreso, items=items,
                           total_general=total_compra, total_compra=total_compra,
                           total_venta=total_venta)

# El ingreso es un libro: no se borra, se anula. Queda el registro completo con
# el motivo, quien lo anulo y cuando (mismo criterio que las ventas canceladas).
MOTIVOS_ANULACION_INGRESO = {
    'duplicado':  'Ingreso duplicado',
    'datos':      'Datos incorrectos (producto, cantidad o precio)',
    'devolucion': 'El proveedor devolvió la mercadería',
    'carga':      'Error de carga',
    'otro':       'Otro motivo',
}


@app.route('/ingresos/anular/<int:id>', methods=['POST'])
def anular_ingreso(id):
    """Anula un ingreso (terminal) y revierte el stock que sumo."""
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect('/ingresos')

    motivo  = (request.form.get('motivo') or '').strip()
    detalle = ' '.join((request.form.get('motivo_detalle') or '').split())
    if motivo not in MOTIVOS_ANULACION_INGRESO:
        flash('Selecciona un motivo de anulación.', 'danger')
        return redirect('/ingresos')
    if motivo == 'otro' and len(detalle) < 5:
        flash('Cuando eliges "Otro motivo" hay que describirlo.', 'danger')
        return redirect('/ingresos')
    texto = MOTIVOS_ANULACION_INGRESO[motivo]
    if detalle:
        texto = f'{texto}: {detalle}' if motivo == 'otro' else f'{texto} ({detalle})'
    texto = texto[:255]

    cur = mysql.connection.cursor()
    cur.execute(f"""SELECT COALESCE(estado,'vigente') AS estado
                    FROM {ALMACEN_DB}.ingresos WHERE id=%s""", (id,))
    ingreso = cur.fetchone()
    if not ingreso:
        cur.close()
        flash('Ingreso no encontrado.', 'danger')
        return redirect('/ingresos')
    if ingreso['estado'] == 'anulado':
        cur.close()
        flash(f'El ingreso #{id} ya está anulado.', 'warning')
        return redirect('/ingresos')

    # Se descuenta el stock que sumo este ingreso (lo mismo que hacia Eliminar).
    # Si parte ya se vendio, el stock baja hasta 0 y se avisa; el registro no se
    # toca: lo que se anula es el efecto, no la historia.
    cur.execute(f"SELECT producto_id, cantidad FROM {ALMACEN_DB}.detalle_ingreso "
                f"WHERE ingreso_id=%s", (id,))
    items = cur.fetchall()
    revertidos = faltaron = 0
    for item in items:
        cant = int(item['cantidad'] or 0)
        cur.execute(f"SELECT stock FROM {ALMACEN_DB}.productos WHERE id=%s",
                    (item['producto_id'],))
        prod = cur.fetchone()
        actual = int(prod['stock'] or 0) if prod else 0
        if actual >= cant:
            cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock-%s WHERE id=%s",
                        (cant, item['producto_id']))
        else:
            cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=0 WHERE id=%s",
                        (item['producto_id'],))
            faltaron += 1
        revertidos += 1

    # El filtro por estado en el UPDATE corta la doble presion y las dos
    # pestanas abiertas: si otro pedido ya anulo el ingreso, esto se revierte.
    cur.execute(f"""
        UPDATE {ALMACEN_DB}.ingresos
        SET estado='anulado', motivo_anulacion=%s, anulado_en=%s, anulado_por=%s
        WHERE id=%s AND COALESCE(estado,'vigente')='vigente'
    """, (texto, _ahora_lima(), session.get('correo') or 'administrador', id))
    if cur.rowcount != 1:
        mysql.connection.rollback()
        cur.close()
        flash(f'El ingreso #{id} ya estaba anulado.', 'warning')
        return redirect('/ingresos')
    mysql.connection.commit()
    cur.close()

    msg = f'Ingreso #{id} anulado. Stock revertido ({revertidos} productos).'
    if faltaron:
        msg += f' En {faltaron} producto(s) el stock ya era menor y quedó en 0.'
    flash(msg, 'warning')
    return redirect('/ingresos')

# ─────────────────────────────────────────────
# PRO14B: EXPORTAR INGRESOS A PDF
# ─────────────────────────────────────────────
# Un PDF por ingreso (detalle de sus productos) y reportes por periodo:
# semanal (lunes a domingo) o mensual (dia 1 al ultimo del mes). Los reportes
# llevan un resumen y despues el detalle de cada ingreso.
MESES_ES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
            'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')
_NARANJA = colors.HexColor('#f39c12')


def _pdf_escapar(valor):
    """Escapa el texto para que ReportLab no lo lea como marcado."""
    return (str(valor if valor is not None else '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _pdf_estilos():
    """Estilos propios de los reportes de ingresos."""
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Celda', parent=styles['Normal'],
                              fontSize=8, leading=10))
    styles.add(ParagraphStyle(name='Cabecera', parent=styles['Normal'],
                              fontSize=8, leading=10, textColor=colors.black))
    styles.add(ParagraphStyle(name='Dato', parent=styles['Normal'],
                              fontSize=9, leading=12))
    styles.add(ParagraphStyle(name='TituloSeccion', parent=styles['Heading3'],
                              fontSize=10, spaceBefore=8, spaceAfter=3))
    styles.add(ParagraphStyle(name='Anulado', parent=styles['Normal'], fontSize=8,
                              leading=10, textColor=colors.red))
    styles.add(ParagraphStyle(name='TituloAnulado', parent=styles['Heading3'],
                              fontSize=10, spaceBefore=8, spaceAfter=3,
                              textColor=colors.red))
    return styles


def _pdf_estilo_tabla():
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), _NARANJA),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2),
         [colors.white, colors.HexColor('#fdf4e6')]),
    ])


def _pdf_fecha(valor, con_hora=True):
    if not valor:
        return '-'
    return valor.strftime('%d/%m/%Y %H:%M' if con_hora else '%d/%m/%Y')


def _pdf_encabezado(styles, titulo, subtitulo, datos=None):
    """Portada corta: empresa, titulo del reporte y datos del periodo."""
    elems = [Paragraph(f"<b>{_pdf_escapar(EMPRESA_INFO['nombre'])}</b>", styles['Title']),
             Paragraph(_pdf_escapar(titulo), styles['Heading3']),
             Paragraph(_pdf_escapar(subtitulo), styles['Dato'])]
    if datos:
        filas = [[Paragraph(f'<b>{_pdf_escapar(k)}</b>', styles['Dato']),
                  Paragraph(_pdf_escapar(v), styles['Dato'])] for k, v in datos]
        t = Table(filas, colWidths=[110, 390], hAlign='LEFT')
        t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                               ('TOPPADDING', (0, 0), (-1, -1), 1),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
                               ('LEFTPADDING', (0, 0), (0, -1), 0)]))
        elems.append(Spacer(1, 0.1 * inch))
        elems.append(t)
    elems.append(Spacer(1, 0.22 * inch))
    return elems


def _pdf_tabla_productos(styles, items):
    """Productos de un ingreso. Devuelve (tabla, total_compra, total_venta)."""
    data = [[Paragraph(f'<b>{c}</b>', styles['Cabecera']) for c in
             ['Producto', 'Cantidad', 'P. compra', 'P. venta', 'Subtotal compra']]]
    for it in items:
        cant = int(it['cantidad'] or 0)
        pc = float(it['precio_compra'] or 0)
        pv = float(it['precio_venta'] or 0)
        data.append([Paragraph(_pdf_escapar(it['producto_nombre']), styles['Celda']),
                     str(cant), f'S/ {pc:.2f}', f'S/ {pv:.2f}',
                     f'S/ {cant * pc:.2f}'])
    total_compra = sum(int(it['cantidad'] or 0) * float(it['precio_compra'] or 0)
                       for it in items)
    total_venta = sum(int(it['cantidad'] or 0) * float(it['precio_venta'] or 0)
                      for it in items)
    if items:
        data.append([Paragraph('<b>Total compra</b>', styles['Celda']), '', '', '',
                     Paragraph(f'<b>S/ {total_compra:.2f}</b>', styles['Celda'])])
    else:
        data.append([Paragraph('Sin productos.', styles['Celda']), '', '', '', ''])
    t = Table(data, colWidths=[235, 60, 70, 70, 80], repeatRows=1)
    t.setStyle(_pdf_estilo_tabla())
    return t, total_compra, total_venta


def _pdf_tabla_resumen(styles, ingresos):
    """Una fila por ingreso con sus totales. Los anulados no suman."""
    vigentes = [r for r in ingresos
                if (r.get('estado') or 'vigente') != 'anulado']
    data = [[Paragraph(f'<b>{c}</b>', styles['Cabecera']) for c in
             ['#', 'Fecha', 'Proveedor', 'Categoría', 'Estado', 'Cant.', 'Total compra']]]
    for r in ingresos:
        anulado = (r.get('estado') or 'vigente') == 'anulado'
        data.append([str(r['id']), _pdf_fecha(r['fecha']),
                     Paragraph(_pdf_escapar(r['proveedor_nombre'] or 'Sin proveedor'),
                               styles['Celda']),
                     _pdf_escapar(r['proveedor_categoria'] or '-'),
                     Paragraph('<b>ANULADO</b>', styles['Anulado']) if anulado else '',
                     str(int(r['total_cantidad'] or 0)),
                     f"S/ {float(r['total_compra'] or 0):.2f}"])
    if ingresos:
        cant = sum(int(r['total_cantidad'] or 0) for r in vigentes)
        compra = sum(float(r['total_compra'] or 0) for r in vigentes)
        data.append([Paragraph('<b>Totales</b>', styles['Celda']), '', '',
                     Paragraph(f'<b>{len(vigentes)} ingreso(s)</b>', styles['Celda']),
                     '',
                     Paragraph(f'<b>{cant}</b>', styles['Celda']),
                     Paragraph(f'<b>S/ {compra:.2f}</b>', styles['Celda'])])
    t = Table(data, colWidths=[26, 82, 150, 64, 52, 38, 78], repeatRows=1)
    t.setStyle(_pdf_estilo_tabla())
    return t


def _datos_ingreso_pdf(cur, ingreso_id):
    """Un ingreso con su detalle de productos (mismo dato que el comprobante)."""
    cur.execute(f"""
        SELECT i.id, i.fecha, i.notas, COALESCE(i.estado,'vigente') AS estado,
               i.motivo_anulacion, i.anulado_en, i.anulado_por,
               COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
               COALESCE(NULLIF(i.proveedor_documento,''),
                        CONCAT_WS(' ', p.tipo_documento, p.numero_documento)) AS proveedor_documento,
               p.celular AS proveedor_celular,
               p.categoria AS proveedor_categoria
        FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        WHERE i.id=%s
    """, (ingreso_id,))
    ingreso = cur.fetchone()
    if not ingreso:
        return None, []
    # LEFT JOIN: si el producto se borro despues, la linea se sigue imprimiendo
    # con el nombre que quedo guardado en el ingreso.
    cur.execute(f"""
        SELECT di.cantidad, di.precio_compra, di.precio_venta,
               COALESCE(NULLIF(di.producto_nombre,''), pr.nombre) AS producto_nombre
        FROM {ALMACEN_DB}.detalle_ingreso di
        LEFT JOIN {ALMACEN_DB}.productos pr ON di.producto_id=pr.id
        WHERE di.ingreso_id=%s
        ORDER BY producto_nombre
    """, (ingreso_id,))
    return ingreso, cur.fetchall()


def _datos_ingresos_pdf(cur, desde, hasta):
    """Ingresos de un rango de fechas con totales y detalle por ingreso."""
    cur.execute(f"""
        SELECT i.id, i.fecha, i.notas, COALESCE(i.estado,'vigente') AS estado,
               i.motivo_anulacion,
               COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
               p.categoria AS proveedor_categoria,
               COUNT(DISTINCT di.producto_id) AS productos_distintos,
               COALESCE(SUM(di.cantidad), 0) AS total_cantidad,
               COALESCE(SUM(di.cantidad * di.precio_compra), 0) AS total_compra
        FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        LEFT JOIN {ALMACEN_DB}.detalle_ingreso di ON di.ingreso_id=i.id
        WHERE DATE(i.fecha) BETWEEN %s AND %s
        GROUP BY i.id, i.fecha, i.notas, i.estado, i.motivo_anulacion,
                 i.proveedor_nombre, p.nombre, p.categoria
        ORDER BY i.fecha ASC, i.id ASC
    """, (desde, hasta))
    ingresos = cur.fetchall()
    detalles = {}
    if ingresos:
        ids = [r['id'] for r in ingresos]
        cur.execute(f"""
            SELECT di.ingreso_id, di.cantidad, di.precio_compra, di.precio_venta,
                   COALESCE(NULLIF(di.producto_nombre,''), pr.nombre) AS producto_nombre
            FROM {ALMACEN_DB}.detalle_ingreso di
            LEFT JOIN {ALMACEN_DB}.productos pr ON di.producto_id=pr.id
            WHERE di.ingreso_id IN ({','.join(['%s'] * len(ids))})
            ORDER BY di.ingreso_id, producto_nombre
        """, tuple(ids))
        for d in cur.fetchall():
            detalles.setdefault(d['ingreso_id'], []).append(d)
    return ingresos, detalles


def _ultimo_dia_mes(anio, mes):
    """Ultimo dia de un mes (sin depender del locale de strftime)."""
    if mes == 12:
        return date(anio + 1, 1, 1) - timedelta(days=1)
    return date(anio, mes + 1, 1) - timedelta(days=1)


def _bloques_semana_mes(anio, mes):
    """Bloques lunes-domingo de un mes, recortados al propio mes.

    Se recortan para que un ingreso no aparezca en dos reportes: la semana 1
    de noviembre, por ejemplo, no repite los dias que caen en octubre.
    """
    ultimo = _ultimo_dia_mes(anio, mes)
    bloques = []
    desde = date(anio, mes, 1)
    while desde <= ultimo:
        # weekday(): lunes 0 ... domingo 6 -> dias que faltan para el domingo.
        hasta = min(desde + timedelta(days=6 - desde.weekday()), ultimo)
        bloques.append((desde, hasta))
        desde = hasta + timedelta(days=1)
    return bloques


def _periodo_ingresos(periodo, referencia=None, numero=None):
    """(desde, hasta, etiqueta) del mes o de una semana dentro del mes.

    periodo: 'mes' o 'semana'. referencia: date que fija el mes (hoy en hora
    de Peru si no se pasa). numero: semana 1..n; si no se usa, la semana que
    contiene a referencia. Los numeros fuera de rango se ajustan al mes.
    """
    ref = referencia or _ahora_lima().date()
    if periodo == 'semana':
        bloques = _bloques_semana_mes(ref.year, ref.month)
        if numero is None:
            indice = next((i for i, (d, h) in enumerate(bloques) if d <= ref <= h), 0)
        else:
            indice = min(max(numero, 1), len(bloques)) - 1
        desde, hasta = bloques[indice]
        etiqueta = (f"Semana {indice + 1} de {MESES_ES[ref.month - 1]} de {ref.year} "
                    f"(del {desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')})")
    else:
        desde = ref.replace(day=1)
        hasta = _ultimo_dia_mes(desde.year, desde.month)
        etiqueta = f"Mes de {MESES_ES[ref.month - 1]} de {ref.year}"
    return desde, hasta, etiqueta


def _anios_ingresos(cur):
    """Anios que ofrece el selector: del ultimo al del primer ingreso."""
    actual = _ahora_lima().year
    cur.execute(f"SELECT MIN(YEAR(fecha)) AS minimo FROM {ALMACEN_DB}.ingresos")
    fila = cur.fetchone()
    minimo = int((fila or {}).get('minimo') or actual)
    minimo = min(minimo, actual)
    return list(range(actual, minimo - 1, -1))


def _mapa_periodos_ingresos(cur):
    """Ingresos existentes por anio, mes y semana, para el selector de PDF.

    Devuelve {anio: {'meses': {mes: cantidad}, 'semanas': {mes: [c1, c2, ...]}}}.
    Las semanas se numeran igual que en el PDF (lunes a domingo recortadas al
    mes), asi lo que el dueno ve como "sin registros" es exactamente lo que el
    servidor rechazaria.
    """
    cur.execute(f"SELECT DATE(fecha) AS dia FROM {ALMACEN_DB}.ingresos")
    mapa = {}
    for fila in cur.fetchall():
        dia = fila['dia']
        if not dia:
            continue
        dia = dia.date() if isinstance(dia, datetime) else dia
        anio, mes = dia.year, dia.month
        datos = mapa.setdefault(str(anio), {'meses': {}, 'semanas': {}})
        datos['meses'][str(mes)] = datos['meses'].get(str(mes), 0) + 1
        bloques = _bloques_semana_mes(anio, mes)
        for indice, (desde, hasta) in enumerate(bloques):
            if desde <= dia <= hasta:
                cuenta = datos['semanas'].setdefault(str(mes), [0] * len(bloques))
                cuenta[indice] += 1
                break
    return mapa


def _pdf_respuesta(buf, nombre):
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=nombre)


@app.route('/ingresos/pdf/<int:id>')
def ingreso_pdf(id):
    """PDF de un solo ingreso, con el detalle de sus productos."""
    if not _es_admin():
        return redirect('/login')
    cur = mysql.connection.cursor()
    ingreso, items = _datos_ingreso_pdf(cur, id)
    cur.close()
    if not ingreso:
        flash('Ingreso no encontrado.', 'danger')
        return redirect('/ingresos')

    styles = _pdf_estilos()
    anulado = (ingreso['estado'] or 'vigente') == 'anulado'
    datos = [('Fecha', _pdf_fecha(ingreso['fecha'])),
             ('Estado', 'ANULADO' if anulado else 'Vigente'),
             ('Proveedor', ingreso['proveedor_nombre'] or 'Sin proveedor')]
    if ingreso['proveedor_documento']:
        datos.append(('Documento', ingreso['proveedor_documento']))
    if ingreso['proveedor_celular']:
        datos.append(('Celular', ingreso['proveedor_celular']))
    if ingreso['proveedor_categoria']:
        datos.append(('Categoría', ingreso['proveedor_categoria']))
    if anulado and ingreso['motivo_anulacion']:
        datos.append(('Motivo de anulación', ingreso['motivo_anulacion']))
    if anulado and ingreso['anulado_en']:
        datos.append(('Anulado', f"{_pdf_fecha(ingreso['anulado_en'])} · "
                                 f"{ingreso['anulado_por'] or '-'}"))
    if ingreso['notas']:
        datos.append(('Notas', ingreso['notas']))

    elems = _pdf_encabezado(styles, 'Comprobante de ingreso',
                            f"Ingreso #{ingreso['id']}", datos)
    if anulado:
        elems.append(Paragraph('<b>INGRESO ANULADO — no cuenta en los totales '
                               'del almacen.</b>', styles['Anulado']))
        elems.append(Spacer(1, 0.12 * inch))
    tabla, total_compra, total_venta = _pdf_tabla_productos(styles, items)
    elems.append(Paragraph('<b>Productos ingresados</b>', styles['TituloSeccion']))
    elems.append(tabla)
    elems.append(Spacer(1, 0.18 * inch))

    cant_total = sum(int(i['cantidad'] or 0) for i in items)
    resumen = Table([
        ['Productos distintos', str(len(items))],
        ['Cantidad total', str(cant_total)],
        ['Total compra', f'S/ {total_compra:.2f}'],
        ['Total venta (estimado)', f'S/ {total_venta:.2f}'],
    ], colWidths=[180, 140], hAlign='LEFT')
    resumen.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elems.append(resumen)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=36, bottomMargin=36,
                            title=f'Ingreso {id}',
                            author='Multiservicios Richard')
    doc.build(elems)
    fecha_pdf = _ahora_lima().strftime('%Y%m%d')
    return _pdf_respuesta(buf, f'ingreso_{id}_{fecha_pdf}.pdf')


@app.route('/ingresos/pdf')
def ingresos_pdf():
    """PDF de los ingresos de una semana o de un mes (resumen + detalle).

    ?periodo=mes|semana&anio=YYYY&mes=1..12&semana=1..5. Sin parametros se
    exporta el periodo en curso; la semana solo aplica a periodo=semana.
    """
    if not _es_admin():
        return redirect('/login')
    periodo = (request.args.get('periodo') or 'mes').strip().lower()
    if periodo != 'semana':
        periodo = 'mes'

    hoy = _ahora_lima().date()
    anio_raw   = (request.args.get('anio') or '').strip()
    mes_raw    = (request.args.get('mes') or '').strip()
    semana_raw = (request.args.get('semana') or '').strip()
    invalido = 'El periodo seleccionado no es válido.'
    referencia, numero = None, None
    if anio_raw or mes_raw:
        try:
            anio = int(anio_raw or hoy.year)
            mes  = int(mes_raw or hoy.month)
            if not (1 <= mes <= 12 and 1900 <= anio <= 9999):
                raise ValueError
            referencia = date(anio, mes, 1)
        except ValueError:
            flash(invalido, 'danger')
            return redirect('/ingresos')
    if semana_raw:
        try:
            numero = int(semana_raw)
        except ValueError:
            flash(invalido, 'danger')
            return redirect('/ingresos')
    # Una semana que no existe en ese mes no se "ajusta" a otra: mejor no
    # descargar nada que entregar un periodo que el dueno no pidio.
    if periodo == 'semana' and referencia is not None and numero is not None:
        if not 1 <= numero <= len(_bloques_semana_mes(referencia.year, referencia.month)):
            flash(invalido, 'danger')
            return redirect('/ingresos')
    desde, hasta, etiqueta = _periodo_ingresos(periodo, referencia, numero)

    cur = mysql.connection.cursor()
    ingresos, detalles = _datos_ingresos_pdf(cur, desde, hasta)
    cur.close()

    # Un periodo sin ingresos no se descarga: se avisa y se vuelve al listado.
    if not ingresos:
        flash('No hay ingresos registrados en el periodo seleccionado.', 'warning')
        return redirect('/ingresos')

    styles = _pdf_estilos()
    vigentes = [r for r in ingresos if (r['estado'] or 'vigente') != 'anulado']
    n_anulados = len(ingresos) - len(vigentes)
    titulo = ('Reporte de ingresos semanal' if periodo == 'semana'
              else 'Reporte de ingresos mensual')
    resumen_cab = f"{len(vigentes)} vigente(s)"
    if n_anulados:
        resumen_cab += f" y {n_anulados} anulado(s)"
    elems = _pdf_encabezado(styles, titulo, etiqueta, [
        ('Periodo', f"{desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')}"),
        ('Ingresos', resumen_cab),
        ('Generado', _ahora_lima().strftime('%d/%m/%Y %H:%M')),
    ])

    elems.append(Paragraph('<b>Resumen</b>', styles['TituloSeccion']))
    elems.append(_pdf_tabla_resumen(styles, ingresos))
    if n_anulados:
        elems.append(Spacer(1, 0.06 * inch))
        elems.append(Paragraph(f'Los {n_anulados} ingreso(s) anulado(s) no incluyen '
                               'en los totales.', styles['Anulado']))
    elems.append(Spacer(1, 0.2 * inch))
    elems.append(Paragraph('<b>Detalle por ingreso</b>', styles['TituloSeccion']))
    for r in ingresos:
        items = detalles.get(r['id'], [])
        anulado = (r['estado'] or 'vigente') == 'anulado'
        encabezado = (('ANULADO · ' if anulado else '') +
                      f"Ingreso #{r['id']} · {_pdf_fecha(r['fecha'])} · "
                      f"{r['proveedor_nombre'] or 'Sin proveedor'}")
        bloque = [Paragraph('<b>' + _pdf_escapar(encabezado) + '</b>',
                            styles['TituloAnulado'] if anulado else styles['TituloSeccion'])]
        if anulado and r['motivo_anulacion']:
            bloque.append(Paragraph('Motivo de anulación: '
                                    + _pdf_escapar(r['motivo_anulacion']), styles['Dato']))
        if r['notas']:
            bloque.append(Paragraph('Notas: ' + _pdf_escapar(r['notas']),
                                    styles['Dato']))
        bloque.append(_pdf_tabla_productos(styles, items)[0])
        elems.append(KeepTogether(bloque))
        elems.append(Spacer(1, 0.1 * inch))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=36, bottomMargin=36, title=titulo,
                            author='Multiservicios Richard')
    doc.build(elems)
    nombre = (f"ingresos_{periodo}_{desde.strftime('%Y%m%d')}_"
              f"{hasta.strftime('%Y%m%d')}.pdf")
    return _pdf_respuesta(buf, nombre)


# ─────────────────────────────────────────────
# PRO15-19: SEGUIMIENTO DE ENTREGAS
# ─────────────────────────────────────────────
@app.route('/entregas')
def entregas():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    filtro = request.args.get('filtro', 'todas')
    motivo_filtro = request.args.get('motivo', '').strip()
    cur = mysql.connection.cursor()
    sql = """
        SELECT e.id, e.venta_id, e.estado, e.direccion_envio, e.fecha_estimada,
               e.fecha_entrega, e.notas, e.created_at,
               v.total, v.nombre AS cliente_nombre, v.documento AS cliente_doc,
               u.correo AS cliente_correo,
               c.motivo_codigo, c.motivo_detalle, c.cancelado_por
        FROM seguimiento_entregas e
        JOIN ventas v ON e.venta_id=v.id
        LEFT JOIN usuarios u ON v.cliente_id=u.id
        LEFT JOIN cancelaciones c ON c.venta_id=v.id
    """
    conds = ["COALESCE(v.oculta,0)=0"]
    params = []
    if filtro == 'pendientes':
        conds.append("e.estado='pendiente'")
    elif filtro == 'en_camino':
        conds.append("e.estado='en camino'")
    elif filtro == 'entregado':
        conds.append("e.estado='entregado'")
    elif filtro == 'cancelado':
        conds.append("e.estado='cancelado'")
    if motivo_cancelacion_valido(motivo_filtro):
        conds.append("c.motivo_codigo=%s")
        params.append(motivo_filtro)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY e.created_at DESC"
    cur.execute(sql, tuple(params))
    lista = cur.fetchall()
    cur.close()
    return render_template('entregas.html', entregas=lista, filtro=filtro, motivo_filtro=motivo_filtro)

@app.route('/entregas/crear/<int:venta_id>', methods=['POST'])
def crear_entrega(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id FROM seguimiento_entregas WHERE venta_id=%s", (venta_id,))
    if cur.fetchone():
        cur.close()
        flash('Ya existe seguimiento para esta venta.', 'warning')
        return redirect('/entregas')
    direccion = request.form.get('direccion_envio', '')
    fecha_est = request.form.get('fecha_estimada') or None
    notas = request.form.get('notas', '')
    cur.execute("INSERT INTO seguimiento_entregas (venta_id, estado, direccion_envio, fecha_estimada, notas) VALUES (%s,'pendiente',%s,%s,%s)",
                (venta_id, direccion, fecha_est, notas))
    mysql.connection.commit()
    _invalidar_cache_kpi()
    cur.close()
    flash(f'Entrega para venta #{venta_id} creada.', 'success')
    return redirect('/entregas')

@app.route('/entregas/actualizar/<int:id>', methods=['POST'])
def actualizar_entrega(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    nuevo_estado = request.form.get('estado', 'pendiente')
    if nuevo_estado not in ('pendiente', 'en camino', 'entregado', 'cancelado'):
        nuevo_estado = 'pendiente'
    notas = request.form.get('notas', '')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, venta_id, estado FROM seguimiento_entregas WHERE id=%s", (id,))
    fila = cur.fetchone()
    if not fila:
        cur.close()
        flash('Entrega no encontrada.', 'danger')
        return redirect('/entregas')
    anterior = fila['estado']
    venta_id = fila['venta_id']
    cur.execute("SELECT id, estado, total FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta:
        cur.close()
        flash('Venta no encontrada.', 'danger')
        return redirect('/entregas')
    venta_anterior = venta['estado']
    # Cancelado es terminal en ambos lados
    cancelada = (anterior == 'cancelado' or venta_anterior == 'cancelado')
    if cancelada:
        cur.close()
        flash(f'Venta #{venta_id} ya está cancelada. Motivo no editable.', 'warning')
        return redirect('/entregas')
    if nuevo_estado == 'entregado':
        cur.execute("UPDATE seguimiento_entregas SET estado=%s, notas=%s, fecha_entrega=NOW() WHERE id=%s",
                    (nuevo_estado, notas, id))
    else:
        cur.execute("UPDATE seguimiento_entregas SET estado=%s, notas=%s WHERE id=%s",
                    (nuevo_estado, notas, id))
    # Replicar al historial de compras (ventas.estado) para que se comuniquen
    venta_estado = {'pendiente': 'en espera', 'en camino': 'en espera',
                    'entregado': 'entregado', 'cancelado': 'cancelado'}.get(nuevo_estado, 'en espera')
    cur.execute("UPDATE ventas SET estado=%s WHERE id=%s", (venta_estado, venta_id))
    # Al cancelar como admin: devolver stock (una sola vez), reducir monto y registrar motivo
    if nuevo_estado == 'cancelado' and venta_anterior != 'cancelado':
        cur.execute(f"SELECT COALESCE(SUM(d.cantidad * d.precio), 0) AS monto_articulos "
                    f"FROM detalle_venta d WHERE d.venta_id=%s", (venta_id,))
        fila2 = cur.fetchone()
        monto_articulos = float(fila2['monto_articulos'] or 0) if fila2 else 0.0
        nuevo_total = max(0.0, float(venta['total'] or 0) - monto_articulos)
        cur.execute("UPDATE ventas SET total=%s WHERE id=%s", (nuevo_total, venta_id))
        restaurar_stock_venta(cur, venta_id)
        registrar_cancelacion(cur, venta_id, 'otro', 'Cancelado por administrador', 'admin')
    mysql.connection.commit()
    _invalidar_cache_kpi()
    cur.close()
    flash('Estado de entrega actualizado.', 'success')
    return redirect('/entregas')

@app.route('/entregas/eliminar/<int:id>')
def eliminar_entrega(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM seguimiento_entregas WHERE id=%s", (id,))
    mysql.connection.commit()
    _invalidar_cache_kpi()
    cur.close()
    flash('Seguimiento de entrega eliminado.', 'success')
    return redirect('/entregas')

@app.route('/entregas/detalle/<int:id>')
def detalle_entrega(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT e.*, v.total, v.nombre AS cliente_nombre, v.documento AS cliente_doc, v.fecha AS venta_fecha,
               u.correo AS cliente_correo
        FROM seguimiento_entregas e
        JOIN ventas v ON e.venta_id=v.id
        LEFT JOIN usuarios u ON v.cliente_id=u.id
        WHERE e.id=%s
    """, (id,))
    entrega = cur.fetchone()
    if not entrega:
        flash('Entrega no encontrada.', 'danger')
        return redirect('/entregas')
    cur.execute("""
        SELECT p.nombre, p.imagen, d.cantidad, d.precio
        FROM detalle_venta d
        JOIN {0}.productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """.format(ALMACEN_DB), (entrega['venta_id'],))
    productos = cur.fetchall()
    cur.close()
    return render_template('detalle_entrega.html', entrega=entrega, productos=productos)

# ─────────────────────────────────────────────
# PRO23: REGISTRO DE SALIDA DE PRODUCTOS
# ─────────────────────────────────────────────
@app.route('/salidas')
def salidas():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    sql_count = """SELECT COUNT(*) AS total FROM {0}.salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE COALESCE(v.nombre, '') LIKE %s""".format(ALMACEN_DB)
    sql_data = """SELECT s.id, s.fecha, s.notas,
               v.id AS venta_id, v.nombre AS cliente_nombre, v.total AS venta_total,
               (SELECT SUM(ds.cantidad) FROM {0}.detalle_salida ds WHERE ds.salida_id=s.id) AS total_items
        FROM {0}.salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE COALESCE(v.nombre, '') LIKE %s
        ORDER BY s.fecha DESC""".format(ALMACEN_DB)
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, (f'%{buscar}%',), page)
    cur.close()
    return render_template('salidas.html', salidas=items, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages)

@app.route('/registrar-salida', methods=['GET','POST'])
def registrar_salida():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT v.id, v.nombre, v.total, v.fecha, v.estado, u.correo
        FROM ventas v LEFT JOIN usuarios u ON v.cliente_id=u.id
        WHERE v.estado IN ('en espera','entregado') ORDER BY v.fecha DESC
    """)
    ventas = cur.fetchall()
    cur.execute(f"SELECT id, nombre, stock FROM {ALMACEN_DB}.productos WHERE estado='activo' ORDER BY nombre")
    productos = cur.fetchall()
    cur.close()

    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/registrar-salida')
        venta_id = request.form.get('venta_id') or None
        notas = request.form.get('notas', '')
        producto_ids = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')

        cur = mysql.connection.cursor()
        errores = []
        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i])
            except (ValueError, IndexError):
                continue
            if cant > 0:
                cur.execute(f"SELECT stock FROM {ALMACEN_DB}.productos WHERE id=%s", (pid,))
                prod = cur.fetchone()
                stock_actual = prod['stock'] if prod else 0
                if stock_actual < cant:
                    nombre_prod = prod.get('nombre', f'ID {pid}') if prod else f'ID {pid}'
                    errores.append(f'{nombre_prod}: stock insuficiente (hay {stock_actual}, necesitas {cant})')
        if errores:
            flash('No se puede registrar la salida: ' + '; '.join(errores), 'danger')
            cur.close()
            return redirect('/registrar-salida')

        cur.execute(f"INSERT INTO {ALMACEN_DB}.salidas (venta_id, notas) VALUES (%s, %s)", (venta_id, notas))
        salida_id = cur.lastrowid

        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i])
            except (ValueError, IndexError):
                continue
            if cant > 0:
                cur.execute(f"INSERT INTO {ALMACEN_DB}.detalle_salida (salida_id, producto_id, cantidad) VALUES (%s,%s,%s)",
                            (salida_id, pid, cant))
                cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock-%s WHERE id=%s AND stock>=%s", (cant, pid, cant))

        mysql.connection.commit()
        cur.close()
        flash(f'Salida #{salida_id} registrada correctamente.', 'success')
        return redirect(f'/comprobante-salida/{salida_id}')

    return render_template('registrar_salida.html', ventas=ventas, productos=productos)

@app.route('/comprobante-salida/<int:id>')
def comprobante_salida(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT s.id, s.fecha, s.notas,
               v.id AS venta_id, v.nombre AS cliente_nombre, v.documento AS cliente_doc,
               v.total AS venta_total, v.fecha AS venta_fecha
        FROM {0}.salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE s.id=%s
    """.format(ALMACEN_DB), (id,))
    salida = cur.fetchone()
    if not salida:
        flash('Salida no encontrada.', 'danger')
        return redirect('/salidas')
    cur.execute("""
        SELECT ds.cantidad, pr.nombre AS producto_nombre, pr.stock AS stock_actual
        FROM {0}.detalle_salida ds
        JOIN {0}.productos pr ON ds.producto_id=pr.id
        WHERE ds.salida_id=%s
    """.format(ALMACEN_DB), (id,))
    items = cur.fetchall()
    cur.close()
    return render_template('comprobante_salida.html', salida=salida, items=items)

@app.route('/eliminar-salida/<int:id>')
def eliminar_salida(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT producto_id, cantidad FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=%s", (id,))
    items = cur.fetchall()
    for item in items:
        cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock+%s WHERE id=%s", (item['cantidad'], item['producto_id']))
    cur.execute(f"DELETE FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=%s", (id,))
    cur.execute(f"DELETE FROM {ALMACEN_DB}.salidas WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Salida eliminada y stock revertido.', 'success')
    return redirect('/salidas')

# ─────────────────────────────────────────────
# ERROR HANDLER (loguea el error exacto)
# ─────────────────────────────────────────────
@app.errorhandler(Exception)
def manejar_error(e):
    if isinstance(e, HTTPException):
        return e
    # Se persiste la excepcion para el KPI de estabilidad antes de decidir
    # que se devuelve al visitante. La funcion nunca propaga un error propio:
    # una metrica no puede impedir que la app conteste.
    try:
        from kpi.collectors import persist_error
        persist_error(e)
    except Exception:
        pass
    try:
        app.logger.error('Error no controlado: %s', e, exc_info=True)
    except Exception:
        pass
    # Solo mostrar el detalle en pantalla si se activa DEBUG_ERRORS=1 en Render
    if os.environ.get('DEBUG_ERRORS') == '1':
        return f"<pre>{traceback.format_exc()}</pre>", 500
    return "Ocurrió un error interno. Revisa los logs.", 500

# ─────────────────────────────────────────────
# PRO08: VERIFICAR INVENTARIO POR CANTIDAD
# ─────────────────────────────────────────────
@app.route('/verificar-inventario')
def verificar_inventario():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    filtro = request.args.get('filtro', 'todos')
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    where = "WHERE p.nombre LIKE %s"
    params = [f'%{buscar}%']
    if filtro == 'sin_stock':
        where += " AND p.stock=0 AND p.estado='activo'"
    elif filtro == 'critico':
        where += " AND p.stock BETWEEN 1 AND 3 AND p.estado='activo'"
    elif filtro == 'bajo':
        where += " AND p.stock BETWEEN 4 AND 10 AND p.estado='activo'"
    elif filtro == 'normal':
        where += " AND p.stock > 10 AND p.estado='activo'"
    elif filtro == 'inactivos':
        where += " AND p.estado='inactivo'"
    sql_count = f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos p {where}"
    sql_data = f"""SELECT p.id, p.nombre, p.stock, p.precio, p.categoria, p.imagen, p.estado
        FROM {ALMACEN_DB}.productos p {where} ORDER BY p.stock ASC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)

    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.productos WHERE estado='activo' AND stock=0")
    sin_stock = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.productos WHERE estado='activo' AND stock BETWEEN 1 AND 3")
    critico = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.productos WHERE estado='activo' AND stock BETWEEN 4 AND 10")
    bajo = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.productos WHERE estado='activo' AND stock > 10")
    normal = cur.fetchone()['n']
    cur.close()
    return render_template('verificar_inventario.html',
                           productos=items, filtro=filtro, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages,
                           sin_stock=sin_stock, critico=critico, bajo=bajo, normal=normal)

# ─────────────────────────────────────────────
# PRO20: AGREGAR PRODUCTOS PARA ENVÍO Y ENTREGA
# ─────────────────────────────────────────────
@app.route('/entregas/agregar-productos/<int:entrega_id>', methods=['POST'])
def agregar_productos_entrega(entrega_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    producto_ids = request.form.getlist('producto_id[]')
    cantidades = request.form.getlist('cantidad[]')
    cur.execute("SELECT venta_id FROM seguimiento_entregas WHERE id=%s", (entrega_id,))
    enc = cur.fetchone()
    if not enc:
        cur.close()
        flash('Entrega no encontrada.', 'danger')
        return redirect('/entregas')
    cur.execute("DELETE FROM entrega_productos WHERE entrega_id=%s", (entrega_id,))
    for i in range(len(producto_ids)):
        try:
            pid = int(producto_ids[i])
            cant = int(cantidades[i]) if cantidades[i] else 1
        except (ValueError, IndexError):
            continue
        if cant > 0:
            cur.execute("INSERT INTO entrega_productos (entrega_id, producto_id, cantidad) VALUES (%s,%s,%s)",
                        (entrega_id, pid, cant))
    mysql.connection.commit()
    _invalidar_cache_kpi()
    cur.close()
    flash('Productos de entrega actualizados.', 'success')
    return redirect('/entregas')

@app.route('/entregas/asignar/<int:venta_id>', methods=['GET','POST'])
def asignar_entrega(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, total, nombre, documento FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta:
        cur.close()
        flash('Venta no encontrada.', 'danger')
        return redirect('/historial-compras')
    cur.execute("""
        SELECT d.producto_id, p.nombre, p.imagen, d.cantidad, d.precio
        FROM detalle_venta d JOIN {0}.productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """.format(ALMACEN_DB), (venta_id,))
    productos_venta = cur.fetchall()
    cur.execute("SELECT id, direccion, distrito, referencia FROM direcciones WHERE usuario_id=%s", (venta['documento'] or '',))
    direcciones = cur.fetchall()

    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/entregas/asignar/{venta_id}')
        direccion = request.form.get('direccion_envio', '')
        fecha_est = request.form.get('fecha_estimada') or None
        notas = request.form.get('notas', '')
        # Una venta ya nace con seguimiento. Si existe, se actualiza en vez
        # de insertar otra fila: el KPI y /entregas deben ver una sola.
        cur.execute("""SELECT id FROM seguimiento_entregas
                       WHERE venta_id=%s ORDER BY id DESC LIMIT 1""",
                    (venta_id,))
        existente = cur.fetchone()
        if existente:
            entrega_id = existente['id']
            cur.execute("""UPDATE seguimiento_entregas
                           SET direccion_envio=%s, fecha_estimada=%s, notas=%s
                           WHERE id=%s""",
                        (direccion, fecha_est, notas, entrega_id))
            cur.execute("DELETE FROM entrega_productos WHERE entrega_id=%s",
                        (entrega_id,))
            accion = 'actualizada'
        else:
            cur.execute("""INSERT INTO seguimiento_entregas
                           (venta_id, estado, direccion_envio, fecha_estimada, notas)
                           VALUES (%s,'pendiente',%s,%s,%s)""",
                        (venta_id, direccion, fecha_est, notas))
            entrega_id = cur.lastrowid
            accion = 'creada'
        producto_ids = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')
        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i]) if cantidades[i] else 1
            except (ValueError, IndexError):
                continue
            if cant > 0:
                cur.execute("INSERT INTO entrega_productos (entrega_id, producto_id, cantidad) VALUES (%s,%s,%s)",
                            (entrega_id, pid, cant))
        mysql.connection.commit()
        _invalidar_cache_kpi()
        cur.close()
        flash(f'Entrega #{entrega_id} {accion} para venta #{venta_id}.', 'success')
        return redirect('/entregas')

    cur.close()
    return render_template('asignar_entrega.html', venta=venta, productos_venta=productos_venta, direcciones=direcciones)

# ─────────────────────────────────────────────
# PRO25-26: VERIFICAR / CONSULTAR REGISTROS
# ─────────────────────────────────────────────
@app.route('/verificar-registros')
def verificar_registros():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    seccion = request.args.get('seccion', 'resumen')
    cur = mysql.connection.cursor()
    stats = {}
    cur.execute("SELECT COUNT(*) AS n FROM ventas")
    stats['total_ventas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_venta")
    stats['total_detalle_ventas'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.ingresos")
    stats['total_ingresos'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.detalle_ingreso")
    stats['total_detalle_ingresos'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.salidas")
    stats['total_salidas'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.detalle_salida")
    stats['total_detalle_salidas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM seguimiento_entregas")
    stats['total_entregas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM usuarios")
    stats['total_usuarios'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.productos")
    stats['total_productos'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.proveedores")
    stats['total_proveedores'] = cur.fetchone()['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.productos_para_pedir")
    stats['total_pedidos'] = cur.fetchone()['n']

    datos = []
    if seccion == 'ventas':
        cur.execute("SELECT v.id, v.total, v.fecha, v.estado, v.nombre, v.documento, u.correo FROM ventas v LEFT JOIN usuarios u ON v.cliente_id=u.id ORDER BY v.fecha DESC LIMIT 50")
        datos = cur.fetchall()
    elif seccion == 'ingresos':
        cur.execute(f"""SELECT i.id, i.fecha, COALESCE(i.estado,'vigente') AS estado,
                               COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
                               (SELECT SUM(cantidad) FROM {ALMACEN_DB}.detalle_ingreso WHERE ingreso_id=i.id) AS items
                        FROM {ALMACEN_DB}.ingresos i
                        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
                        ORDER BY i.fecha DESC LIMIT 50""")
        datos = cur.fetchall()
    elif seccion == 'salidas':
        cur.execute(f"SELECT s.id, s.fecha, v.nombre AS cliente_nombre, (SELECT SUM(cantidad) FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=s.id) AS items FROM {ALMACEN_DB}.salidas s LEFT JOIN ventas v ON s.venta_id=v.id ORDER BY s.fecha DESC LIMIT 50")
        datos = cur.fetchall()
    elif seccion == 'entregas':
        cur.execute("SELECT e.id, e.estado, e.fecha_entrega, v.nombre AS cliente_nombre, v.total FROM seguimiento_entregas e JOIN ventas v ON e.venta_id=v.id ORDER BY e.created_at DESC LIMIT 50")
        datos = cur.fetchall()
    elif seccion == 'usuarios':
        cur.execute("SELECT id, correo, rol, estado, created_at FROM usuarios ORDER BY created_at DESC LIMIT 50")
        datos = cur.fetchall()

    cur.close()
    return render_template('verificar_registros.html', stats=stats, seccion=seccion, datos=datos)

# ─────────────────────────────────────────────
# PRO27: INFORME FINAL DEL DÍA
# ─────────────────────────────────────────────
@app.route('/informe-diario')
def informe_diario():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    hoy = datetime.now().strftime('%Y-%m-%d')
    fecha_str = request.args.get('fecha', hoy)
    cur = mysql.connection.cursor()

    cur.execute("SELECT id, total, fecha, estado, nombre, documento FROM ventas WHERE DATE(fecha)=%s ORDER BY fecha", (fecha_str,))
    ventas_dia = cur.fetchall()
    cur.execute("SELECT COALESCE(SUM(total),0) AS total FROM ventas WHERE DATE(fecha)=%s AND COALESCE(estado,'') <> 'cancelado'", (fecha_str,))
    ingresos_ventas = float(cur.fetchone()['total'])
    cur.execute("SELECT COUNT(*) AS n FROM ventas WHERE DATE(fecha)=%s AND COALESCE(estado,'') <> 'cancelado'", (fecha_str,))
    num_ventas = cur.fetchone()['n']

    # Los ingresos anulados no cuentan en el informe del dia (igual que las
    # ventas canceladas no cuentan como venta).
    cur.execute(f"""
        SELECT i.id, i.fecha,
               COALESCE(NULLIF(i.proveedor_nombre,''), p.nombre) AS proveedor_nombre,
               (SELECT SUM(di.cantidad) FROM {ALMACEN_DB}.detalle_ingreso di WHERE di.ingreso_id=i.id) AS items,
               (SELECT COALESCE(SUM(di.cantidad*di.precio_compra),0) FROM {ALMACEN_DB}.detalle_ingreso di WHERE di.ingreso_id=i.id) AS costo
        FROM {ALMACEN_DB}.ingresos i LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        WHERE DATE(i.fecha)=%s AND COALESCE(i.estado,'vigente') <> 'anulado'
        ORDER BY i.fecha
    """, (fecha_str,))
    ingresos_dia = cur.fetchall()
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.ingresos WHERE DATE(fecha)=%s "
                f"AND COALESCE(estado,'vigente') <> 'anulado'", (fecha_str,))
    num_ingresos = cur.fetchone()['n']

    cur.execute(f"""
        SELECT s.id, s.fecha, v.nombre AS cliente_nombre,
               (SELECT SUM(ds.cantidad) FROM {ALMACEN_DB}.detalle_salida ds WHERE ds.salida_id=s.id) AS items
        FROM {ALMACEN_DB}.salidas s LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE DATE(s.fecha)=%s ORDER BY s.fecha
    """, (fecha_str,))
    salidas_dia = cur.fetchall()
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.salidas WHERE DATE(fecha)=%s", (fecha_str,))
    num_salidas = cur.fetchone()['n']

    cur.execute("""
        SELECT e.id, e.estado, e.fecha_entrega, v.nombre AS cliente_nombre, v.total
        FROM seguimiento_entregas e JOIN ventas v ON e.venta_id=v.id
        WHERE DATE(e.created_at)=%s OR DATE(e.fecha_entrega)=%s
        ORDER BY e.created_at
    """, (fecha_str, fecha_str))
    entregas_dia = cur.fetchall()
    num_entregadas = sum(1 for e in entregas_dia if e['estado'] == 'entregado')
    num_pendientes = sum(1 for e in entregas_dia if e['estado'] != 'entregado')

    cur.close()
    return render_template('informe_diario.html',
                           fecha=fecha_str,
                           ventas_dia=ventas_dia, ingresos_ventas=ingresos_ventas, num_ventas=num_ventas,
                           ingresos_dia=ingresos_dia, num_ingresos=num_ingresos,
                           salidas_dia=salidas_dia, num_salidas=num_salidas,
                           entregas_dia=entregas_dia, num_entregadas=num_entregadas, num_pendientes=num_pendientes)

# ─────────────────────────────────────────────
# PRO02: DETALLE DE PRODUCTO
# ─────────────────────────────────────────────
@app.route('/producto/<int:id>')
def detalle_producto(id):
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT {COLS_PRODUCTO} FROM {ALMACEN_DB}.productos WHERE id=%s AND estado='activo'", (id,))
    producto = cur.fetchone()
    if producto:
        extras = _imagenes_extra(cur, [id]).get(id, [])
        imagenes = ([producto['imagen']] if producto['imagen'] else []) + extras
        imagenes = imagenes[:MAX_IMAGENES_PRODUCTO]
    cur.close()
    if not producto:
        flash('Producto no encontrado.', 'danger')
        return redirect('/')
    # KPI: si venia buscando algo, esta visita a un producto es lo que hizo
    # que esa busqueda valiera.
    kpi_reg.marcar_producto_visto(id)
    return render_template('producto_detalle.html', producto=producto, imagenes=imagenes)

# ─────────────────────────────────────────────
# PRO17-18: DIRECCIONES DEL CLIENTE
# ─────────────────────────────────────────────
@app.route('/direcciones')
def mis_direcciones():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM direcciones WHERE usuario_id=%s ORDER BY predeterminada DESC, created_at DESC", (user_id,))
    dirs = cur.fetchall()
    cur.close()
    return render_template('direcciones.html', direcciones=dirs)

@app.route('/direcciones/nueva', methods=['GET','POST'])
def nueva_direccion():
    if 'user_id' not in session:
        return redirect('/login')
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/direcciones')
        user_id = session['user_id']
        direccion = request.form.get('direccion', '').strip()
        distrito = request.form.get('distrito', '').strip()
        referencia = request.form.get('referencia', '').strip()
        predeterminada = 1 if request.form.get('predeterminada') else 0
        if not direccion:
            flash('La dirección es obligatoria.', 'danger')
            return redirect('/direcciones/nueva')
        cur = mysql.connection.cursor()
        if predeterminada:
            cur.execute("UPDATE direcciones SET predeterminada=0 WHERE usuario_id=%s", (user_id,))
        cur.execute("INSERT INTO direcciones (usuario_id, direccion, distrito, referencia, predeterminada) VALUES (%s,%s,%s,%s,%s)",
                    (user_id, direccion, distrito, referencia, predeterminada))
        mysql.connection.commit()
        cur.close()
        flash('Dirección agregada.', 'success')
        return redirect('/direcciones')
    return render_template('direccion_form.html', editar=None)

@app.route('/direcciones/editar/<int:id>', methods=['GET','POST'])
def editar_direccion(id):
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/direcciones')
        direccion = request.form.get('direccion', '').strip()
        distrito = request.form.get('distrito', '').strip()
        referencia = request.form.get('referencia', '').strip()
        predeterminada = 1 if request.form.get('predeterminada') else 0
        if not direccion:
            flash('La dirección es obligatoria.', 'danger')
            return redirect(f'/direcciones/editar/{id}')
        if predeterminada:
            cur.execute("UPDATE direcciones SET predeterminada=0 WHERE usuario_id=%s", (user_id,))
        cur.execute("UPDATE direcciones SET direccion=%s, distrito=%s, referencia=%s, predeterminada=%s WHERE id=%s AND usuario_id=%s",
                    (direccion, distrito, referencia, predeterminada, id, user_id))
        mysql.connection.commit()
        cur.close()
        flash('Dirección actualizada.', 'success')
        return redirect('/direcciones')
    cur.execute("SELECT * FROM direcciones WHERE id=%s AND usuario_id=%s", (id, user_id))
    dir_editar = cur.fetchone()
    cur.close()
    if not dir_editar:
        flash('Dirección no encontrada.', 'danger')
        return redirect('/direcciones')
    return render_template('direccion_form.html', editar=dir_editar)

@app.route('/direcciones/eliminar/<int:id>')
def eliminar_direccion(id):
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM direcciones WHERE id=%s AND usuario_id=%s", (id, user_id))
    mysql.connection.commit()
    cur.close()
    flash('Dirección eliminada.', 'success')
    return redirect('/direcciones')

# ─────────────────────────────────────────────
# PRO19+PRO21: CHECKOUT (Dirección → Pago → Resumen)
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# CULQI: COBRO REAL CON LA API v2 DE CARGOS
# ─────────────────────────────────────────────
# El Checkout v4 solo tokeniza la tarjeta en el navegador: NO cobra. El dinero
# se mueve cuando el servidor llama a POST /v2/charges con la llave secreta.
# Antes de este bloque la app guardaba la venta y descontaba stock sin cobrar.

_CULQI_DECLINE_MSG = {
    'expired_card': 'Tu tarjeta está vencida o la fecha ingresada es incorrecta.',
    'stolen_card': 'El banco reportó la tarjeta como robada. Usa otro medio de pago.',
    'lost_card': 'El banco reportó la tarjeta como perdida. Usa otro medio de pago.',
    'insufficient_funds': 'Tu tarjeta no tiene fondos suficientes. Prueba con otra tarjeta.',
    'contact_issuer': 'El banco rechazó la operación. Contacta a tu banco para autorizarla.',
    'invalid_cvv': 'El código de seguridad (CVV) es inválido.',
    'incorrect_cvv': 'El código de seguridad (CVV) es incorrecto.',
    'too_many_attempts_cvv': 'Demasiados intentos con el CVV. Espera unos minutos o usa otra tarjeta.',
    'issuer_not_available': 'El banco no respondió. Intenta el pago nuevamente.',
    'issuer_decline_operation': 'El banco rechazó la operación. Prueba con otra tarjeta.',
    'invalid_card': 'La tarjeta no permite este tipo de compra. Contacta a tu banco.',
    'processing_error': 'Culqi no pudo procesar el pago. Intenta nuevamente en unos minutos.',
    'fraudulent': 'El banco marcó la operación como sospechosa. Usa otro medio de pago.',
    'culqi_card': 'Estás usando una tarjeta de prueba de Culqi en modo producción.',
}


def culqi_configurado():
    """True si el servidor tiene lo mínimo para cobrar con tarjeta."""
    return bool(CULQI_SECRET_KEY and CULQI_PUBLIC_KEY)


def _culqi_headers():
    return {'Authorization': 'Bearer %s' % CULQI_SECRET_KEY,
            'Content-Type': 'application/json'}


def _culqi_cargo_aprobado(data):
    if not data or not data.get('id'):
        return False
    if (data.get('state') or '').strip().lower() == 'exitosa':
        return True
    if (data.get('response_code') or '').strip().lower() == 'venta_exitosa':
        return True
    if data.get('paid') is True:
        return True
    outcome = data.get('outcome') or {}
    return (outcome.get('type') or '').strip().lower() == 'venta_exitosa'


def _culqi_requiere_3ds(data):
    """El banco pidió autenticación 3DS: el cargo NO se cobró todavía.

    Culqi responde HTTP 200 con `outcome.decline_code=authentication_required`
    (a veces `DNGE0116`). En ese caso hay que autenticar en el navegador con
    la librería Culqi3DS y volver a crear el cargo con `authentication_3DS`.
    """
    if not isinstance(data, dict):
        return False
    # Respuesta típica de Culqi cuando pide 3DS (sin objeto charge):
    # {"user_message":"El usuario necesita autenticarse","action_code":"REVIEW"}
    if (data.get('action_code') or '').strip().upper() in (
            'REVIEW', 'AUTHENTICATE', 'AUTHENTICATION_REQUIRED'):
        return True
    outcome = data.get('outcome') or {}
    if (outcome.get('decline_code') or '').strip().lower() == 'authentication_required':
        return True
    if (outcome.get('code') or '').strip().upper() in ('DNGE0116',):
        return True
    return data.get('authentication_required') is True


def _culqi_mensaje_error(data, status):
    """Convierte el error de Culqi en algo que el cliente pueda entender."""
    data = data if isinstance(data, dict) else {}
    decline = (data.get('decline_code') or '').strip().lower()
    if decline in _CULQI_DECLINE_MSG:
        return _CULQI_DECLINE_MSG[decline]
    tipo = (data.get('type') or '').strip().lower()
    outcome = ((data.get('outcome') or {}).get('type') or '').strip().lower()
    if tipo == 'authentication_required' or outcome in ('review', 'revision'):
        return ('Tu banco pidió una verificación adicional (3DS). '
                'Vuelve a intentar el pago y completa la validación.')
    mensaje = (data.get('user_message') or data.get('merchant_message')
               or data.get('message') or '').strip()
    if mensaje:
        return mensaje
    if status == 401:
        return 'La configuración de Culqi no es válida (llave secreta rechazada).'
    if status == 429:
        return 'Demasiados intentos de pago. Espera un momento y vuelve a probar.'
    return 'Culqi rechazó el cobro (código HTTP %s).' % status


def _culqi_crear_cargo(amount_centimos, email, source_id, descripcion,
                       antifraude=None, metadata=None, auth_3ds=None):
    """Cobra en Culqi. Devuelve (aprobado, datos, mensaje).

    `datos` es la respuesta cruda (se guarda para auditoría). Nunca lanza
    excepción: cualquier falla se traduce a un mensaje, de modo que el
    checkout nunca se quede cargando.

    `auth_3ds` son los parámetros que devuelve la librería Culqi3DS tras el
    challenge del banco. Se mandan en el segundo intento del cargo, con el
    mismo token y el mismo device fingerprint.
    """
    if not CULQI_SECRET_KEY:
        return False, None, ('El pago con tarjeta no está disponible: '
                             'falta configurar CULQI_SECRET_KEY en el servidor.')
    payload = {
        'amount': int(amount_centimos),
        'currency_code': CULQI_CURRENCY,
        'email': email,
        'source_id': source_id,
        'description': descripcion[:80],
        'capture': True,
    }
    if antifraude:
        payload['antifraud_details'] = antifraude
    if metadata:
        payload['metadata'] = metadata
    if auth_3ds:
        payload['authentication_3DS'] = auth_3ds
    try:
        resp = requests.post('%s/charges' % CULQI_API_BASE, json=payload,
                             headers=_culqi_headers(), timeout=CULQI_TIMEOUT)
    except requests.Timeout:
        return False, None, ('Culqi no respondió a tiempo. No vuelvas a pagar '
                             'todavía: revisa tu correo o el panel de Culqi en '
                             'unos minutos antes de reintentar.')
    except requests.RequestException as exc:
        print(f"[culqi] error de red al crear cargo: {exc}")
        return False, None, ('No se pudo conectar con Culqi. Revisa tu conexión '
                             'e intenta nuevamente. No se realizó ningún cobro.')
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code in (200, 201) and _culqi_cargo_aprobado(data):
        return True, data, ''
    mensaje = _culqi_mensaje_error(data, resp.status_code)
    print(f"[culqi] cargo rechazado HTTP {resp.status_code}: "
          f"{(data or {}).get('code')} / {(data or {}).get('decline_code')}")
    return False, data, mensaje


def _culqi_reembolsar(charge_id, amount_centimos, motivo='solicitud_comprador'):
    """Devuelve el dinero si el cargo se aprobó pero la venta no pudo guardarse."""
    if not (CULQI_SECRET_KEY and charge_id):
        return False
    try:
        resp = requests.post('%s/refunds' % CULQI_API_BASE,
                             json={'amount': int(amount_centimos),
                                   'charge_id': charge_id,
                                   'reason': motivo},
                             headers=_culqi_headers(), timeout=CULQI_TIMEOUT)
        if resp.status_code in (200, 201):
            print(f"[culqi] devolución solicitada para {charge_id}")
            return True
        print(f"[culqi] no se pudo devolver {charge_id}: HTTP "
              f"{resp.status_code} {resp.text[:300]}")
    except requests.RequestException as exc:
        print(f"[culqi] error de red al reembolsar {charge_id}: {exc}")
    return False


def _venta_por_token(culqi_token):
    """Venta ya registrada con este token (para reintentos y carreras)."""
    if not culqi_token:
        return None
    try:
        cur = mysql.connection.cursor()
        cur.execute("SELECT id FROM ventas WHERE culqi_token=%s "
                    "ORDER BY id DESC LIMIT 1", (culqi_token,))
        fila = cur.fetchone()
        cur.close()
        return fila
    except Exception:
        return None


@app.route('/checkout')
def checkout():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = int(session['user_id'])
    cur = mysql.connection.cursor()
    if 'guest_id' in session:
        cur.execute("UPDATE carrito SET usuario_id=%s WHERE usuario_id=%s", (str(user_id), session['guest_id']))
        mysql.connection.commit()
        # El carrito del invitado pasa a ser del usuario: el contador cambia
        # para las dos claves.
        _carrito_invalidar(user_id, session.get('guest_id'))
    cur.execute(f"""
        SELECT c.id, c.producto_id, p.nombre, p.precio, p.imagen, c.cantidad
        FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id
        WHERE c.usuario_id=%s
    """, (user_id,))
    items = cur.fetchall()
    if not items:
        flash('Su carrito está vacío.', 'warning')
        return redirect('/carrito')
    total = sum(p['precio'] * p['cantidad'] for p in items)
    cur.execute("SELECT * FROM direcciones WHERE usuario_id=%s ORDER BY predeterminada DESC", (user_id,))
    direcciones = cur.fetchall()
    cur.execute("SELECT correo, nombre, apellido, telefono, documento_tipo, documento_numero FROM usuarios WHERE id=%s", (user_id,))
    usuario = cur.fetchone()
    cur.close()
    kpi_reg.sesion_compra(kpi_reg.PASO_CHECKOUT)
    return render_template('checkout.html', items=items, total=total,
                           direcciones=direcciones, usuario=usuario,
                           culqi_public_key=CULQI_PUBLIC_KEY,
                           culqi_rsa_id=CULQI_RSA_ID,
                           culqi_rsa_public_key=CULQI_RSA_PUBLIC_KEY,
                           culqi_currency=CULQI_CURRENCY)

@app.route('/procesar_compra', methods=['POST'])
def procesar_compra():
    # Soporte peticiones AJAX (checkout con overlay "Procesando compra...")
    es_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def _chk_error(msg, destino='/checkout'):
        # KPI: el cliente llego al formulario pero no lo completo. Se guarda
        # el motivo (dni invalido, stock, campos vacios...) porque saber DONDE
        # se queda la gente no sirve de nada si no se sabe POR QUE.
        kpi_reg.fallo_checkout(msg)
        flash(msg, 'danger')
        if es_ajax:
            return jsonify({'ok': False, 'error': msg})
        return redirect(destino)

    if 'user_id' not in session:
        return redirect('/login')
    if not validate_csrf():
        return _chk_error('Token CSRF inválido.')
    user_id = int(session['user_id'])

    nombres = request.form.get('nombres', '').strip()
    apellidos = request.form.get('apellidos', '').strip()
    apellido_paterno = request.form.get('apellido_paterno', '').strip()
    apellido_materno = request.form.get('apellido_materno', '').strip()
    razon_social = request.form.get('razon_social', '').strip()
    doc_tipo = request.form.get('doc_tipo', 'dni')
    doc_numero = request.form.get('doc_numero', '').strip()
    telefono = request.form.get('telefono', '').strip()
    correo = request.form.get('correo', '').strip()
    metodo_pago = request.form.get('metodo_pago', 'tarjeta')
    direccion_envio = request.form.get('direccion_envio', '')
    notas_entrega = request.form.get('notas_entrega', '').strip()

    if not doc_numero or not telefono or not direccion_envio:
        return _chk_error('Todos los campos son obligatorios.')
    if doc_tipo not in ('dni', 'ruc'):
        return _chk_error('Tipo de documento inválido.')
    if not doc_numero.isdigit():
        return _chk_error('El número de documento solo puede contener dígitos.')
    if doc_tipo == 'dni' and len(doc_numero) != 8:
        return _chk_error('DNI debe tener exactamente 8 dígitos.')
    if doc_tipo == 'ruc' and len(doc_numero) != 11:
        return _chk_error('RUC debe tener exactamente 11 dígitos.')
    if not telefono.isdigit():
        return _chk_error('El celular solo puede contener dígitos.')
    if len(telefono) != 9:
        return _chk_error('El celular debe tener exactamente 9 dígitos.')

    # ── Nombre según el tipo de documento ──
    # DNI: nombres + apellido paterno + apellido materno (el checkout los pide
    # separados). RUC: una sola razón social.
    if doc_tipo == 'dni':
        # Se normalizan a mayúsculas y se validan (no se rechazan por estar en
        # minúscula: así no se bloquea a quien ya tiene el nombre guardado así).
        nombres = ' '.join(nombres.upper().split())
        apellido_paterno = ' '.join(apellido_paterno.upper().split())
        apellido_materno = ' '.join(apellido_materno.upper().split())
        if not (nombres and apellido_paterno and apellido_materno):
            return _chk_error('Completa nombres, apellido paterno y '
                              'apellido materno.')
        solo_letras = r"^[A-ZÁÉÍÓÚÑÜ\s'.-]+$"
        for campo, valor in (('nombre', nombres),
                             ('apellido paterno', apellido_paterno),
                             ('apellido materno', apellido_materno)):
            if re.search(r'\d', valor):
                return _chk_error(f'El {campo} no puede contener números.')
            if not re.match(solo_letras, valor):
                return _chk_error(f'El {campo} solo puede contener letras.')
        apellidos = f'{apellido_paterno} {apellido_materno}'.strip()
    else:
        razon_social = ' '.join(razon_social.upper().split())
        if len(razon_social) < 3:
            return _chk_error('Completa la razón social.')
        if re.search(r'[\x00-\x1f\x7f<>]', razon_social):
            return _chk_error('La razón social contiene caracteres inválidos.')
        # La venta guarda la razón social en `nombre`; `apellido` queda vacío.
        nombres = razon_social
        apellidos = ''

    # ── Pago: token de Culqi y validaciones previas ──
    culqi_token = request.form.get('culqi_token', '').strip()
    culqi_email = (request.form.get('culqi_email') or correo).strip()
    # Device fingerprint que genera Culqi3DS en el navegador. Mejora el
    # antifraude y es obligatorio para el flujo con autenticación 3DS.
    culqi_device_id = request.form.get('culqi_device_id', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_\-]{8,120}', culqi_device_id or ''):
        culqi_device_id = ''
    # Parámetros que devuelve Culqi3DS tras el challenge del banco. Solo se
    # aceptan si vienen completos; Culqi los valida igual contra el emisor.
    culqi_3ds = None
    _raw_3ds = request.form.get('culqi_3ds', '').strip()
    if _raw_3ds:
        try:
            _p3 = json.loads(_raw_3ds)
        except (ValueError, TypeError):
            _p3 = None
        if isinstance(_p3, dict) and all(_p3.get(k) for k in ('eci', 'xid', 'cavv')):
            culqi_3ds = {k: str(_p3[k])[:512]
                         for k in ('eci', 'xid', 'cavv', 'protocolVersion',
                                   'directoryServerTransactionId')
                         if _p3.get(k)}
    if metodo_pago not in ('tarjeta', 'yape', 'transferencia'):
        metodo_pago = 'tarjeta'
    # El Checkout de Culqi devuelve un token distinto para cada medio:
    # tkn_... (tarjeta) o ype_... (Yape). El metodo real se deduce del token,
    # no de lo que diga el formulario.
    if metodo_pago != 'transferencia' and culqi_token:
        metodo_pago = 'yape' if culqi_token.startswith('ype_') else 'tarjeta'
    if metodo_pago in ('tarjeta', 'yape'):
        if not culqi_configurado():
            return _chk_error('El pago con tarjeta o Yape no está disponible en '
                              'este momento. Usa transferencia bancaria o '
                              'intenta más tarde.')
        if not culqi_token:
            return _chk_error('No se recibió la autorización de Culqi. '
                              'Vuelve a intentar el pago.')
        if not re.fullmatch(r'[A-Za-z0-9_\-]{6,80}', culqi_token):
            return _chk_error('El token de pago no es válido. Vuelve a intentarlo.')
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', culqi_email):
            return _chk_error('Ingresa un correo electrónico válido para el pago.')

    cur = mysql.connection.cursor()
    charge_id = None
    total_centimos = 0
    cargo = None
    try:
        # Un token de Culqi solo puede originar una venta: si el cliente
        # reintenta (doble clic o recarga), se devuelve la venta ya creada.
        if metodo_pago in ('tarjeta', 'yape'):
            cur.execute("SELECT id FROM ventas WHERE culqi_token=%s "
                        "ORDER BY id DESC LIMIT 1", (culqi_token,))
            venta_token = cur.fetchone()
            if venta_token:
                if es_ajax:
                    return jsonify({'ok': True,
                                    'redirect': f'/confirmacion/{venta_token["id"]}'})
                return redirect(f'/confirmacion/{venta_token["id"]}')

        cur.execute("SELECT id FROM ventas WHERE cliente_id=%s AND fecha > NOW() - INTERVAL 30 SECOND ORDER BY id DESC LIMIT 1", (user_id,))
        venta_reciente = cur.fetchone()
        if venta_reciente:
            # Reintento de una compra ya registrada: devolver su confirmación/boleta
            if es_ajax:
                return jsonify({'ok': True,
                                'redirect': f'/confirmacion/{venta_reciente["id"]}'})
            return redirect(f'/confirmacion/{venta_reciente["id"]}')

        cur.execute(f"""
            SELECT p.id, p.nombre, p.precio, c.cantidad
            FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id
            WHERE c.usuario_id=%s
        """, (user_id,))
        items = cur.fetchall()
        if not items:
            # Posible reintento: si la compra ya se registró, ir a su confirmación
            cur.execute("SELECT id FROM ventas WHERE cliente_id=%s AND fecha > NOW() - INTERVAL 5 MINUTE ORDER BY fecha DESC LIMIT 1", (user_id,))
            ultima = cur.fetchone()
            if ultima:
                if es_ajax:
                    return jsonify({'ok': True,
                                    'redirect': f'/confirmacion/{ultima["id"]}'})
                return redirect(f'/confirmacion/{ultima["id"]}')
            return _chk_error('Tu carrito está vacío.', '/carrito')
        total = 0
        for item in items:
            cur.execute(f"SELECT stock FROM {ALMACEN_DB}.productos WHERE id=%s", (item['id'],))
            row = cur.fetchone()
            stock = row['stock'] if row else 0
            if stock < item['cantidad']:
                return _chk_error(f'Stock insuficiente para: {item["nombre"]}. Solo quedan {stock} unidad(es).', '/carrito')
            total += item['precio'] * item['cantidad']

        total_centimos = int(round(float(total) * 100))

        if metodo_pago in ('tarjeta', 'yape') and total_centimos < CULQI_MONTO_MINIMO:
            return _chk_error('El monto mínimo para pagar con tarjeta o Yape '
                              'es S/ %.2f. Agrega más productos al carrito.'
                              % (CULQI_MONTO_MINIMO / 100.0))
        if metodo_pago == 'yape' and total_centimos > CULQI_YAPE_MONTO_MAXIMO:
            return _chk_error('El monto máximo para pagar con Yape es S/ %.2f. '
                              'Divide tu compra o usa tarjeta.'
                              % (CULQI_YAPE_MONTO_MAXIMO / 100.0))

        # 1) Cobrar PRIMERO. Si Culqi rechaza, no se escribe nada en la BD y
        #    el stock queda intacto.
        estado_pago = 'pendiente'
        if metodo_pago in ('tarjeta', 'yape'):
            antifraude = {
                'first_name': nombres.title(),
                'email': culqi_email,
                'address': direccion_envio[:100],
                'address_city': 'Lima',
                'country_code': 'PE',
                'phone_number': telefono,
            }
            # Con RUC no hay apellido: Culqi lo acepta si no se envía.
            if apellidos:
                antifraude['last_name'] = apellidos.title()
            if culqi_device_id:
                antifraude['device_finger_print_id'] = culqi_device_id
            aprobado, cargo, mensaje = _culqi_crear_cargo(
                total_centimos, culqi_email, culqi_token,
                f'Pedido Multiservicios Richard - {nombres} {apellidos}'.strip(),
                antifraude=antifraude,
                metadata={'cliente_id': str(user_id),
                          'documento': doc_numero,
                          'metodo': metodo_pago},
                # Yape no usa 3DS: solo las tarjetas pasan por el challenge.
                auth_3ds=culqi_3ds if metodo_pago == 'tarjeta' else None)
            if not aprobado:
                # El banco pide 3DS: todavía no se cobró nada y no se guarda
                # la venta. El navegador debe autenticar y reintentar.
                if (metodo_pago == 'tarjeta' and _culqi_requiere_3ds(cargo)
                        and not culqi_3ds):
                    if es_ajax:
                        return jsonify({'ok': False, 'requiere_3ds': True,
                                        'token_id': culqi_token,
                                        'monto': total_centimos,
                                        'email': culqi_email,
                                        'error': 'Tu banco pidió una verificación adicional.'})
                    return _chk_error('Tu banco pidió una verificación adicional '
                                      '(3DS). Vuelve a intentar el pago desde un '
                                      'navegador actualizado.')
                return _chk_error(mensaje)
            charge_id = (cargo or {}).get('id')
            estado_pago = 'pagado'

        # 2) Con el cobro aprobado (o con transferencia pendiente) se registra
        #    la venta. Todo va en la misma transacción.
        cur.execute("""INSERT INTO ventas
            (cliente_id, total, metodo_pago, direccion_envio, documento, doc_tipo,
             nombre, apellido, telefono, correo, notas_entrega, estado_pago,
             culqi_charge_id, culqi_token, culqi_respuesta, pagado_en)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, total, metodo_pago, direccion_envio, doc_numero, doc_tipo,
             nombres, apellidos, telefono, correo, notas_entrega, estado_pago,
             charge_id, culqi_token or None,
             json.dumps(cargo, ensure_ascii=False) if cargo else None,
             datetime.now() if estado_pago == 'pagado' else None))
        venta_id = cur.lastrowid
        # Toda venta nace con seguimiento para que /entregas y el KPI de la
        # seccion 1 lean exactamente la misma fuente desde el primer minuto.
        cur.execute("""INSERT INTO seguimiento_entregas
            (venta_id, estado, direccion_envio, notas)
            VALUES(%s, 'pendiente', %s, %s)""",
            (venta_id, direccion_envio,
             'Seguimiento creado automaticamente al registrar la venta.'))
        for item in items:
            cur.execute("INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio) VALUES(%s,%s,%s,%s)",
                        (venta_id, item['id'], item['cantidad'], item['precio']))
            # Descuento condicional: si otro cliente compro la ultima unidad
            # entre la validacion y este momento, rowcount es 0 y se aborta la
            # compra (antes se restaba stock a ciegas y se podia sobrevender).
            cur.execute(
                f"UPDATE {ALMACEN_DB}.productos SET stock=stock-%s "
                f"WHERE id=%s AND stock>=%s",
                (item['cantidad'], item['id'], item['cantidad']))
            if cur.rowcount != 1:
                raise _SinStock(f'Stock insuficiente para: {item["nombre"]}.')
            verificar_stock_bajo(cur, item['id'])
        cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (user_id,))
        _carrito_invalidar(user_id)

        cur.execute("""UPDATE usuarios SET
            nombre=IFNULL(nombre,%s), apellido=IFNULL(apellido,%s),
            telefono=IFNULL(telefono,%s), documento_tipo=IFNULL(documento_tipo,%s),
            documento_numero=IFNULL(documento_numero,%s)
            WHERE id=%s""",
            (nombres, apellidos, telefono, doc_tipo, doc_numero, user_id))

        mysql.connection.commit()
        _invalidar_cache_kpi()

        # KPI: la compra se completo. Se cierra la sesion de compra y se
        # marcan las busquedas de esta visita como convertidas, que es lo que
        # permite medir si el buscador vende.
        kpi_reg.sesion_compra(kpi_reg.PASO_VENTA, venta_id=venta_id,
                              estado=kpi_reg.ESTADO_CONVERTIDA)
        kpi_reg.marcar_busqueda_convertida(user_id)

        try:
            enviar_boleta_cliente(correo, venta_id)
        except Exception:
            pass

        if es_ajax:
            return jsonify({'ok': True,
                            'redirect': f'/confirmacion/{venta_id}'})
        return redirect(f'/confirmacion/{venta_id}')
    except _SinStock as exc:
        # Otro cliente se quedó con las unidades: no se guarda nada. Si el
        # cargo ya se había aprobado, se devuelve el dinero automáticamente.
        mysql.connection.rollback()
        ya = _venta_por_token(culqi_token) if metodo_pago in ('tarjeta', 'yape') else None
        if ya:
            if es_ajax:
                return jsonify({'ok': True,
                                'redirect': f'/confirmacion/{ya["id"]}'})
            return redirect(f'/confirmacion/{ya["id"]}')
        if charge_id and not (cargo or {}).get('duplicated'):
            _culqi_reembolsar(charge_id, total_centimos)
        return _chk_error(str(exc), '/carrito')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[procesar_compra] {e}")
        # Doble envío en paralelo: la otra petición ya guardó este token.
        ya = _venta_por_token(culqi_token) if metodo_pago in ('tarjeta', 'yape') else None
        if ya:
            if es_ajax:
                return jsonify({'ok': True,
                                'redirect': f'/confirmacion/{ya["id"]}'})
            return redirect(f'/confirmacion/{ya["id"]}')
        if charge_id and (cargo or {}).get('duplicated'):
            # El cargo ya existía (mismo token). No se reembolsa: pertenece a
            # la venta que la otra petición está terminando de guardar.
            error = ('Tu pago ya fue procesado. Revisa tu historial en unos '
                     'segundos para ver la confirmación.')
        elif charge_id:
            _culqi_reembolsar(charge_id, total_centimos)
            error = ('El cobro se realizó pero no pudimos registrar tu pedido. '
                     'Iniciamos la devolución del dinero; contáctanos si no la '
                     'ves reflejada en 48 horas.')
        else:
            error = 'Error al procesar la compra. Intenta nuevamente.'
        if es_ajax:
            return jsonify({'ok': False, 'error': error})
        flash(error, 'danger')
        return redirect('/carrito')
    finally:
        cur.close()

@app.route('/confirmacion/<int:id>')
def confirmacion(id):
    if 'user_id' not in session and ('rol' not in session or session['rol'] not in ['admin','administrador']):
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM ventas WHERE id=%s", (id,))
    venta = cur.fetchone()
    if not venta or not puede_ver_venta(venta):
        flash('Venta no encontrada.', 'danger')
        return redirect('/')
    cur.execute(f"""
        SELECT p.nombre, p.imagen, d.cantidad, d.precio
        FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """, (id,))
    productos = cur.fetchall()
    cur.close()
    return render_template('confirmacion.html', venta=venta, productos=productos, id=id)

# ─────────────────────────────────────────────
# PERFIL DEL CLIENTE
# ─────────────────────────────────────────────
@app.route('/perfil', methods=['GET','POST'])
def perfil():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/perfil')

        accion = request.form.get('accion', 'datos')

        # ── 1) Agregar / cambiar número de teléfono ──
        if accion == 'telefono':
            telefono = request.form.get('telefono', '').strip()
            if telefono and not re.fullmatch(r"[0-9]+", telefono):
                flash('El teléfono solo puede contener números.', 'danger')
                return redirect('/perfil')
            if len(telefono) > 9:
                flash('El teléfono no puede tener más de 9 dígitos.', 'danger')
                return redirect('/perfil')
            cur.execute("UPDATE usuarios SET telefono=%s WHERE id=%s",
                        (telefono or None, user_id))
            mysql.connection.commit()
            cur.close()
            flash('Número de teléfono guardado.', 'success')
            return redirect('/perfil')

        # ── 2) Cambiar correo electrónico ──
        if accion == 'correo':
            nuevo_correo = request.form.get('correo', '').strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", nuevo_correo):
                flash('Ingresa un correo electrónico válido.', 'danger')
                return redirect('/perfil')
            # Cambiar el correo es la puerta a un reseteo de contraseña: se
            # confirma con la clave actual para que una sesion robada no baste.
            clave = request.form.get('password', '')
            cur.execute("SELECT password FROM usuarios WHERE id=%s", (user_id,))
            fila = cur.fetchone()
            clave_ok = False
            if clave and fila and len(clave.encode('utf-8')) <= 72:
                try:
                    clave_ok = bcrypt.check_password_hash(fila['password'], clave)
                except (ValueError, TypeError):
                    clave_ok = False
            if not clave_ok:
                flash('Confirma tu contraseña actual para cambiar el correo.', 'danger')
                cur.close()
                return redirect('/perfil')
            cur.execute("SELECT id FROM usuarios WHERE correo=%s AND id!=%s",
                        (nuevo_correo, user_id))
            if cur.fetchone():
                flash('Ese correo ya está en uso.', 'danger')
            else:
                cur.execute("UPDATE usuarios SET correo=%s WHERE id=%s",
                            (nuevo_correo, user_id))
                session['correo'] = nuevo_correo
                mysql.connection.commit()
                flash('Correo electrónico actualizado.', 'success')
            cur.close()
            return redirect('/perfil')

        # ── 3) Datos personales (nombre, apellido, documento) ──
        nombre   = request.form.get('nombre', '').strip()
        apellido = request.form.get('apellido', '').strip()
        doc_tipo = request.form.get('documento_tipo', '').strip()
        doc_num  = request.form.get('documento_numero', '').strip()
        if doc_tipo not in ('', 'dni', 'ruc'):
            doc_tipo = ''

        # ── Validaciones de los campos ──
        errores = []
        if nombre and not re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+", nombre):
            errores.append('Los nombres solo pueden contener letras.')
        if apellido and not re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+", apellido):
            errores.append('Los apellidos solo pueden contener letras.')
        if doc_num and not re.fullmatch(r"[0-9]+", doc_num):
            errores.append('El número de documento solo puede contener números.')
        if doc_tipo == 'dni' and len(doc_num) > 8:
            errores.append('El DNI no puede tener más de 8 dígitos.')
        if doc_tipo == 'ruc' and len(doc_num) > 11:
            errores.append('El RUC no puede tener más de 11 dígitos.')

        if errores:
            for e in errores:
                flash(e, 'danger')
            cur.execute("""SELECT id, correo, rol, estado, created_at, nombre, apellido,
                           telefono, documento_tipo, documento_numero
                           FROM usuarios WHERE id=%s""", (user_id,))
            usuario = cur.fetchone()
            cur.close()
            usuario['nombre']   = nombre or usuario['nombre']
            usuario['apellido'] = apellido or usuario['apellido']
            usuario['documento_tipo']   = doc_tipo
            usuario['documento_numero'] = doc_num
            return render_template('perfil.html', usuario=usuario, mi_historial=[])

        cur.execute("""
            UPDATE usuarios SET nombre=%s, apellido=%s,
            documento_tipo=%s, documento_numero=%s WHERE id=%s
        """, (nombre or None, apellido or None,
              doc_tipo or None, doc_num or None, user_id))
        session['nombre']   = nombre
        session['apellido'] = apellido
        mysql.connection.commit()
        cur.close()
        flash('Datos actualizados.', 'success')
        return redirect('/perfil')
    cur.execute("""SELECT id, correo, rol, estado, created_at, nombre, apellido,
                   telefono, documento_tipo, documento_numero
                   FROM usuarios WHERE id=%s""", (user_id,))
    usuario = cur.fetchone()
    # P2: historial de estados propio (últimos 10) para timeline en perfil
    try:
        cur.execute("""
            SELECT estado_anterior, estado_nuevo, motivo, created_at
            FROM historial_estado_cliente WHERE usuario_id=%s
            ORDER BY created_at DESC LIMIT 10
        """, (user_id,))
        mi_historial = cur.fetchall()
    except Exception:
        mi_historial = []
    cur.close()
    return render_template('perfil.html', usuario=usuario, mi_historial=mi_historial)

@app.route('/perfil/solicitar-revision', methods=['POST'])
def solicitar_revision_estado():
    """Botón del cliente: pide revisión sin cambiar su estado (queda registrado)."""
    if 'user_id' not in session:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect('/perfil')
    user_id = session['user_id']
    motivo = (request.form.get('motivo') or 'Solicitud de revisión de estado').strip()[:300]
    cur = mysql.connection.cursor()
    cur.execute("SELECT COALESCE(estado,'activo') AS estado FROM usuarios WHERE id=%s", (user_id,))
    fila = cur.fetchone()
    actual = (fila['estado'] if fila else 'activo')
    try:
        cur.execute(
            "INSERT INTO historial_estado_cliente (usuario_id, estado_anterior, estado_nuevo, motivo, admin_id)"
            " VALUES (%s,%s,%s,%s,NULL)", (user_id, actual, actual, motivo))
        mysql.connection.commit()
    except Exception:
        pass
    cur.close()
    flash('Solicitud enviada. Un administrador la revisará.', 'success')
    return redirect('/perfil')

@app.route('/api/mi-estado')
def api_mi_estado():
    """Badge en tiempo real para el cliente."""
    if 'user_id' not in session:
        return jsonify({'ok': False}), 401
    cur = mysql.connection.cursor()
    cur.execute("SELECT COALESCE(estado,'activo') AS estado FROM usuarios WHERE id=%s",
                (session['user_id'],))
    fila = cur.fetchone()
    cur.close()
    est = (fila['estado'] if fila else 'activo')
    return jsonify({'ok': True, 'estado': est,
                    'nombre': ESTADOS_CLIENTE.get(est, {}).get('nombre', est),
                    'color': color_estado_cliente(est)})

@app.route('/cambiar-password', methods=['POST'])
def cambiar_password():
    if 'user_id' not in session:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect('/perfil')
    user_id = session['user_id']
    actual = request.form.get('actual', '')
    nueva = request.form.get('nueva', '')
    confirmar = request.form.get('confirmar_password', '')
    if not actual or not nueva:
        flash('Completa todos los campos.', 'danger')
        return redirect('/perfil')
    ok, msg = _password_valida(nueva)
    if not ok:
        flash(msg, 'danger')
        return redirect('/perfil')
    if nueva != confirmar:
        flash('Las contraseñas no coinciden.', 'danger')
        return redirect('/perfil')
    cur = mysql.connection.cursor()
    cur.execute("SELECT password FROM usuarios WHERE id=%s", (user_id,))
    user = cur.fetchone()
    if not user or not bcrypt.check_password_hash(user['password'], actual):
        flash('La contraseña actual es incorrecta.', 'danger')
        cur.close()
        return redirect('/perfil')
    h = bcrypt.generate_password_hash(nueva).decode('utf-8')
    cur.execute("UPDATE usuarios SET password=%s WHERE id=%s", (h, user_id))
    mysql.connection.commit()
    cur.close()
    flash('Contraseña cambiada correctamente.', 'success')
    return redirect('/perfil')

# ─────────────────────────────────────────────
# SEGUIMIENTO DE ENTREGAS PARA EL CLIENTE
# ─────────────────────────────────────────────
@app.route('/mis-entregas')
def mis_entregas():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    motivo_filtro = request.args.get('motivo', '').strip()
    cur = mysql.connection.cursor()
    cond_motivo = ""
    params = [user_id]
    if motivo_cancelacion_valido(motivo_filtro):
        cond_motivo = " AND c.motivo_codigo=%s"
        params.append(motivo_filtro)
    cur.execute(f"""
        SELECT v.id AS venta_id, v.total, v.fecha AS venta_fecha, v.estado,
               v.direccion_envio, v.notas_entrega, v.metodo_pago,
               s.id AS seg_id, s.estado AS seg_estado,
               s.fecha_estimada, s.fecha_entrega, s.notas AS seg_notas,
               c.motivo_codigo, c.motivo_detalle
        FROM ventas v
        LEFT JOIN seguimiento_entregas s ON s.venta_id = v.id
        LEFT JOIN cancelaciones c ON c.venta_id = v.id
        WHERE v.cliente_id=%s{cond_motivo}
        ORDER BY v.fecha DESC
    """, tuple(params))
    filas = cur.fetchall()
    entregas = []
    for v in filas:
        cur.execute(f"""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['venta_id'],))
        # Cancelable: todo lo que no sea terminal (cubre 'en espera' y
        # cualquier valor legacy como 'pendiente')
        entregas.append({**v, 'productos': cur.fetchall(),
                         'cancelable': (v['estado'] not in ('entregado', 'cancelado'))})
    cur.execute("SELECT COALESCE(estado,'activo') AS estado FROM usuarios WHERE id=%s", (user_id,))
    f = cur.fetchone()
    mi_estado = (f['estado'] if f else 'activo')

    # Resumen de cancelaciones del propio cliente (solo las suyas). Sin filtro
    # de fecha: el cliente quiere ver "que se me cancelo", no analizar tendencias.
    mis_cancelaciones = []
    resumen_cancel = {'n': 0, 'monto': 0.0}
    try:
        cur.execute("""
            SELECT v.id, v.total, v.fecha,
                   COALESCE(c.motivo_codigo,'sin_motivo') AS motivo_codigo,
                   c.motivo_detalle
            FROM ventas v
            LEFT JOIN cancelaciones c ON c.venta_id = v.id
            WHERE v.cliente_id=%s AND COALESCE(v.estado,'')='cancelado'
            ORDER BY v.fecha DESC
        """, (user_id,))
        for m in cur.fetchall():
            motivo_txt = etiqueta_motivo(m['motivo_codigo'])
            mis_cancelaciones.append({**m, 'motivo_etiqueta': motivo_txt})
            resumen_cancel['n'] += 1
            resumen_cancel['monto'] += float(m['total'] or 0.0)
        resumen_cancel['monto'] = round(resumen_cancel['monto'], 2)
    except Exception:
        mis_cancelaciones = []
        resumen_cancel = {'n': 0, 'monto': 0.0}

    cur.close()
    return render_template('mis_entregas.html', entregas=entregas, mi_estado=mi_estado,
                           motivo_filtro=motivo_filtro,
                           mis_cancelaciones=mis_cancelaciones,
                           resumen_cancel=resumen_cancel)

@app.route('/mis-entregas/cancelar/<int:venta_id>', methods=['POST'])
def cancelar_pedido(venta_id):
    """El cliente cancela su propio pedido (pendiente o en espera)."""
    if 'user_id' not in session:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect('/mis-entregas')
    user_id = session['user_id']
    motivo = (request.form.get('motivo') or '').strip()
    detalle = (request.form.get('detalle') or '').strip()[:300]
    if not motivo_cancelacion_valido(motivo):
        flash('Elige un motivo de cancelación.', 'danger')
        return redirect('/mis-entregas')
    if motivo == 'otro' and not detalle:
        flash('Describe el motivo de la cancelación.', 'danger')
        return redirect('/mis-entregas')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, estado, cliente_id, total FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta or venta['cliente_id'] != user_id:
        cur.close()
        flash('Pedido no encontrado.', 'danger')
        return redirect('/mis-entregas')
    if venta['estado'] == 'cancelado':
        cur.close()
        flash('Este pedido ya está cancelado.', 'info')
        return redirect('/mis-entregas')
    if venta['estado'] == 'entregado':
        cur.close()
        flash('Ya no se puede cancelar (pedido entregado). Contacta al administrador.', 'warning')
        return redirect('/mis-entregas')
    # Calcular monto de los artículos a cancelar y reducir el total de la venta
    cur.execute(f"SELECT COALESCE(SUM(d.cantidad * d.precio), 0) AS monto_articulos "
                f"FROM detalle_venta d WHERE d.venta_id=%s", (venta_id,))
    fila = cur.fetchone()
    monto_articulos = float(fila['monto_articulos'] or 0) if fila else 0.0
    nuevo_total = max(0.0, float(venta['total'] or 0) - monto_articulos)
    # Cambiar estado de venta y seguimiento a cancelado
    cur.execute("UPDATE ventas SET estado='cancelado', total=%s WHERE id=%s", (nuevo_total, venta_id))
    cur.execute("UPDATE seguimiento_entregas SET estado='cancelado' WHERE venta_id=%s", (venta_id,))
    # Reponer stock de los artículos
    restaurar_stock_venta(cur, venta_id)
    # Registrar motivo de cancelación
    registrar_cancelacion(cur, venta_id, motivo, detalle or None, 'cliente')
    mysql.connection.commit()
    _invalidar_cache_kpi()
    cur.close()
    flash(f'Pedido #{venta_id} cancelado. El stock fue devuelto y el monto actualizado.', 'success')
    return redirect('/mis-entregas')

@app.route('/api/mis-entregas-estados')
def api_mis_entregas_estados():
    """Devuelve los estados de las ventas del usuario para actualizar en tiempo real."""
    if 'user_id' not in session:
        return jsonify({'ok': False}), 401
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, estado FROM ventas WHERE cliente_id=%s AND COALESCE(oculta,0)=0 ORDER BY fecha DESC", (user_id,))
    rows = cur.fetchall()
    cur.close()
    return jsonify({'ok': True, 'entregas': [
        {'venta_id': r['id'], 'estado': r['estado']} for r in rows
    ]})

# ─────────────────────────────────────────────
# RECUPERAR CONTRASEÑA
# ─────────────────────────────────────────────
@app.route('/recuperar-password', methods=['GET','POST'])
def recuperar_password():
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/recuperar-password')
        correo = request.form.get('correo', '').strip()
        cur = mysql.connection.cursor()
        cur.execute("SELECT id FROM usuarios WHERE correo=%s", (correo,))
        user = cur.fetchone()
        if user:
            token = secrets.token_urlsafe(48)
            expira = datetime.now() + timedelta(hours=2)
            cur.execute("UPDATE usuarios SET recuperacion_token=%s, recuperacion_expira=%s WHERE id=%s",
                        (token, expira, user['id']))
            mysql.connection.commit()
            flash('Si el correo existe, recibirás un enlace para restablecer tu contraseña.', 'success')
        else:
            flash('Si el correo existe, recibirás un enlace para restablecer tu contraseña.', 'success')
        cur.close()
        return redirect('/login')
    return render_template('recuperar_password.html')

@app.route('/restablecer-password/<token>', methods=['GET','POST'])
def restablecer_password(token):
    cur = mysql.connection.cursor()
    cur.execute("SELECT id FROM usuarios WHERE recuperacion_token=%s AND recuperacion_expira>%s",
                (token, datetime.now()))
    user = cur.fetchone()
    if not user:
        cur.close()
        flash('El enlace es inválido o ha expirado.', 'danger')
        return redirect('/login')
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/restablecer-password/{token}')
        nueva = request.form.get('nueva', '')
        confirmar = request.form.get('confirmar_password', '')
        ok, msg = _password_valida(nueva)
        if not ok:
            flash(msg, 'danger')
            return redirect(f'/restablecer-password/{token}')
        if nueva != confirmar:
            flash('Las contraseñas no coinciden.', 'danger')
            return redirect(f'/restablecer-password/{token}')
        h = bcrypt.generate_password_hash(nueva).decode('utf-8')
        cur.execute("UPDATE usuarios SET password=%s, recuperacion_token=NULL, recuperacion_expira=NULL WHERE id=%s",
                    (h, user['id']))
        mysql.connection.commit()
        cur.close()
        flash('Contraseña restablecida correctamente. Ya puedes iniciar sesión.', 'success')
        return redirect('/login')
    cur.close()
    return render_template('restablecer_password.html', token=token)

# ─────────────────────────────────────────────
# FIX: BOLETA PDF CON AUTH + GUARDAR BOLETA CON OWNERSHIP
# ─────────────────────────────────────────────
@app.route('/boleta_pdf/<int:venta_id>')
def boleta_pdf(venta_id):
    if 'user_id' not in session and not _es_admin():
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, cliente_id FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    cur.close()
    if not venta or not puede_ver_venta(venta):
        flash('No tienes acceso a esta boleta.', 'danger')
        return redirect('/')
    pdf_bytes, _ = generar_boleta_pdf(venta_id)
    if not pdf_bytes:
        flash('Venta no encontrada.', 'danger')
        return redirect('/')
    filename = f"boleta_{venta_id}.pdf"
    filepath = os.path.join(app.root_path, 'static', filename)
    with open(filepath, 'wb') as f:
        f.write(pdf_bytes)
    return send_file(filepath, as_attachment=True)

@app.route('/guardar_boleta', methods=['POST'])
def guardar_boleta():
    if 'user_id' not in session and ('rol' not in session or session['rol'] not in ['admin','administrador']):
        return redirect('/login')
    venta_id = request.form.get('venta_id')
    doc = request.form.get('doc', '')
    nombre = request.form.get('nombre', '')
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, cliente_id FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta or not puede_ver_venta(venta):
        cur.close()
        flash('No tienes acceso a esta venta.', 'danger')
        return redirect('/')
    cur.execute("UPDATE ventas SET documento=%s, nombre=%s WHERE id=%s", (doc, nombre, venta_id))
    mysql.connection.commit()
    cur.close()
    return redirect(f'/boleta_pdf/{venta_id}')

# ─────────────────────────────────────────────
# FIX: ELIMINAR CARRITO CON OWNERSHIP CHECK
# ─────────────────────────────────────────────
@app.route('/eliminar_carrito/<int:id>')
def eliminar_carrito(id):
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM carrito WHERE id=%s AND usuario_id=%s", (id, usuario))
    mysql.connection.commit()
    _carrito_invalidar(usuario)
    cur.close()
    return redirect('/carrito')

# ─────────────────────────────────────────────
# FIX: AUMENTAR CANTIDAD CON VALIDACIÓN DE STOCK
# ─────────────────────────────────────────────
@app.route('/aumentar-cantidad/<int:id_producto>')
def aumentar_cantidad(id_producto):
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT stock FROM {ALMACEN_DB}.productos WHERE id=%s AND estado='activo'", (id_producto,))
    prod = cur.fetchone()
    if prod:
        cur.execute("SELECT SUM(cantidad) AS total FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id_producto))
        en_carrito = cur.fetchone()['total'] or 0
        if en_carrito < prod['stock']:
            cur.execute("UPDATE carrito SET cantidad=cantidad+1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
            mysql.connection.commit()
            _carrito_invalidar(usuario)
        else:
            flash('No hay más stock disponible.', 'warning')
    cur.close()
    return redirect('/carrito')

# ─────────────────────────────────────────────
# FIX: AGREGAR SOLO PRODUCTOS ACTIVOS
# ─────────────────────────────────────────────
def _destino_despues_agregar():
    """Devuelve adónde redirigir tras agregar al carrito (solo rutas internas)."""
    destino = request.args.get('next', '')
    if destino.startswith('/') and not destino.startswith('//'):
        return destino
    return '/'

@app.route('/agregar/<int:id>')
def agregar(id):
    destino = _destino_despues_agregar()
    es_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
    try:
        usuario = obtener_usuario()
        cur = mysql.connection.cursor()
        cur.execute(f"SELECT stock, estado, nombre, precio FROM {ALMACEN_DB}.productos WHERE id=%s", (id,))
        prod = cur.fetchone()
        if not prod or prod['estado'] != 'activo':
            msg = 'Producto no disponible.'
            if es_ajax: return jsonify({'ok': False, 'mensaje': msg}), 400
            flash(msg, 'danger')
            return redirect(destino)
        stock_disponible = prod['stock']
        if stock_disponible <= 0:
            msg = 'Producto sin stock disponible.'
            if es_ajax: return jsonify({'ok': False, 'mensaje': msg}), 400
            flash(msg, 'danger')
            return redirect(destino)
        cur.execute("SELECT SUM(cantidad) AS total FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        en_carrito = cur.fetchone()['total'] or 0
        if en_carrito + 1 > stock_disponible:
            msg = f'Stock insuficiente. Solo hay {stock_disponible} unidad(es) disponible(s).'
            if es_ajax: return jsonify({'ok': False, 'mensaje': msg}), 400
            flash(msg, 'danger')
            return redirect(destino)
        cur.execute("SELECT * FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        item = cur.fetchone()
        if item:
            cur.execute("UPDATE carrito SET cantidad=cantidad+1 WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        else:
            cur.execute("INSERT INTO carrito (usuario_id, producto_id, cantidad) VALUES(%s,%s,1)", (usuario, id))
        mysql.connection.commit()
        _carrito_invalidar(usuario)

        # KPI: paso 1 del recorrido. Armar el carrito es el primer paso de la
        # intencion de compra; aqui nace la sesion que luego va avanzando.
        kpi_reg.sesion_compra(kpi_reg.PASO_CARRITO)

        # Datos para respuesta AJAX
        cur.execute("SELECT cantidad FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        fila = cur.fetchone()
        nueva_cant = fila['cantidad'] if fila else 1
        subtotal = round(nueva_cant * float(prod['precio']), 2)
        total = _total_carrito(usuario)
        cur.close()

        if es_ajax:
            return jsonify({
                'ok': True,
                'producto_id': id,
                'nombre': prod['nombre'],
                'precio': float(prod['precio']),
                'stock': stock_disponible,
                'cantidad': nueva_cant,
                'subtotal': subtotal,
                'total': total,
                'maximo': min(stock_disponible, 500),
            })

        flash('Producto agregado al carrito', 'success')
    except Exception as e:
        print(f"[agregar_carrito] {e}")
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        msg = 'Error al agregar al carrito.'
        if es_ajax: return jsonify({'ok': False, 'mensaje': msg}), 500
        flash(msg, 'danger')
    return redirect(destino)

# ─────────────────────────────────────────────
# INIT DB — Intento ÚNICO al arrancar (no en before_request)
# ─────────────────────────────────────────────
# Evita reintentos en cada request si la BD tarda en estar lista.
# Si falla, se logea y la app continúa; las tablas se crearán
# cuando se necesiten o vía comando manual.
_db_initialized = False

def _try_init_db_once():
    """Intenta inicializar la BD una sola vez al arrancar la app."""
    global _db_initialized
    if _db_initialized:
        return
    try:
        with app.app_context():
            cur = mysql.connection.cursor()
            cur.execute("SELECT 1")
            cur.close()
            init_db()
            _db_initialized = True
            app.logger.info('init_db completado al arranque')
    except Exception as e:
        app.logger.warning('init_db falló al arranque (se reintentará en primer request): %s', e)

# Ejecutar al importar el módulo (antes de que llegue el primer request)
_try_init_db_once()

# Fallback: si falló al arranque, reintentar en el primer request (solo una vez)
@app.before_request
def _ensure_db_fallback():
    global _db_initialized
    if _db_initialized:
        return
    try:
        cur = mysql.connection.cursor()
        cur.execute("SELECT 1")
        cur.close()
        init_db()
        _db_initialized = True
    except Exception as e:
        # Si falla aquí también, marcamos como inicializado para NO reintentar
        # en cada request. Las tablas se crearán cuando se acceda a ellas.
        _db_initialized = True
        app.logger.error('init_db falló definitivamente, continuando sin tablas: %s', e)

# ─────────────────────────────────────────────
# IMAGENES PERSISTENTES DE PRODUCTOS
# ─────────────────────────────────────────────
# El catalogo vive en static/imagenes/ y se versiona con cada app, asi que se
# sirve directo desde disco (rapido y cacheable). Las subidas nuevas van a
# static/uploads, un disco EFIMERO: en Render se borra al reiniciar/spin-down
# (por eso la imagen se veia unos minutos y luego desaparecia) y ademas esta
# en .gitignore. Para que persistan, al subir se guardan los bytes en la BD
# (productos.imagen_datos). Aqui se envuelve la vista estatica de Flask: si el
# archivo no esta en disco, se busca el blob en la BD y se devuelve. Asi las
# plantillas que usan url_for('static', filename=... ) siguen funcionando.
_static_original = app.view_functions.get('static')


def _servir_imagen_desde_db(filename):
    """Devuelve el blob guardado en la BD para la ruta relativa indicada."""
    try:
        cur = mysql.connection.cursor()
        cur.execute(
            f"SELECT imagen_datos, imagen_mime FROM {ALMACEN_DB}.productos "
            f"WHERE imagen=%s AND imagen_datos IS NOT NULL LIMIT 1",
            (filename,),
        )
        fila = cur.fetchone()
        if not fila or not fila['imagen_datos']:
            # Galeria: imagenes adicionales del producto (hasta 4 por producto).
            cur.execute(
                f"SELECT imagen_datos, imagen_mime FROM {ALMACEN_DB}.productos_imagenes "
                f"WHERE imagen=%s AND imagen_datos IS NOT NULL LIMIT 1",
                (filename,),
            )
            fila = cur.fetchone()
        cur.close()
    except Exception:
        return None
    if not fila or not fila['imagen_datos']:
        return None
    # Defensa en profundidad: si una fila antigua guardo un Content-Type que no
    # es de imagen, no se devuelve como pagina ejecutable.
    mime = (fila['imagen_mime'] or '').lower().split(';')[0].strip()
    if mime not in _MIMES_IMAGEN:
        mime = 'application/octet-stream'
    return Response(fila['imagen_datos'], mimetype=mime)


def _static_con_db(filename):
    try:
        return _static_original(filename=filename)
    except NotFound:
        if filename.startswith(('uploads/', 'imagenes/')):
            resp = _servir_imagen_desde_db(filename)
            if resp is not None:
                return resp
        raise


if _static_original is not None:
    app.view_functions['static'] = _static_con_db

# ─────────────────────────────────────────────
# KPIs DE NEGOCIO — VENTAS Y ALMACEN
# ─────────────────────────────────────────────
# El modulo registra su blueprint y el context processor
# que inyecta `kpis_biz` en /admin.
init_kpi_biz(app)

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
