import os, uuid, io, threading, secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, send_file)
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
import requests

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
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
    if not token or token != session.get('_csrf_token'):
        return False
    return True

UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────
# MYSQL (env vars para Render)
# ─────────────────────────────────────────────
app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', '')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

# Segunda base de datos (Gestión de Almacén): productos, proveedores y para pedir
ALMACEN_DB = os.environ.get('MYSQL_DB_ALMACEN', 'gestion_de_almacen')
MAIN_DB = os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard')

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

CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']

# ─────────────────────────────────────────────
# INIT TABLAS NUEVAS
# ─────────────────────────────────────────────
def init_db():
    global ALMACEN_DB
    try:
        cur = mysql.connection.cursor()

        # ── Base de datos principal: usuarios, ventas, carrito, etc.
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
        try:
            cur.execute("ALTER TABLE usuarios ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE usuarios ADD COLUMN recuperacion_token VARCHAR(255)")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE usuarios ADD COLUMN recuperacion_expira DATETIME")
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS carrito (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                usuario_id  VARCHAR(200) NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cur.execute("ALTER TABLE carrito ADD COLUMN cantidad INT DEFAULT 1")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE carrito DROP FOREIGN KEY carrito_ibfk_1")
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
        try:
            cur.execute("ALTER TABLE ventas ADD COLUMN documento VARCHAR(20)")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE ventas ADD COLUMN nombre VARCHAR(200)")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE ventas ADD COLUMN estado VARCHAR(20) DEFAULT 'en espera'")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE ventas ADD COLUMN metodo_pago VARCHAR(50)")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE ventas ADD COLUMN direccion_envio TEXT")
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
                intentos        INT DEFAULT 1,
                bloqueado_hasta DATETIME,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingresos (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                proveedor_id    INT,
                notas           TEXT,
                fecha           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS detalle_ingreso (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                ingreso_id  INT NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT NOT NULL,
                precio_compra DECIMAL(10,2) DEFAULT 0
            )
        """)
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
            CREATE TABLE IF NOT EXISTS salidas (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                venta_id    INT,
                notas       TEXT,
                fecha       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS detalle_salida (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                salida_id   INT NOT NULL,
                producto_id INT NOT NULL,
                cantidad    INT NOT NULL
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

        mysql.connection.commit()
        print("[init_db] Tablas de la BD principal creadas/verificadas OK")
    except Exception as e:
        print(f"[init_db] Error creando tablas principales: {e}")

    # ── Base de datos de Gestión de Almacén: productos, proveedores, para pedir
    try:
        cur = mysql.connection.cursor()
        try:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {ALMACEN_DB} CHARACTER SET utf8mb4")
        except Exception:
            print(f"[init_db] No se pudo crear BD '{ALMACEN_DB}', usando BD principal")
            ALMACEN_DB = MAIN_DB
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.productos (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                nombre      VARCHAR(200) NOT NULL,
                descripcion TEXT,
                precio      DECIMAL(10,2) NOT NULL,
                stock       INT DEFAULT 0,
                categoria   VARCHAR(100),
                imagen      VARCHAR(300),
                estado      VARCHAR(20) DEFAULT 'activo',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cur.execute(f"ALTER TABLE {ALMACEN_DB}.productos ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        except Exception:
            pass
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {ALMACEN_DB}.proveedores (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                nombre       VARCHAR(200) NOT NULL,
                celular      VARCHAR(30),
                correo       VARCHAR(200),
                dni          VARCHAR(20),
                ruc          VARCHAR(20),
                direccion    VARCHAR(300),
                categoria    VARCHAR(100),
                notas        TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        # Migración única: si la tabla de almacén está vacía pero existe en la BD
        # principal con datos, se copian para no perder nada.
        for tabla in ['productos', 'proveedores', 'productos_para_pedir']:
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
        cur.close()
        print(f"[init_db] Tablas de almacén en '{ALMACEN_DB}' creadas/verificadas OK")
    except Exception as e:
        print(f"[init_db] Error en tablas de almacén: {e}")

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

def generar_boleta_pdf(venta_id):
    """Genera PDF en memoria y lo devuelve como bytes."""
    cur = mysql.connection.cursor()
    cur.execute("SELECT documento, nombre, total, fecha FROM ventas WHERE id=%s", (venta_id,))
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
    elems.append(Paragraph("RUC: 20123456789", styles['Normal']))
    elems.append(Spacer(1, 0.3*inch))
    elems.append(Paragraph(f"<b>Boleta N°:</b> {venta_id}", styles['Normal']))
    elems.append(Paragraph(f"<b>Cliente:</b> {venta.get('nombre') or 'Sin nombre'}", styles['Normal']))
    elems.append(Paragraph(f"<b>DNI/RUC:</b> {venta.get('documento') or '-'}", styles['Normal']))
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
    """Envía boleta PDF al correo del cliente."""
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
        mail.send(msg)
        return True
    except Exception as e:
        print(f"[email_boleta] {e}")
        return False

def enviar_email_proveedor(proveedor, productos_lista):
    """Envía email al proveedor con la lista de productos a pedir."""
    try:
        if not proveedor.get('correo'):
            return False
        lineas = "\n".join([
            f"- {p['nombre']} (Categoría: {p.get('categoria','')}, Qty pedido: {p.get('cantidad_pedido',1)})"
            for p in productos_lista
        ])
        msg = Message(
            subject='Pedido de reabastecimiento - Multiservicios Richard',
            recipients=[proveedor['correo']],
            body=(
                f"Estimado/a {proveedor['nombre']},\n\n"
                f"Le informamos que los siguientes productos necesitan reabastecimiento:\n\n"
                f"{lineas}\n\n"
                f"Por favor contáctenos para coordinar la entrega.\n\n"
                f"Multiservicios Richard"
            )
        )
        mail.send(msg)
        return True
    except Exception as e:
        print(f"[email_proveedor] {e}")
        return False

def verificar_stock_bajo(cur, producto_id):
    """Si stock <= 1 auto-agrega a productos_para_pedir y notifica al proveedor.
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
            SELECT id, nombre, celular, correo
            FROM {ALMACEN_DB}.proveedores WHERE categoria=%s LIMIT 1
        """, (p['categoria'],))
        proveedor = cur.fetchone()
        proveedor_id = proveedor['id'] if proveedor else None

        cur.execute(f"""
            INSERT INTO {ALMACEN_DB}.productos_para_pedir (producto_id, cantidad_pedido, proveedor_id)
            VALUES (%s, 1, %s)
        """, (producto_id, proveedor_id))

        # Email al proveedor (en segundo plano para no bloquear la compra)
        if proveedor and proveedor.get('correo'):
            proveedor_copy = dict(proveedor)
            productos_info = [{
                'nombre': p['nombre'],
                'categoria': p['categoria'],
                'cantidad_pedido': 1
            }]
            def _enviar_proveedor():
                with app.app_context():
                    enviar_email_proveedor(proveedor_copy, productos_info)
            threading.Thread(target=_enviar_proveedor, daemon=True).start()
    except Exception as e:
        print(f"[stock_bajo] {e}")

def whatsapp_url(celular, mensaje):
    """Genera URL de WhatsApp con mensaje prefill."""
    numero = ''.join(filter(str.isdigit, celular or ''))
    if not numero.startswith('51'):
        numero = '51' + numero
    from urllib.parse import quote
    return f"https://wa.me/{numero}?text={quote(mensaje)}"

# ─────────────────────────────────────────────
# CONTEXT PROCESSOR
# ─────────────────────────────────────────────
@app.context_processor
def cantidad_carrito():
    try:
        usuario = obtener_usuario()
        cur = mysql.connection.cursor()
        cur.execute("SELECT SUM(cantidad) AS total FROM carrito WHERE usuario_id=%s", (usuario,))
        res = cur.fetchone()
        return dict(cantidad_carrito=res['total'] if res['total'] else 0)
    except:
        return dict(cantidad_carrito=0)

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
@app.route('/login', methods=['GET','POST'])
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
                flash(f"IP bloqueada. Intenta en {restante.seconds//60}m {restante.seconds%60}s", 'danger')
                return redirect('/login')
            else:
                cur.execute("DELETE FROM bloqueos_ip WHERE ip=%s", (ip,))
                mysql.connection.commit()

        cur.execute("SELECT * FROM intentos_usuario WHERE correo=%s", (correo,))
        bloqueo_usuario = cur.fetchone()
        if bloqueo_usuario and bloqueo_usuario['bloqueado_hasta']:
            ahora = datetime.now()
            if ahora < bloqueo_usuario['bloqueado_hasta']:
                restante = bloqueo_usuario['bloqueado_hasta'] - ahora
                flash(f"Usuario bloqueado. Intenta en {restante.seconds//60}m {restante.seconds%60}s", 'danger')
                return redirect('/login')
            else:
                cur.execute("DELETE FROM intentos_usuario WHERE correo=%s", (correo,))
                mysql.connection.commit()
                bloqueo_usuario = None

        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        usuario = cur.fetchone()

        if usuario and bcrypt.check_password_hash(usuario['password'], password):
            cur.execute("DELETE FROM intentos_usuario WHERE correo=%s", (correo,))
            mysql.connection.commit()
            session['user_id'] = usuario['id']
            session['correo']   = usuario['correo']
            session['rol']      = usuario['rol'].lower()
            flash('Bienvenido', 'success')
            if session['rol'] in ['admin','administrador']:
                init_db()
                return redirect('/dashboard')
            return redirect('/')

        if bloqueo_usuario:
            intentos  = bloqueo_usuario['intentos'] + 1
            restantes = 3 - intentos
            if intentos >= 3:
                bloqueo_hasta = datetime.now() + timedelta(minutes=5)
                cur.execute("UPDATE intentos_usuario SET intentos=%s, bloqueado_hasta=%s WHERE correo=%s",
                            (intentos, bloqueo_hasta, correo))
                flash('Usuario bloqueado por 5 minutos.', 'danger')
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
                cur.execute("UPDATE intentos_usuario SET intentos=%s WHERE correo=%s", (intentos, correo))
                flash(f'Credenciales incorrectas. Te quedan {restantes} intento(s).', 'warning')
        else:
            cur.execute("INSERT INTO intentos_usuario(correo, intentos) VALUES(%s,1)", (correo,))
            flash('Credenciales incorrectas. Te quedan 2 intento(s).', 'warning')

        mysql.connection.commit()
        return redirect('/login')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/registro', methods=['GET','POST'])
def registro():
    if request.method == 'POST':
        correo   = request.form['correo']
        password = request.form['password']
        confirmar = request.form['confirmar']
        if password != confirmar:
            flash('Las contraseñas no coinciden.', 'danger')
            return redirect('/registro')
        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        if cur.fetchone():
            flash('Ya existe una cuenta con ese correo.', 'danger')
            return redirect('/registro')
        h = bcrypt.generate_password_hash(password).decode('utf-8')
        cur.execute("INSERT INTO usuarios (correo, password, rol) VALUES (%s,%s,'cliente')", (correo, h))
        mysql.connection.commit()
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

    cur.execute("SELECT COUNT(*) AS total FROM ventas")
    total_ventas = cur.fetchone()['total'] or 0

    cur.execute("SELECT COALESCE(SUM(total), 0) AS total FROM ventas")
    ingresos_totales = float(cur.fetchone()['total'] or 0)

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
                           productos_activos=productos_activos,
                           stock_bajo=stock_bajo,
                           total_usuarios=total_usuarios,
                           pedidos_pendientes=pedidos_pendientes,
                           total_proveedores=total_proveedores,
                           ventas_pendientes=ventas_pendientes,
                           ultimas_ventas=ultimas_ventas,
                           productos_stock_bajo=productos_stock_bajo)

@app.route('/admin')
def admin():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return "Acceso denegado"
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT * FROM {ALMACEN_DB}.productos")
    productos = cur.fetchall()

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.productos_para_pedir WHERE estado='pendiente'")
    r = cur.fetchone()
    pedidos_pendientes = r['total'] if r else 0

    cur.execute(f"SELECT COUNT(*) AS total FROM {ALMACEN_DB}.proveedores")
    r2 = cur.fetchone()
    total_proveedores = r2['total'] if r2 else 0

    return render_template('admin.html',
                           productos=productos,
                           pedidos_pendientes=pedidos_pendientes,
                           total_proveedores=total_proveedores)

# ─────────────────────────────────────────────
# PRODUCTOS CRUD
# ─────────────────────────────────────────────
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
    imagen = request.files.get('imagen')
    imagen_db = None
    if imagen and imagen.filename:
        fn  = secure_filename(imagen.filename)
        imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
        imagen_db = 'uploads/' + fn

    cur = mysql.connection.cursor()
    cur.execute(f"""
        INSERT INTO {ALMACEN_DB}.productos (nombre, descripcion, precio, stock, categoria, imagen)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (nombre, descripcion, precio, stock, categoria, imagen_db))
    mysql.connection.commit()
    nuevo_id = cur.lastrowid
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
        imagen      = request.files.get('imagen')

        if precio < 0:
            flash('No se permiten precios negativos.', 'danger')
            return redirect(f'/editar_producto/{id}')
        if stock < 0:
            flash('No se permiten valores negativos en el stock.', 'danger')
            return redirect(f'/editar_producto/{id}')

        if imagen and imagen.filename:
            fn = secure_filename(imagen.filename)
            imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
            cur.execute(f"""
                UPDATE {ALMACEN_DB}.productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s, imagen=%s WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, 'uploads/'+fn, id))
        else:
            cur.execute(f"""
                UPDATE {ALMACEN_DB}.productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, id))

        mysql.connection.commit()
        verificar_stock_bajo(cur, id)
        mysql.connection.commit()
        cur.close()
        flash('Producto actualizado correctamente.', 'success')
        return redirect('/admin')

    cur.execute(f"SELECT * FROM {ALMACEN_DB}.productos WHERE id=%s", (id,))
    producto = cur.fetchone()
    return render_template('editar_producto.html', producto=producto, categorias=CATEGORIAS)

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
        data = requests.get(url).json()
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
# TIENDA / CARRITO
# ─────────────────────────────────────────────
@app.route('/')
def index():
    buscar   = request.args.get('buscar','')
    categoria = request.args.get('categoria','Todos')
    cur = mysql.connection.cursor()
    sql = f"SELECT * FROM {ALMACEN_DB}.productos WHERE nombre LIKE %s AND estado='activo'"
    vals = [f'%{buscar}%']
    if categoria != 'Todos':
        sql += ' AND categoria=%s'
        vals.append(categoria)
    cur.execute(sql, tuple(vals))
    productos = cur.fetchall()
    return render_template('index.html', productos=productos, categorias=CATEGORIAS)

@app.route('/carrito')
def ver_carrito():
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute(f"""
        SELECT c.id, c.producto_id, p.nombre, p.precio, c.cantidad
        FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id
        WHERE c.usuario_id=%s
    """, (usuario,))
    productos = cur.fetchall()
    total = sum(p['precio'] * p['cantidad'] for p in productos)
    return render_template('carrito.html', productos=productos, total=total)


@app.route('/actualizar-cantidad/<int:id_producto>', methods=['POST'])
def actualizar_cantidad(id_producto):
    usuario = obtener_usuario()
    if not request.is_json:
        return jsonify({'error': 'Request debe ser JSON'}), 400
    accion  = request.json.get('accion')
    if accion not in ('aumentar', 'reducir'):
        return jsonify({'error': 'Acción inválida'}), 400
    cur = mysql.connection.cursor()
    if accion == 'aumentar':
        cur.execute("UPDATE carrito SET cantidad=cantidad+1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
    elif accion == 'reducir':
        cur.execute("UPDATE carrito SET cantidad=cantidad-1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
        cur.execute("DELETE FROM carrito WHERE producto_id=%s AND usuario_id=%s AND cantidad<=0", (id_producto, usuario))
    mysql.connection.commit()
    cur.execute(f"SELECT c.cantidad, p.precio FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id WHERE c.producto_id=%s AND c.usuario_id=%s", (id_producto, usuario))
    fila = cur.fetchone()
    cur.execute(f"SELECT SUM(c.cantidad*p.precio) AS total FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id WHERE c.usuario_id=%s", (usuario,))
    res = cur.fetchone()
    total = res['total'] if res['total'] else 0
    cur.close()
    if fila:
        return jsonify({'eliminado':False,'cantidad':fila['cantidad'],'subtotal':round(fila['cantidad']*float(fila['precio']),2),'total':round(float(total),2)})
    return jsonify({'eliminado':True,'total':round(float(total),2)})


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
    if 'user_id' not in session and ('rol' not in session or session['rol'] not in ['admin','administrador']):
        return redirect('/login')
    return render_template('boleta_form.html', venta_id=venta_id)

@app.route('/preview_boleta')
def preview_boleta():
    if 'user_id' not in session and ('rol' not in session or session['rol'] not in ['admin','administrador']):
        return redirect('/login')
    venta_id = request.args.get('venta_id')
    doc      = request.args.get('doc')
    nombre   = request.args.get('nombre')
    cur = mysql.connection.cursor()
    cur.execute(f"""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """, (venta_id,))
    productos = cur.fetchall()
    total = sum(p['cantidad'] * p['precio'] for p in productos)
    return render_template('preview_boleta.html', productos=productos, total=total,
                           doc=doc, nombre=nombre, venta_id=venta_id)

# ─────────────────────────────────────────────
# HISTORIAL
# ─────────────────────────────────────────────
@app.route('/historial')
def historial():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, total, fecha FROM ventas WHERE cliente_id=%s ORDER BY fecha DESC", (user_id,))
    ventas = cur.fetchall()
    hist = []
    for v in ventas:
        cur.execute(f"""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['id'],))
        hist.append({'id':v['id'],'total':v['total'],'fecha':v['fecha'],'productos':cur.fetchall()})
    cur.close()
    return render_template('historial.html', historial=hist)

@app.route('/historial-compras')
def historial_compras():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    buscar = request.args.get('buscar','')
    cur = mysql.connection.cursor()
    cur.execute("SELECT SUM(total) AS gran_total FROM ventas")
    res = cur.fetchone()
    gran_total = res['gran_total'] or 0
    cur.execute("""
        SELECT v.id, u.correo, u.id AS cliente_id, v.total, v.fecha, v.documento, v.nombre AS titular, v.estado
        FROM ventas v JOIN usuarios u ON v.cliente_id=u.id
        WHERE u.correo LIKE %s ORDER BY v.fecha DESC
    """, (f'%{buscar}%',))
    ventas_raw = cur.fetchall()
    historial = []
    for v in ventas_raw:
        cur.execute(f"""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN {ALMACEN_DB}.productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['id'],))
        historial.append({**v, 'productos': cur.fetchall()})
    cur.close()
    return render_template('historial_compras_admin.html', historial=historial, buscar=buscar, gran_total=gran_total)

@app.route('/historial-compras/estado/<int:venta_id>', methods=['POST'])
def actualizar_estado_venta(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    estado = request.form.get('estado', 'en espera')
    if estado not in ['entregado', 'en espera', 'cancelado']:
        estado = 'en espera'
    cur = mysql.connection.cursor()
    cur.execute("UPDATE ventas SET estado=%s WHERE id=%s", (estado, venta_id))
    mysql.connection.commit()
    cur.close()
    flash(f'Venta #{venta_id} actualizada a "{estado}".', 'success')
    return redirect('/historial-compras')

@app.route('/historial-compras/eliminar/<int:venta_id>')
def eliminar_venta(venta_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM detalle_venta WHERE venta_id=%s", (venta_id,))
    cur.execute("DELETE FROM ventas WHERE id=%s", (venta_id,))
    mysql.connection.commit()
    cur.close()
    flash(f'Venta #{venta_id} eliminada.', 'success')
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
    cur.execute("""
        SELECT id, correo, rol, estado FROM usuarios WHERE correo LIKE %s
        ORDER BY CASE rol WHEN 'admin' THEN 0 WHEN 'administrador' THEN 1 ELSE 2 END, correo ASC
    """, (f'%{buscar}%',))
    usuarios = cur.fetchall()
    cur.close()
    return render_template('permisos.html', usuarios=usuarios, buscar=buscar,
                           es_superadmin=es_superadmin)

# ─────────────────────────────────────────────
# PROVEEDORES
# ─────────────────────────────────────────────
@app.route('/proveedores', methods=['GET','POST'])
def proveedores():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/proveedores')
        nombre    = request.form.get('nombre','')
        celular   = request.form.get('celular','')
        correo    = request.form.get('correo','')
        dni       = request.form.get('dni','')
        ruc       = request.form.get('ruc','')
        direccion = request.form.get('direccion','')
        categoria = request.form.get('categoria','')
        notas     = request.form.get('notas','')
        cur.execute(f"""
            INSERT INTO {ALMACEN_DB}.proveedores (nombre, celular, correo, dni, ruc, direccion, categoria, notas)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (nombre, celular, correo, dni, ruc, direccion, categoria, notas))
        mysql.connection.commit()
        flash(f'Proveedor "{nombre}" agregado correctamente.', 'success')
        return redirect('/proveedores')

    buscar = request.args.get('buscar','')
    cat_filtro = request.args.get('categoria','')
    sql = f"SELECT * FROM {ALMACEN_DB}.proveedores WHERE nombre LIKE %s"
    vals = [f'%{buscar}%']
    if cat_filtro:
        sql += " AND categoria=%s"
        vals.append(cat_filtro)
    sql += " ORDER BY nombre ASC"
    cur.execute(sql, tuple(vals))
    lista = cur.fetchall()
    cur.close()
    return render_template('proveedores.html', proveedores=lista,
                           categorias=CATEGORIAS, buscar=buscar, cat_filtro=cat_filtro)

@app.route('/proveedores/editar/<int:id>', methods=['GET','POST'])
def editar_proveedor(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/proveedores/editar/{id}')
        cur.execute(f"""
            UPDATE {ALMACEN_DB}.proveedores SET nombre=%s, celular=%s, correo=%s, dni=%s,
            ruc=%s, direccion=%s, categoria=%s, notas=%s WHERE id=%s
        """, (
            request.form.get('nombre'), request.form.get('celular'),
            request.form.get('correo'), request.form.get('dni'),
            request.form.get('ruc'),    request.form.get('direccion'),
            request.form.get('categoria'), request.form.get('notas'), id
        ))
        mysql.connection.commit()
        flash('Proveedor actualizado.', 'success')
        return redirect('/proveedores')
    cur.execute(f"SELECT * FROM {ALMACEN_DB}.proveedores WHERE id=%s", (id,))
    p = cur.fetchone()
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
               pr.celular AS proveedor_celular, pr.correo AS proveedor_correo
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
                'proveedor_correo': ped['proveedor_correo'] or '',
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

@app.route('/productos-para-pedir/enviar-email/<int:proveedor_id>')
def enviar_email_a_proveedor(proveedor_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT * FROM {ALMACEN_DB}.proveedores WHERE id=%s", (proveedor_id,))
    proveedor = cur.fetchone()
    if not proveedor:
        flash('Proveedor no encontrado.', 'danger')
        return redirect('/productos-para-pedir')
    cur.execute(f"""
        SELECT p.nombre, p.categoria, pp.cantidad_pedido
        FROM {ALMACEN_DB}.productos_para_pedir pp
        JOIN {ALMACEN_DB}.productos p ON pp.producto_id=p.id
        WHERE pp.proveedor_id=%s AND pp.estado='pendiente'
    """, (proveedor_id,))
    productos_lista = cur.fetchall()
    cur.close()
    if not productos_lista:
        flash('No hay productos pendientes para este proveedor.', 'warning')
        return redirect('/productos-para-pedir')
    ok = enviar_email_proveedor(proveedor, productos_lista)
    if ok:
        flash(f'Email enviado a {proveedor["correo"]}', 'success')
    else:
        flash('Error al enviar email. Verifica la configuración SMTP.', 'danger')
    return redirect('/productos-para-pedir')

# ─────────────────────────────────────────────
# PRO14: INGRESOS DE PRODUCTOS (Comprobante de ingreso)
# ─────────────────────────────────────────────
@app.route('/ingresos')
def ingresos():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT i.id, i.fecha, i.notas,
               p.nombre AS proveedor_nombre,
               (SELECT SUM(di.cantidad) FROM detalle_ingreso di WHERE di.ingreso_id=i.id) AS total_items
        FROM ingresos i
        LEFT JOIN {0}.proveedores p ON i.proveedor_id=p.id
        ORDER BY i.fecha DESC
    """.format(ALMACEN_DB))
    lista = cur.fetchall()
    cur.close()
    return render_template('ingresos.html', ingresos=lista)

@app.route('/registrar-ingreso', methods=['GET','POST'])
def registrar_ingreso():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT id, nombre, stock, precio FROM {ALMACEN_DB}.productos WHERE estado='activo' ORDER BY nombre")
    productos = cur.fetchall()
    cur.execute(f"SELECT id, nombre FROM {ALMACEN_DB}.proveedores ORDER BY nombre")
    proveedores = cur.fetchall()
    cur.close()

    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect('/registrar-ingreso')
        proveedor_id = request.form.get('proveedor_id') or None
        notas = request.form.get('notas', '')
        producto_ids = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')
        precios = request.form.getlist('precio_compra[]')

        cur = mysql.connection.cursor()
        cur.execute("INSERT INTO ingresos (proveedor_id, notas) VALUES (%s, %s)", (proveedor_id, notas))
        ingreso_id = cur.lastrowid

        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i])
                prec = float(precios[i]) if precios[i] else 0
            except (ValueError, IndexError):
                continue
            if cant > 0:
                cur.execute("INSERT INTO detalle_ingreso (ingreso_id, producto_id, cantidad, precio_compra) VALUES (%s,%s,%s,%s)",
                            (ingreso_id, pid, cant, prec))
                cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock+%s WHERE id=%s", (cant, pid))

        mysql.connection.commit()
        cur.close()
        flash(f'Ingreso #{ingreso_id} registrado correctamente.', 'success')
        return redirect(f'/comprobante-ingreso/{ingreso_id}')

    return render_template('registrar_ingreso.html', productos=productos, proveedores=proveedores)

@app.route('/comprobante-ingreso/<int:id>')
def comprobante_ingreso(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT i.id, i.fecha, i.notas,
               p.nombre AS proveedor_nombre, p.ruc AS proveedor_ruc,
               p.celular AS proveedor_celular, p.correo AS proveedor_correo
        FROM ingresos i
        LEFT JOIN {0}.proveedores p ON i.proveedor_id=p.id
        WHERE i.id=%s
    """.format(ALMACEN_DB), (id,))
    ingreso = cur.fetchone()
    if not ingreso:
        flash('Ingreso no encontrado.', 'danger')
        return redirect('/ingresos')
    cur.execute("""
        SELECT di.cantidad, di.precio_compra,
               pr.nombre AS producto_nombre, pr.stock AS stock_actual
        FROM detalle_ingreso di
        JOIN {0}.productos pr ON di.producto_id=pr.id
        WHERE di.ingreso_id=%s
    """.format(ALMACEN_DB), (id,))
    items = cur.fetchall()
    total_general = sum(i['cantidad'] * float(i['precio_compra']) for i in items)
    cur.close()
    return render_template('comprobante_ingreso.html', ingreso=ingreso, items=items, total_general=total_general)

@app.route('/eliminar-ingreso/<int:id>')
def eliminar_ingreso(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT producto_id, cantidad FROM detalle_ingreso WHERE ingreso_id=%s", (id,))
    items = cur.fetchall()
    revertidos = 0
    for item in items:
        cur.execute(f"SELECT stock FROM {ALMACEN_DB}.productos WHERE id=%s", (item['producto_id'],))
        prod = cur.fetchone()
        stock_actual = prod['stock'] if prod else 0
        if stock_actual >= item['cantidad']:
            cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock-%s WHERE id=%s", (item['cantidad'], item['producto_id']))
            revertidos += 1
        else:
            cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=0 WHERE id=%s", (item['producto_id'],))
            revertidos += 1
    cur.execute("DELETE FROM detalle_ingreso WHERE ingreso_id=%s", (id,))
    cur.execute("DELETE FROM ingresos WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash(f'Ingreso eliminado y stock revertido ({revertidos} productos).', 'success')
    return redirect('/ingresos')

# ─────────────────────────────────────────────
# PRO15-19: SEGUIMIENTO DE ENTREGAS
# ─────────────────────────────────────────────
@app.route('/entregas')
def entregas():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    filtro = request.args.get('filtro', 'todas')
    cur = mysql.connection.cursor()
    sql = """
        SELECT e.id, e.venta_id, e.estado, e.direccion_envio, e.fecha_estimada,
               e.fecha_entrega, e.notas, e.created_at,
               v.total, v.nombre AS cliente_nombre, v.documento AS cliente_doc,
               u.correo AS cliente_correo
        FROM seguimiento_entregas e
        JOIN ventas v ON e.venta_id=v.id
        LEFT JOIN usuarios u ON v.cliente_id=u.id
    """
    if filtro == 'pendientes':
        sql += " WHERE e.estado='pendiente'"
    elif filtro == 'en_camino':
        sql += " WHERE e.estado='en camino'"
    elif filtro == 'entregado':
        sql += " WHERE e.estado='entregado'"
    sql += " ORDER BY e.created_at DESC"
    cur.execute(sql)
    lista = cur.fetchall()
    cur.close()
    return render_template('entregas.html', entregas=lista, filtro=filtro)

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
    cur.close()
    flash(f'Entrega para venta #{venta_id} creada.', 'success')
    return redirect('/entregas')

@app.route('/entregas/actualizar/<int:id>', methods=['POST'])
def actualizar_entrega(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    nuevo_estado = request.form.get('estado', 'pendiente')
    notas = request.form.get('notas', '')
    cur = mysql.connection.cursor()
    if nuevo_estado == 'entregado':
        cur.execute("UPDATE seguimiento_entregas SET estado=%s, notas=%s, fecha_entrega=NOW() WHERE id=%s",
                    (nuevo_estado, notas, id))
    else:
        cur.execute("UPDATE seguimiento_entregas SET estado=%s, notas=%s WHERE id=%s",
                    (nuevo_estado, notas, id))
    mysql.connection.commit()
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
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT s.id, s.fecha, s.notas,
               v.id AS venta_id, v.nombre AS cliente_nombre, v.total AS venta_total,
               (SELECT SUM(ds.cantidad) FROM detalle_salida ds WHERE ds.salida_id=s.id) AS total_items
        FROM salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        ORDER BY s.fecha DESC
    """)
    lista = cur.fetchall()
    cur.close()
    return render_template('salidas.html', salidas=lista)

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

        cur.execute("INSERT INTO salidas (venta_id, notas) VALUES (%s, %s)", (venta_id, notas))
        salida_id = cur.lastrowid

        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i])
            except (ValueError, IndexError):
                continue
            if cant > 0:
                cur.execute("INSERT INTO detalle_salida (salida_id, producto_id, cantidad) VALUES (%s,%s,%s)",
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
        FROM salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE s.id=%s
    """, (id,))
    salida = cur.fetchone()
    if not salida:
        flash('Salida no encontrada.', 'danger')
        return redirect('/salidas')
    cur.execute("""
        SELECT ds.cantidad, pr.nombre AS producto_nombre, pr.stock AS stock_actual
        FROM detalle_salida ds
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
    cur.execute("SELECT producto_id, cantidad FROM detalle_salida WHERE salida_id=%s", (id,))
    items = cur.fetchall()
    for item in items:
        cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock+%s WHERE id=%s", (item['cantidad'], item['producto_id']))
    cur.execute("DELETE FROM detalle_salida WHERE salida_id=%s", (id,))
    cur.execute("DELETE FROM salidas WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Salida eliminada y stock revertido.', 'success')
    return redirect('/salidas')

# ─────────────────────────────────────────────
# ERROR HANDLER (loguea el error exacto)
# ─────────────────────────────────────────────
import logging
from werkzeug.exceptions import HTTPException
logging.basicConfig(level=logging.ERROR)

@app.errorhandler(Exception)
def manejar_error(e):
    if isinstance(e, HTTPException):
        return e
    try:
        app.logger.error('Error no controlado', exc_info=e)
    except Exception:
        pass
    return "Ocurrió un error interno. Revisa los logs.", 500

# ─────────────────────────────────────────────
# PRO08: VERIFICAR INVENTARIO POR CANTIDAD
# ─────────────────────────────────────────────
@app.route('/verificar-inventario')
def verificar_inventario():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    filtro = request.args.get('filtro', 'todos')
    cur = mysql.connection.cursor()
    sql = f"SELECT id, nombre, stock, precio, categoria, imagen, estado FROM {ALMACEN_DB}.productos WHERE 1=1"
    if filtro == 'sin_stock':
        sql += " AND stock=0 AND estado='activo'"
    elif filtro == 'critico':
        sql += " AND stock BETWEEN 1 AND 3 AND estado='activo'"
    elif filtro == 'bajo':
        sql += " AND stock BETWEEN 4 AND 10 AND estado='activo'"
    elif filtro == 'normal':
        sql += " AND stock > 10 AND estado='activo'"
    elif filtro == 'inactivos':
        sql += " AND estado='inactivo'"
    sql += " ORDER BY stock ASC"
    cur.execute(sql)
    productos = cur.fetchall()
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
                           productos=productos, filtro=filtro,
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
    cur.execute("SELECT id, direccion, distrito, referencia FROM direcciones WHERE usuario_id=%s", (venta['documento'] or ''))
    direcciones = cur.fetchall()

    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/entregas/asignar/{venta_id}')
        direccion = request.form.get('direccion_envio', '')
        fecha_est = request.form.get('fecha_estimada') or None
        notas = request.form.get('notas', '')
        cur.execute("INSERT INTO seguimiento_entregas (venta_id, estado, direccion_envio, fecha_estimada, notas) VALUES (%s,'pendiente',%s,%s,%s)",
                    (venta_id, direccion, fecha_est, notas))
        entrega_id = cur.lastrowid
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
        cur.close()
        flash(f'Entrega #{entrega_id} creada para venta #{venta_id}.', 'success')
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
    cur.execute("SELECT COUNT(*) AS n FROM ingresos")
    stats['total_ingresos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_ingreso")
    stats['total_detalle_ingresos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM salidas")
    stats['total_salidas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_salida")
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
        cur.execute("SELECT i.id, i.fecha, p.nombre AS proveedor_nombre, (SELECT SUM(cantidad) FROM detalle_ingreso WHERE ingreso_id=i.id) AS items FROM ingresos i LEFT JOIN proveedores p ON i.proveedor_id=p.id ORDER BY i.fecha DESC LIMIT 50")
        datos = cur.fetchall()
    elif seccion == 'salidas':
        cur.execute("SELECT s.id, s.fecha, v.nombre AS cliente_nombre, (SELECT SUM(cantidad) FROM detalle_salida WHERE salida_id=s.id) AS items FROM salidas s LEFT JOIN ventas v ON s.venta_id=v.id ORDER BY s.fecha DESC LIMIT 50")
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
    cur.execute("SELECT COALESCE(SUM(total),0) AS total FROM ventas WHERE DATE(fecha)=%s", (fecha_str,))
    ingresos_ventas = float(cur.fetchone()['total'])
    cur.execute("SELECT COUNT(*) AS n FROM ventas WHERE DATE(fecha)=%s", (fecha_str,))
    num_ventas = cur.fetchone()['n']

    cur.execute("""
        SELECT i.id, i.fecha, p.nombre AS proveedor_nombre,
               (SELECT SUM(di.cantidad) FROM detalle_ingreso di WHERE di.ingreso_id=i.id) AS items,
               (SELECT COALESCE(SUM(di.cantidad*di.precio_compra),0) FROM detalle_ingreso di WHERE di.ingreso_id=i.id) AS costo
        FROM ingresos i LEFT JOIN proveedores p ON i.proveedor_id=p.id
        WHERE DATE(i.fecha)=%s ORDER BY i.fecha
    """, (fecha_str,))
    ingresos_dia = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS n FROM ingresos WHERE DATE(fecha)=%s", (fecha_str,))
    num_ingresos = cur.fetchone()['n']

    cur.execute("""
        SELECT s.id, s.fecha, v.nombre AS cliente_nombre,
               (SELECT SUM(ds.cantidad) FROM detalle_salida ds WHERE ds.salida_id=s.id) AS items
        FROM salidas s LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE DATE(s.fecha)=%s ORDER BY s.fecha
    """, (fecha_str,))
    salidas_dia = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS n FROM salidas WHERE DATE(fecha)=%s", (fecha_str,))
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
    cur.execute(f"SELECT * FROM {ALMACEN_DB}.productos WHERE id=%s AND estado='activo'", (id,))
    producto = cur.fetchone()
    cur.close()
    if not producto:
        flash('Producto no encontrado.', 'danger')
        return redirect('/')
    return render_template('producto_detalle.html', producto=producto)

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
@app.route('/checkout')
def checkout():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = int(session['user_id'])
    cur = mysql.connection.cursor()
    if 'guest_id' in session:
        cur.execute("UPDATE carrito SET usuario_id=%s WHERE usuario_id=%s", (str(user_id), session['guest_id']))
        mysql.connection.commit()
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
    cur.execute("SELECT correo FROM usuarios WHERE id=%s", (user_id,))
    usuario = cur.fetchone()
    cur.close()
    return render_template('checkout.html', items=items, total=total,
                           direcciones=direcciones, usuario=usuario)

@app.route('/procesar_compra', methods=['POST'])
def procesar_compra():
    if 'user_id' not in session:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF inválido.', 'danger')
        return redirect('/checkout')
    user_id = int(session['user_id'])
    metodo_pago = request.form.get('metodo_pago', 'efectivo')
    direccion_envio = request.form.get('direccion_envio', '')
    cur = mysql.connection.cursor()
    try:
        cur.execute(f"""
            SELECT p.id, p.nombre, p.precio, c.cantidad
            FROM carrito c JOIN {ALMACEN_DB}.productos p ON c.producto_id=p.id
            WHERE c.usuario_id=%s
        """, (user_id,))
        items = cur.fetchall()
        if not items:
            return redirect('/carrito')
        total = 0
        for item in items:
            cur.execute(f"SELECT stock FROM {ALMACEN_DB}.productos WHERE id=%s", (item['id'],))
            row = cur.fetchone()
            stock = row['stock'] if row else 0
            if stock < item['cantidad']:
                flash(f'Stock insuficiente para: {item["nombre"]}. Solo quedan {stock} unidad(es).', 'danger')
                return redirect('/carrito')
            total += item['precio'] * item['cantidad']
        cur.execute("INSERT INTO ventas (cliente_id, total, metodo_pago, direccion_envio) VALUES(%s,%s,%s,%s)",
                    (user_id, total, metodo_pago, direccion_envio))
        venta_id = cur.lastrowid
        for item in items:
            cur.execute("INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio) VALUES(%s,%s,%s,%s)",
                        (venta_id, item['id'], item['cantidad'], item['precio']))
            cur.execute(f"UPDATE {ALMACEN_DB}.productos SET stock=stock-%s WHERE id=%s", (item['cantidad'], item['id']))
            verificar_stock_bajo(cur, item['id'])
        cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (user_id,))
        mysql.connection.commit()
        return redirect(f'/confirmacion/{venta_id}')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[procesar_compra] {e}")
        flash('Error al procesar la compra.', 'danger')
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
    if not venta:
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
        nuevo_correo = request.form.get('correo', '').strip()
        if nuevo_correo and nuevo_correo != session.get('correo'):
            cur.execute("SELECT id FROM usuarios WHERE correo=%s AND id!=%s", (nuevo_correo, user_id))
            if cur.fetchone():
                flash('Ese correo ya está en uso.', 'danger')
            else:
                cur.execute("UPDATE usuarios SET correo=%s WHERE id=%s", (nuevo_correo, user_id))
                mysql.connection.commit()
                session['correo'] = nuevo_correo
                flash('Correo actualizado.', 'success')
        cur.close()
        return redirect('/perfil')
    cur.execute("SELECT id, correo, rol, estado, created_at FROM usuarios WHERE id=%s", (user_id,))
    usuario = cur.fetchone()
    cur.close()
    return render_template('perfil.html', usuario=usuario)

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
    if len(nueva) < 6:
        flash('La nueva contraseña debe tener al menos 6 caracteres.', 'danger')
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
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT e.id, e.estado, e.direccion_envio, e.fecha_estimada, e.fecha_entrega, e.notas,
               v.id AS venta_id, v.total, v.fecha AS venta_fecha
        FROM seguimiento_entregas e
        JOIN ventas v ON e.venta_id=v.id
        WHERE v.cliente_id=%s
        ORDER BY e.created_at DESC
    """, (user_id,))
    entregas = cur.fetchall()
    cur.close()
    return render_template('mis_entregas.html', entregas=entregas)

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
        if len(nueva) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'danger')
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
    if 'user_id' not in session and ('rol' not in session or session['rol'] not in ['admin','administrador']):
        return redirect('/login')
    user_id = session.get('user_id')
    if user_id:
        cur = mysql.connection.cursor()
        cur.execute("SELECT id FROM ventas WHERE id=%s AND cliente_id=%s", (venta_id, user_id))
        if not cur.fetchone():
            cur.close()
            flash('No tienes acceso a esta boleta.', 'danger')
            return redirect('/')
        cur.close()
    pdf_bytes, _ = generar_boleta_pdf(venta_id)
    if not pdf_bytes:
        flash('Venta no encontrada.', 'danger')
        return redirect('/')
    filename = f"boleta_{venta_id}.pdf"
    filepath = os.path.join('static', filename)
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
    user_id = session.get('user_id')
    cur = mysql.connection.cursor()
    if user_id:
        cur.execute("SELECT id FROM ventas WHERE id=%s AND cliente_id=%s", (venta_id, user_id))
        if not cur.fetchone():
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
        else:
            flash('No hay más stock disponible.', 'warning')
    cur.close()
    return redirect('/carrito')

# ─────────────────────────────────────────────
# FIX: AGREGAR SOLO PRODUCTOS ACTIVOS
# ─────────────────────────────────────────────
@app.route('/agregar/<int:id>')
def agregar(id):
    try:
        usuario = obtener_usuario()
        cur = mysql.connection.cursor()
        cur.execute(f"SELECT stock, estado FROM {ALMACEN_DB}.productos WHERE id=%s", (id,))
        prod = cur.fetchone()
        if not prod or prod['estado'] != 'activo':
            flash('Producto no disponible.', 'danger')
            return redirect('/')
        stock_disponible = prod['stock']
        if stock_disponible <= 0:
            flash('Producto sin stock disponible.', 'danger')
            return redirect('/')
        cur.execute("SELECT SUM(cantidad) AS total FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        en_carrito = cur.fetchone()['total'] or 0
        if en_carrito + 1 > stock_disponible:
            flash(f'Stock insuficiente. Solo hay {stock_disponible} unidad(es) disponible(s).', 'danger')
            return redirect('/')
        cur.execute("SELECT * FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        item = cur.fetchone()
        if item:
            cur.execute("UPDATE carrito SET cantidad=cantidad+1 WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
        else:
            cur.execute("INSERT INTO carrito (usuario_id, producto_id, cantidad) VALUES(%s,%s,1)", (usuario, id))
        mysql.connection.commit()
        cur.close()
        flash('Producto agregado al carrito', 'success')
    except Exception as e:
        print(f"[agregar_carrito] {e}")
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        flash('Error al agregar al carrito.', 'danger')
    return redirect('/')

# ─────────────────────────────────────────────
# INIT DB EN PRIMER REQUEST (MySQL request-scoped en Flask-MySQLdb)
# ─────────────────────────────────────────────
_db_initialized = False

@app.before_request
def _ensure_db():
    global _db_initialized
    if not _db_initialized:
        _db_initialized = True
        try:
            init_db()
        except Exception as e:
            print(f"[before_request init_db] {e}")

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
