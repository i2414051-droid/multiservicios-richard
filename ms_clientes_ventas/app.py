import os, uuid, io, threading, requests as http_requests
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, send_file)
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambiar-esta-clave-segura')
bcrypt = Bcrypt(app)

UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────
# MYSQL — BD Principal (proyecto_multiservicios_richard)
# ─────────────────────────────────────────────
app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', '')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

# Base de datos de Gestión de Almacén (ingresos, salidas, detalle_*)
# Esta conexión apunta a la BD principal; las tablas de almacén se prefijan
# con ALMACEN_DB para leerlas desde su base dedicada.
ALMACEN_DB = os.environ.get('MYSQL_DB_ALMACEN', 'proyecto_gestion_almacen')

# URL del microservicio de Almacén
MS_ALMACEN_URL = os.environ.get('MS_ALMACEN_URL', 'http://localhost:5001')

# ─────────────────────────────────────────────
# FLASK-MAIL
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
# INIT TABLAS — Solo tablas de Clientes/Ventas
# ─────────────────────────────────────────────
def init_db():
    try:
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
        try:
            cur.execute("ALTER TABLE usuarios ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
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
        mysql.connection.commit()
        print("[MS Clientes/Ventas] Tablas creadas/verificadas OK")
    except Exception as e:
        print(f"[MS Clientes/Ventas] Error creando tablas: {e}")

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

def api_almacen(endpoint):
    """Llama a la API del microservicio de Almacén."""
    try:
        r = http_requests.get(f"{MS_ALMACEN_URL}{endpoint}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[api_almacen] Error {endpoint}: {e}")
    return None

def api_almacen_post(endpoint, data=None):
    """Llama POST a la API del microservicio de Almacén."""
    try:
        r = http_requests.post(f"{MS_ALMACEN_URL}{endpoint}", json=data, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[api_almacen_post] Error {endpoint}: {e}")
    return None

def generar_boleta_pdf(venta_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT documento, nombre, total, fecha FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    if not venta:
        return None, None

    # Obtener detalle de productos desde el microservicio de almacén
    cur.execute("SELECT producto_id, cantidad, precio FROM detalle_venta WHERE venta_id=%s", (venta_id,))
    detalles = cur.fetchall()

    productos = []
    for d in detalles:
        p = api_almacen(f"/api/productos/{d['producto_id']}")
        if p:
            productos.append({'nombre': p['nombre'], 'cantidad': d['cantidad'], 'precio': d['precio']})
        else:
            productos.append({'nombre': f'Producto #{d["producto_id"]}', 'cantidad': d['cantidad'], 'precio': d['precio']})
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

def whatsapp_url(celular, mensaje):
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
    try:
        cur = mysql.connection.cursor()
        cur.execute("SHOW TABLES;")
        return jsonify({'tables': cur.fetchall(), 'service': 'ms_clientes_ventas'})
    except Exception as e:
        return jsonify({'error': str(e)})

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
            return "Contraseñas no coinciden"
        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        if cur.fetchone():
            return "Usuario ya existe"
        h = bcrypt.generate_password_hash(password).decode('utf-8')
        cur.execute("INSERT INTO usuarios (correo, password, rol) VALUES (%s,%s,'cliente')", (correo, h))
        mysql.connection.commit()
        return redirect('/login')
    return render_template('registro.html')

# ─────────────────────────────────────────────
# ADMIN PANEL (Dashboard llama a API de Almacén)
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
    cur.execute("SELECT COUNT(*) AS total FROM usuarios")
    total_usuarios = cur.fetchone()['total'] or 0
    cur.execute("SELECT COUNT(*) AS total FROM ventas WHERE estado='en espera'")
    ventas_pendientes = cur.fetchone()['total'] or 0
    cur.execute("SELECT v.id, v.total, v.fecha, v.estado, v.nombre, v.documento FROM ventas v ORDER BY v.fecha DESC LIMIT 5")
    ultimas_ventas = cur.fetchall()
    cur.close()

    # Datos de Almacén vía API
    stats_almacen = api_almacen('/api/stats') or {}
    productos_stock_bajo = api_almacen('/api/productos/stock_bajo') or []

    return render_template('dashboard.html',
                           total_ventas=total_ventas,
                           ingresos_totales=ingresos_totales,
                           productos_activos=stats_almacen.get('productos_activos', 0),
                           stock_bajo=stats_almacen.get('stock_bajo', 0),
                           total_usuarios=total_usuarios,
                           pedidos_pendientes=stats_almacen.get('pedidos_pendientes', 0),
                           total_proveedores=stats_almacen.get('total_proveedores', 0),
                           ventas_pendientes=ventas_pendientes,
                           ultimas_ventas=ultimas_ventas,
                           productos_stock_bajo=productos_stock_bajo)

@app.route('/admin')
def admin():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return "Acceso denegado"
    # Obtener productos y stats desde el microservicio de Almacén
    productos = api_almacen('/api/productos') or []
    stats_almacen = api_almacen('/api/stats') or {}
    return render_template('admin.html',
                           productos=productos,
                           pedidos_pendientes=stats_almacen.get('pedidos_pendientes', 0),
                           total_proveedores=stats_almacen.get('total_proveedores', 0))

# ─────────────────────────────────────────────
# PRODUCTOS CRUD (vía API de Almacén)
# ─────────────────────────────────────────────
@app.route('/agregar_producto', methods=['POST'])
def agregar_producto():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    nombre      = request.form['nombre']
    descripcion = request.form['descripcion']
    precio      = float(request.form['precio'])
    stock       = int(request.form['stock'])
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

    result = api_almacen_post('/api/productos', {
        'nombre': nombre, 'descripcion': descripcion, 'precio': precio,
        'stock': stock, 'categoria': categoria, 'imagen': imagen_db
    })
    if result:
        flash('Producto agregado correctamente.', 'success')
    else:
        flash('Error al agregar producto.', 'danger')
    return redirect('/admin')

@app.route('/editar_producto/<int:id>', methods=['GET','POST'])
def editar_producto(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    if request.method == 'POST':
        nombre      = request.form['nombre']
        descripcion = request.form['descripcion']
        precio      = float(request.form['precio'])
        stock       = int(request.form['stock'])
        categoria   = request.form['categoria']
        imagen      = request.files.get('imagen')

        if precio < 0:
            flash('No se permiten precios negativos.', 'danger')
            return redirect(f'/editar_producto/{id}')
        if stock < 0:
            flash('No se permiten valores negativos en el stock.', 'danger')
            return redirect(f'/editar_producto/{id}')

        imagen_db = None
        if imagen and imagen.filename:
            fn = secure_filename(imagen.filename)
            imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
            imagen_db = 'uploads/' + fn

        result = api_almacen_post(f'/api/productos/{id}/editar', {
            'nombre': nombre, 'descripcion': descripcion, 'precio': precio,
            'stock': stock, 'categoria': categoria, 'imagen': imagen_db
        })
        flash('Producto actualizado correctamente.' if result else 'Error al actualizar.', 'success' if result else 'danger')
        return redirect('/admin')

    producto = api_almacen(f'/api/productos/{id}')
    return render_template('editar_producto.html', producto=producto, categorias=CATEGORIAS)

@app.route('/eliminar_producto/<int:id>')
def eliminar_producto(id):
    api_almacen_post(f'/api/productos/{id}/inactivar')
    return redirect('/admin')

@app.route('/activar_producto/<int:id>')
def activar_producto(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    api_almacen_post(f'/api/productos/{id}/activar')
    return redirect('/admin')

@app.route('/eliminar_producto_definitivo/<int:id>')
def eliminar_producto_definitivo(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    api_almacen_post(f'/api/productos/{id}/eliminar')
    flash('Producto eliminado permanentemente.', 'success')
    return redirect('/admin')

# ─────────────────────────────────────────────
# CONSULTAR DNI/RUC
# ─────────────────────────────────────────────
@app.route('/consultar/<tipo>/<numero>')
def consultar(tipo, numero):
    venta_id = request.args.get('venta_id')
    if not venta_id:
        return jsonify({'error': 'venta_id no recibido'})
    if tipo not in ['dni','ruc']:
        return jsonify({'error': 'Tipo inválido'})
    url = f"https://dniruc.apisperu.com/api/v1/{tipo}/{numero}?token={TOKEN}"
    try:
        data = http_requests.get(url).json()
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
# TIENDA / CARRITO (productos vía API de Almacén)
# ─────────────────────────────────────────────
@app.route('/')
def index():
    buscar   = request.args.get('buscar','')
    categoria = request.args.get('categoria','Todos')
    params = f"?buscar={buscar}&categoria={categoria}"
    productos = api_almacen(f'/api/productos/buscar{params}') or []
    return render_template('index.html', productos=productos, categorias=CATEGORIAS)

@app.route('/agregar/<int:id>')
def agregar(id):
    try:
        usuario = obtener_usuario()
        # Verificar stock vía API de Almacén
        prod = api_almacen(f'/api/productos/{id}')
        if not prod:
            flash('Producto no encontrado.', 'danger')
            return redirect('/')
        stock_disponible = prod.get('stock', 0)
        if stock_disponible <= 0:
            flash('Producto sin stock disponible.', 'danger')
            return redirect('/')
        cur = mysql.connection.cursor()
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
        flash(f'Error: {e}', 'danger')
    return redirect('/')

@app.route('/carrito')
def ver_carrito():
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute("SELECT c.id, c.producto_id, c.cantidad FROM carrito c WHERE c.usuario_id=%s", (usuario,))
    items = cur.fetchall()
    cur.close()
    # Enriquecer con datos del producto vía API
    productos = []
    for item in items:
        p = api_almacen(f"/api/productos/{item['producto_id']}")
        if p:
            productos.append({
                'id': item['id'], 'producto_id': item['producto_id'],
                'nombre': p['nombre'], 'precio': p['precio'], 'cantidad': item['cantidad']
            })
    total = sum(p['precio'] * p['cantidad'] for p in productos)
    return render_template('carrito.html', productos=productos, total=total)

@app.route('/aumentar-cantidad/<int:id_producto>')
def aumentar_cantidad(id_producto):
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute("UPDATE carrito SET cantidad=cantidad+1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
    mysql.connection.commit()
    return redirect('/carrito')

@app.route('/reducir-cantidad/<int:id_producto>')
def reducir_cantidad(id_producto):
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute("UPDATE carrito SET cantidad=cantidad-1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
    cur.execute("DELETE FROM carrito WHERE cantidad<=0")
    mysql.connection.commit()
    return redirect('/carrito')

@app.route('/actualizar-cantidad/<int:id_producto>', methods=['POST'])
def actualizar_cantidad(id_producto):
    usuario = obtener_usuario()
    accion  = request.json.get('accion')
    cur = mysql.connection.cursor()
    if accion == 'aumentar':
        cur.execute("UPDATE carrito SET cantidad=cantidad+1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
    elif accion == 'reducir':
        cur.execute("UPDATE carrito SET cantidad=cantidad-1 WHERE producto_id=%s AND usuario_id=%s", (id_producto, usuario))
        cur.execute("DELETE FROM carrito WHERE producto_id=%s AND usuario_id=%s AND cantidad<=0", (id_producto, usuario))
    mysql.connection.commit()

    cur.execute("SELECT c.cantidad FROM carrito c WHERE c.producto_id=%s AND c.usuario_id=%s", (id_producto, usuario))
    fila = cur.fetchone()
    cur.execute("SELECT SUM(c.cantidad) AS total_items FROM carrito c WHERE c.usuario_id=%s", (usuario,))
    res = cur.fetchone()
    cur.close()

    if fila:
        # Obtener precio del producto vía API
        p = api_almacen(f"/api/productos/{id_producto}")
        precio = p['precio'] if p else 0
        # Calcular total del carrito
        cur2 = mysql.connection.cursor()
        cur2.execute("SELECT SUM(c.cantidad) AS total_items FROM carrito c WHERE c.usuario_id=%s", (usuario,))
        total_items = cur2.fetchone()['total_items'] or 0
        cur2.close()
        # Simplificación: total calculado por el cliente JS
        subtotal = round(fila['cantidad'] * precio, 2)
        return jsonify({'eliminado':False,'cantidad':fila['cantidad'],'subtotal':subtotal,'total':0})
    return jsonify({'eliminado':True,'total':0})

@app.route('/eliminar_carrito/<int:id>')
def eliminar_carrito(id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM carrito WHERE id=%s", (id,))
    mysql.connection.commit()
    return redirect('/carrito')

# ─────────────────────────────────────────────
# COMPRA (descuenta stock vía API de Almacén)
# ─────────────────────────────────────────────
@app.route('/comprar')
def comprar():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = int(session['user_id'])
    cur = mysql.connection.cursor()
    if 'guest_id' in session:
        cur.execute("UPDATE carrito SET usuario_id=%s WHERE usuario_id=%s", (user_id, session['guest_id']))
        mysql.connection.commit()
    cur.execute("SELECT * FROM carrito WHERE usuario_id=%s", (user_id,))
    if not cur.fetchall():
        flash('Su carrito está vacío', 'warning')
        return redirect('/carrito')
    return redirect('/checkout')

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
    return render_template('boleta_form.html', venta_id=venta_id)

@app.route('/preview_boleta')
def preview_boleta():
    venta_id = request.args.get('venta_id')
    doc      = request.args.get('doc')
    nombre   = request.args.get('nombre')
    cur = mysql.connection.cursor()
    cur.execute("SELECT producto_id, cantidad, precio FROM detalle_venta WHERE venta_id=%s", (venta_id,))
    detalles = cur.fetchall()
    cur.close()
    productos = []
    for d in detalles:
        p = api_almacen(f"/api/productos/{d['producto_id']}")
        pname = p['nombre'] if p else f'Producto #{d["producto_id"]}'
        productos.append({'nombre': pname, 'cantidad': d['cantidad'], 'precio': d['precio']})
    total = sum(p['cantidad'] * p['precio'] for p in productos)
    return render_template('preview_boleta.html', productos=productos, total=total,
                           doc=doc, nombre=nombre, venta_id=venta_id)

@app.route('/guardar_boleta')
def guardar_boleta():
    venta_id = request.args.get('venta_id')
    doc      = request.args.get('doc')
    nombre   = request.args.get('nombre')
    cur = mysql.connection.cursor()
    cur.execute("UPDATE ventas SET documento=%s, nombre=%s WHERE id=%s", (doc, nombre, venta_id))
    mysql.connection.commit()
    return redirect(f'/boleta_pdf/{venta_id}')

@app.route('/boleta_pdf/<int:venta_id>')
def boleta_pdf(venta_id):
    pdf_bytes, _ = generar_boleta_pdf(venta_id)
    if not pdf_bytes:
        return "Venta no encontrada"
    filename = f"boleta_{venta_id}.pdf"
    filepath = os.path.join('static', filename)
    with open(filepath, 'wb') as f:
        f.write(pdf_bytes)
    return send_file(filepath, as_attachment=True)

@app.route('/confirmacion/<int:id>')
def confirmacion(id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT total, fecha FROM ventas WHERE id=%s", (id,))
    venta = cur.fetchone()
    return render_template('confirmacion.html', venta=venta, id=id)

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
        cur.execute("SELECT producto_id, cantidad, precio FROM detalle_venta WHERE venta_id=%s", (v['id'],))
        detalles = cur.fetchall()
        productos = []
        for d in detalles:
            p = api_almacen(f"/api/productos/{d['producto_id']}")
            if p:
                productos.append({'producto_id': d['producto_id'], 'nombre': p['nombre'],
                                  'descripcion': p.get('descripcion',''), 'cantidad': d['cantidad'], 'precio': d['precio']})
            else:
                productos.append({'producto_id': d['producto_id'], 'nombre': f'Producto #{d["producto_id"]}',
                                  'descripcion': '', 'cantidad': d['cantidad'], 'precio': d['precio']})
        hist.append({'id':v['id'],'total':v['total'],'fecha':v['fecha'],'productos':productos})
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
        cur.execute("SELECT producto_id, cantidad, precio FROM detalle_venta WHERE venta_id=%s", (v['id'],))
        detalles = cur.fetchall()
        productos = []
        for d in detalles:
            p = api_almacen(f"/api/productos/{d['producto_id']}")
            if p:
                productos.append({'producto_id': d['producto_id'], 'nombre': p['nombre'],
                                  'descripcion': p.get('descripcion',''), 'cantidad': d['cantidad'], 'precio': d['precio']})
            else:
                productos.append({'producto_id': d['producto_id'], 'nombre': f'Producto #{d["producto_id"]}',
                                  'descripcion': '', 'cantidad': d['cantidad'], 'precio': d['precio']})
        historial.append({**v, 'productos': productos})
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
    try:
        cur.execute("ALTER TABLE usuarios ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        mysql.connection.commit()
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
    if request.method == 'POST':
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
# PROVEEDORES (vía API de Almacén)
# ─────────────────────────────────────────────
@app.route('/proveedores', methods=['GET','POST'])
def proveedores():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    if request.method == 'POST':
        api_almacen_post('/api/proveedores', {
            'nombre': request.form.get('nombre',''), 'celular': request.form.get('celular',''),
            'correo': request.form.get('correo',''), 'dni': request.form.get('dni',''),
            'ruc': request.form.get('ruc',''), 'direccion': request.form.get('direccion',''),
            'categoria': request.form.get('categoria',''), 'notas': request.form.get('notas','')
        })
        flash(f'Proveedor "{request.form.get("nombre","")}" agregado correctamente.', 'success')
        return redirect('/proveedores')

    buscar = request.args.get('buscar','')
    cat_filtro = request.args.get('categoria','')
    params = f"?buscar={buscar}"
    if cat_filtro:
        params += f"&categoria={cat_filtro}"
    lista = api_almacen(f'/api/proveedores{params}') or []
    return render_template('proveedores.html', proveedores=lista,
                           categorias=CATEGORIAS, buscar=buscar, cat_filtro=cat_filtro)

@app.route('/proveedores/editar/<int:id>', methods=['GET','POST'])
def editar_proveedor(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    if request.method == 'POST':
        api_almacen_post(f'/api/proveedores/{id}/editar', {
            'nombre': request.form.get('nombre'), 'celular': request.form.get('celular'),
            'correo': request.form.get('correo'), 'dni': request.form.get('dni'),
            'ruc': request.form.get('ruc'), 'direccion': request.form.get('direccion'),
            'categoria': request.form.get('categoria'), 'notas': request.form.get('notas')
        })
        flash('Proveedor actualizado.', 'success')
        return redirect('/proveedores')
    p = api_almacen(f'/api/proveedores/{id}')
    return render_template('proveedores.html', editar=p, categorias=CATEGORIAS,
                           proveedores=[], buscar='', cat_filtro='')

@app.route('/proveedores/eliminar/<int:id>')
def eliminar_proveedor(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    api_almacen_post(f'/api/proveedores/{id}/eliminar')
    flash('Proveedor eliminado.', 'success')
    return redirect('/proveedores')

# ─────────────────────────────────────────────
# PRODUCTOS PARA PEDIR (vía API de Almacén)
# ─────────────────────────────────────────────
@app.route('/productos-para-pedir')
def productos_para_pedir():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    pedidos = api_almacen('/api/pedidos') or []

    proveedores_dict = {}
    for ped in pedidos:
        pid = ped.get('proveedor_id') or 'sin_proveedor'
        if pid not in proveedores_dict:
            proveedores_dict[pid] = {
                'proveedor_nombre': ped.get('proveedor_nombre') or 'Sin proveedor',
                'proveedor_celular': ped.get('proveedor_celular') or '',
                'proveedor_correo': ped.get('proveedor_correo') or '',
                'productos': []
            }
        proveedores_dict[pid]['productos'].append(ped)

    for pid, data in proveedores_dict.items():
        if data['proveedor_celular']:
            lista = "\n".join([
                f"- {p['producto_nombre']} x{p['cantidad_pedido']} (Stock actual: {p.get('stock_actual',0)})"
                for p in data['productos']
            ])
            msg = f"Hola {data['proveedor_nombre']}, necesitamos reponer los siguientes productos:\n\n{lista}\n\nGracias - Multiservicios Richard"
            data['whatsapp_url'] = whatsapp_url(data['proveedor_celular'], msg)
        else:
            data['whatsapp_url'] = None

    return render_template('productos_para_pedir.html',
                           pedidos=pedidos,
                           proveedores_pedidos=proveedores_dict)

@app.route('/productos-para-pedir/eliminar/<int:id>')
def eliminar_pedido(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    api_almacen_post(f'/api/pedidos/{id}/cancelar')
    flash('Pedido eliminado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/actualizar/<int:id>', methods=['POST'])
def actualizar_pedido(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cantidad = int(request.form.get('cantidad', 1))
    if cantidad < 1:
        cantidad = 1
    api_almacen_post(f'/api/pedidos/{id}/actualizar', {'cantidad': cantidad})
    flash('Cantidad actualizada.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/marcar-enviado/<int:id>')
def marcar_enviado(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    api_almacen_post(f'/api/pedidos/{id}/marcar-enviado')
    flash('Pedido marcado como enviado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/enviar-email/<int:proveedor_id>')
def enviar_email_a_proveedor(proveedor_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    proveedor = api_almacen(f'/api/proveedores/{proveedor_id}')
    if not proveedor:
        flash('Proveedor no encontrado.', 'danger')
        return redirect('/productos-para-pedir')
    productos_lista = api_almacen(f'/api/pedidos/proveedor/{proveedor_id}') or []
    if not productos_lista:
        flash('No hay productos pendientes para este proveedor.', 'warning')
        return redirect('/productos-para-pedir')
    # Enviar email desde MS Clientes/Ventas (tiene Flask-Mail)
    try:
        if not proveedor.get('correo'):
            flash('El proveedor no tiene correo electrónico.', 'danger')
            return redirect('/productos-para-pedir')
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
        flash(f'Email enviado a {proveedor["correo"]}', 'success')
    except Exception as e:
        print(f"[email_proveedor] {e}")
        flash('Error al enviar email. Verifica la configuración SMTP.', 'danger')
    return redirect('/productos-para-pedir')

# ─────────────────────────────────────────────
# API INTERNOS (para MS Almacén)
# ─────────────────────────────────────────────
@app.route('/api/ventas/<int:venta_id>')
def api_venta(venta_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, cliente_id, total, documento, nombre, estado, fecha FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()
    cur.close()
    if not venta:
        return jsonify({'error': 'Venta no encontrada'}), 404
    if isinstance(venta.get('fecha'), datetime):
        venta['fecha'] = venta['fecha'].isoformat()
    return jsonify(venta)

@app.route('/api/usuarios/<int:user_id>')
def api_usuario(user_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, correo, rol, estado FROM usuarios WHERE id=%s", (user_id,))
    usuario = cur.fetchone()
    cur.close()
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    return jsonify(usuario)

@app.route('/api/ventas')
def api_ventas():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, cliente_id, total, estado, fecha FROM ventas ORDER BY fecha DESC")
    ventas = cur.fetchall()
    cur.close()
    for v in ventas:
        if isinstance(v.get('fecha'), datetime):
            v['fecha'] = v['fecha'].isoformat()
    return jsonify(ventas)

# ─────────────────────────────────────────────
# VALIDAR DOCUMENTO (para checkout - clientes)
# ─────────────────────────────────────────────
@app.route('/api/validar-documento', methods=['POST'])
def api_validar_documento():
    if 'user_id' not in session:
        return jsonify({'error': 'No autenticado'}), 401
    data = request.get_json()
    tipo = data.get('tipo', '')
    numero = data.get('numero', '')
    nombres = data.get('nombres', '').strip()
    apellidos = data.get('apellidos', '').strip()
    if tipo not in ['dni', 'ruc']:
        return jsonify({'error': 'Tipo invalido', 'valido': False})
    if not numero or not nombres or not apellidos:
        return jsonify({'error': 'Campos incompletos', 'valido': False})
    if tipo == 'dni' and len(numero) != 8:
        return jsonify({'error': 'DNI debe tener 8 digitos', 'valido': False})
    if tipo == 'ruc' and len(numero) != 11:
        return jsonify({'error': 'RUC debe tener 11 digitos', 'valido': False})
    if not TOKEN:
        return jsonify({'error': 'API no configurada', 'valido': False})
    url = f"https://dniruc.apisperu.com/api/v1/{tipo}/{numero}?token={TOKEN}"
    try:
        resp = http_requests.get(url, timeout=10)
        api_data = resp.json()
        if 'error' in api_data:
            return jsonify({'error': 'Documento no encontrado', 'valido': False})
        if tipo == 'dni':
            api_nombre = f"{api_data.get('nombres', '')} {api_data.get('apellidoPaterno', '')} {api_data.get('apellidoMaterno', '')}".strip()
            api_estado = api_data.get('estado', '')
            if api_estado and api_estado.upper() != 'ACTIVO':
                return jsonify({'error': f'Estado del DNI: {api_estado}', 'valido': False})
        else:
            api_nombre = api_data.get('razonSocial', '')
            api_estado = api_data.get('estado', '')
            api_condicion = api_data.get('condicion', '')
            if api_estado and api_estado.upper() != 'ACTIVO':
                return jsonify({'error': f'Estado del RUC: {api_estado}', 'valido': False})
            if api_condicion and 'HABIDO' not in api_condicion.upper():
                return jsonify({'error': f'Condicion: {api_condicion}', 'valido': False})
        usuario_nombre = f"{nombres} {apellidos}".strip().upper()
        api_nombre_upper = api_nombre.upper()
        if usuario_nombre not in api_nombre_upper and api_nombre_upper not in usuario_nombre:
            similitud = sum(1 for a, b in zip(usuario_nombre.split(), api_nombre_upper.split()) if a == b)
            total_palabras = max(len(usuario_nombre.split()), len(api_nombre_upper.split()))
            if total_palabras > 0 and similitud / total_palabras < 0.5:
                return jsonify({'error': f'Los datos no coinciden. Registrado: {api_nombre}', 'valido': False, 'api_nombre': api_nombre})
        return jsonify({'valido': True, 'api_nombre': api_nombre})
    except Exception as e:
        return jsonify({'error': 'Error al validar documento', 'valido': False})

# ─────────────────────────────────────────────
# DETALLE DE PRODUCTO
# ─────────────────────────────────────────────
@app.route('/producto/<int:id>')
def detalle_producto(id):
    data = almacen_api(f"/api/productos/{id}")
    if not data or not data.get('producto'):
        flash('Producto no encontrado.', 'danger')
        return redirect('/')
    return render_template('producto_detalle.html', producto=data['producto'])

# ─────────────────────────────────────────────
# DIRECCIONES DEL CLIENTE
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
            flash('Token CSRF invalido.', 'danger')
            return redirect('/direcciones')
        user_id = session['user_id']
        direccion = request.form.get('direccion', '').strip()
        distrito = request.form.get('distrito', '').strip()
        referencia = request.form.get('referencia', '').strip()
        predeterminada = 1 if request.form.get('predeterminada') else 0
        if not direccion:
            flash('La direccion es obligatoria.', 'danger')
            return redirect('/direcciones/nueva')
        cur = mysql.connection.cursor()
        if predeterminada:
            cur.execute("UPDATE direcciones SET predeterminada=0 WHERE usuario_id=%s", (user_id,))
        cur.execute("INSERT INTO direcciones (usuario_id, direccion, distrito, referencia, predeterminada) VALUES (%s,%s,%s,%s,%s)",
                    (user_id, direccion, distrito, referencia, predeterminada))
        mysql.connection.commit()
        cur.close()
        flash('Direccion agregada.', 'success')
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
            flash('Token CSRF invalido.', 'danger')
            return redirect('/direcciones')
        direccion = request.form.get('direccion', '').strip()
        distrito = request.form.get('distrito', '').strip()
        referencia = request.form.get('referencia', '').strip()
        predeterminada = 1 if request.form.get('predeterminada') else 0
        if not direccion:
            flash('La direccion es obligatoria.', 'danger')
            return redirect(f'/direcciones/editar/{id}')
        if predeterminada:
            cur.execute("UPDATE direcciones SET predeterminada=0 WHERE usuario_id=%s", (user_id,))
        cur.execute("UPDATE direcciones SET direccion=%s, distrito=%s, referencia=%s, predeterminada=%s WHERE id=%s AND usuario_id=%s",
                    (direccion, distrito, referencia, predeterminada, id, user_id))
        mysql.connection.commit()
        cur.close()
        flash('Direccion actualizada.', 'success')
        return redirect('/direcciones')
    cur.execute("SELECT * FROM direcciones WHERE id=%s AND usuario_id=%s", (id, user_id))
    dir_editar = cur.fetchone()
    cur.close()
    if not dir_editar:
        flash('Direccion no encontrada.', 'danger')
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
    flash('Direccion eliminada.', 'success')
    return redirect('/direcciones')

# ─────────────────────────────────────────────
# CHECKOUT
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
    cur.execute("SELECT c.id, c.producto_id, c.cantidad FROM carrito c WHERE c.usuario_id=%s", (user_id,))
    carrito_items = cur.fetchall()
    if not carrito_items:
        flash('Su carrito esta vacio.', 'warning')
        return redirect('/carrito')
    items = []
    total = 0
    for c in carrito_items:
        data = almacen_api(f"/api/productos/{c['producto_id']}")
        if data and data.get('producto'):
            p = data['producto']
            items.append({'id': c['id'], 'producto_id': c['producto_id'], 'nombre': p['nombre'],
                          'precio': float(p['precio']), 'imagen': p.get('imagen'), 'cantidad': c['cantidad']})
            total += float(p['precio']) * c['cantidad']
    cur.execute("SELECT * FROM direcciones WHERE usuario_id=%s ORDER BY predeterminada DESC", (user_id,))
    direcciones = cur.fetchall()
    cur.execute("SELECT correo, nombre, apellido, telefono, documento_tipo, documento_numero FROM usuarios WHERE id=%s", (user_id,))
    usuario = cur.fetchone()
    cur.close()
    return render_template('checkout.html', items=items, total=total,
                           direcciones=direcciones, usuario=usuario)

@app.route('/procesar_compra', methods=['POST'])
def procesar_compra():
    if 'user_id' not in session:
        return redirect('/login')
    if not validate_csrf():
        flash('Token CSRF invalido.', 'danger')
        return redirect('/checkout')
    user_id = int(session['user_id'])
    nombres = request.form.get('nombres', '').strip()
    apellidos = request.form.get('apellidos', '').strip()
    doc_tipo = request.form.get('doc_tipo', 'dni')
    doc_numero = request.form.get('doc_numero', '').strip()
    telefono = request.form.get('telefono', '').strip()
    correo = request.form.get('correo', '').strip()
    metodo_pago = request.form.get('metodo_pago', 'efectivo')
    direccion_envio = request.form.get('direccion_envio', '')
    notas_entrega = request.form.get('notas_entrega', '').strip()
    if not all([nombres, apellidos, doc_numero, telefono, direccion_envio]):
        flash('Todos los campos son obligatorios.', 'danger')
        return redirect('/checkout')
    if doc_tipo == 'dni' and (len(doc_numero) != 8 or not doc_numero.isdigit()):
        flash('DNI debe tener exactamente 8 digitos.', 'danger')
        return redirect('/checkout')
    if doc_tipo == 'ruc' and (len(doc_numero) != 11 or not doc_numero.isdigit()):
        flash('RUC debe tener exactamente 11 digitos.', 'danger')
        return redirect('/checkout')
    if len(telefono) < 9 or not telefono.isdigit():
        flash('Celular debe tener al menos 9 digitos.', 'danger')
        return redirect('/checkout')
    comprobante_filename = ''
    if metodo_pago in ['yape', 'plin', 'transferencia']:
        comprobante = request.files.get('comprobante_pago')
        if comprobante and comprobante.filename:
            ext = os.path.splitext(comprobante.filename)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp', '.pdf']:
                fn = f"comprobante_{user_id}_{int(time.time())}{ext}"
                comprobante.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
                comprobante_filename = 'uploads/' + fn
            else:
                flash('Formato de comprobante no valido.', 'danger')
                return redirect('/checkout')
    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT id FROM ventas WHERE cliente_id=%s AND fecha > NOW() - INTERVAL 30 SECOND", (user_id,))
        if cur.fetchone():
            flash('Ya estas procesando una compra. Espera unos segundos.', 'warning')
            return redirect('/checkout')
        cur.execute("SELECT c.id, c.producto_id, c.cantidad FROM carrito c WHERE c.usuario_id=%s", (user_id,))
        carrito_items = cur.fetchall()
        if not carrito_items:
            return redirect('/carrito')
        items = []
        total = 0
        for c in carrito_items:
            data = almacen_api(f"/api/productos/{c['producto_id']}")
            if data and data.get('producto'):
                p = data['producto']
                items.append({'id': c['producto_id'], 'precio': float(p['precio']), 'cantidad': c['cantidad'], 'nombre': p['nombre']})
                if int(p.get('stock', 0)) < c['cantidad']:
                    flash(f'Stock insuficiente para: {p["nombre"]}. Solo quedan {p["stock"]} unidad(es).', 'danger')
                    return redirect('/carrito')
                total += float(p['precio']) * c['cantidad']
        cur.execute("""INSERT INTO ventas
            (cliente_id, total, metodo_pago, direccion_envio, documento, nombre, apellido, telefono, correo, notas_entrega, comprobante_pago)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, total, metodo_pago, direccion_envio, doc_numero, nombres, apellidos, telefono, correo, notas_entrega, comprobante_filename or None))
        venta_id = cur.lastrowid
        for item in items:
            cur.execute("INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio) VALUES(%s,%s,%s,%s)",
                        (venta_id, item['id'], item['cantidad'], item['precio']))
            almacen_api(f"/api/productos/{item['id']}/descontar-stock", method='POST', data={'cantidad': item['cantidad']})
        cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (user_id,))
        cur.execute("""UPDATE usuarios SET
            nombre=IFNULL(nombre,%s), apellido=IFNULL(apellido,%s),
            telefono=IFNULL(telefono,%s), documento_tipo=IFNULL(documento_tipo,%s),
            documento_numero=IFNULL(documento_numero,%s)
            WHERE id=%s""",
            (nombres, apellidos, telefono, doc_tipo, doc_numero, user_id))
        mysql.connection.commit()
        try:
            enviar_boleta_cliente(correo, venta_id)
        except Exception:
            pass
        return redirect(f'/confirmacion/{venta_id}')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[procesar_compra] {e}")
        flash('Error al procesar la compra.', 'danger')
        return redirect('/carrito')
    finally:
        cur.close()

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
            flash('Token CSRF invalido.', 'danger')
            return redirect('/perfil')
        nuevo_correo = request.form.get('correo', '').strip()
        if nuevo_correo and nuevo_correo != session.get('correo'):
            cur.execute("SELECT id FROM usuarios WHERE correo=%s AND id!=%s", (nuevo_correo, user_id))
            if cur.fetchone():
                flash('Ese correo ya esta en uso.', 'danger')
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
        flash('Token CSRF invalido.', 'danger')
        return redirect('/perfil')
    user_id = session['user_id']
    actual = request.form.get('actual', '')
    nueva = request.form.get('nueva', '')
    confirmar = request.form.get('confirmar_password', '')
    if not actual or not nueva:
        flash('Completa todos los campos.', 'danger')
        return redirect('/perfil')
    if len(nueva) < 6:
        flash('La nueva contrasena debe tener al menos 6 caracteres.', 'danger')
        return redirect('/perfil')
    if nueva != confirmar:
        flash('Las contrasenas no coinciden.', 'danger')
        return redirect('/perfil')
    cur = mysql.connection.cursor()
    cur.execute("SELECT password FROM usuarios WHERE id=%s", (user_id,))
    user = cur.fetchone()
    if not user or not bcrypt.check_password_hash(user['password'], actual):
        flash('La contrasena actual es incorrecta.', 'danger')
        cur.close()
        return redirect('/perfil')
    h = bcrypt.generate_password_hash(nueva).decode('utf-8')
    cur.execute("UPDATE usuarios SET password=%s WHERE id=%s", (h, user_id))
    mysql.connection.commit()
    cur.close()
    flash('Contrasena cambiada correctamente.', 'success')
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
# RECUPERAR CONTRASENA
# ─────────────────────────────────────────────
@app.route('/recuperar-password', methods=['GET','POST'])
def recuperar_password():
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF invalido.', 'danger')
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
        flash('Si el correo existe, recibiras un enlace para restablecer tu contrasena.', 'success')
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
        flash('El enlace es invalido o ha expirado.', 'danger')
        return redirect('/login')
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF invalido.', 'danger')
            return redirect(f'/restablecer-password/{token}')
        nueva = request.form.get('nueva', '')
        confirmar = request.form.get('confirmar_password', '')
        if len(nueva) < 6:
            flash('La contrasena debe tener al menos 6 caracteres.', 'danger')
            return redirect(f'/restablecer-password/{token}')
        if nueva != confirmar:
            flash('Las contrasenas no coinciden.', 'danger')
            return redirect(f'/restablecer-password/{token}')
        h = bcrypt.generate_password_hash(nueva).decode('utf-8')
        cur.execute("UPDATE usuarios SET password=%s, recuperacion_token=NULL, recuperacion_expira=NULL WHERE id=%s",
                    (h, user['id']))
        mysql.connection.commit()
        cur.close()
        flash('Contrasena restablecida correctamente.', 'success')
        return redirect('/login')
    cur.close()
    return render_template('restablecer_password.html', token=token)

# ─────────────────────────────────────────────
# SEGUIMIENTO DE ENTREGAS (ADMIN)
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
    cur.execute("SELECT producto_id, cantidad, precio FROM detalle_venta WHERE venta_id=%s", (entrega['venta_id'],))
    detalles = cur.fetchall()
    cur.close()
    productos = []
    for d in detalles:
        data = almacen_api(f"/api/productos/{d['producto_id']}")
        nombre = data['producto']['nombre'] if data and data.get('producto') else f'Producto #{d["producto_id"]}'
        imagen = data['producto'].get('imagen') if data and data.get('producto') else None
        productos.append({'nombre': nombre, 'imagen': imagen, 'cantidad': d['cantidad'], 'precio': d['precio']})
    return render_template('detalle_entrega.html', entrega=entrega, productos=productos)

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
    cur.execute("SELECT producto_id, cantidad, precio FROM detalle_venta WHERE venta_id=%s", (venta_id,))
    detalles = cur.fetchall()
    cur.execute("SELECT id, direccion, distrito, referencia FROM direcciones WHERE usuario_id=%s", (venta['documento'] or ''))
    direcciones = cur.fetchall()
    cur.close()
    productos_venta = []
    for d in detalles:
        data = almacen_api(f"/api/productos/{d['producto_id']}")
        nombre = data['producto']['nombre'] if data and data.get('producto') else f'Producto #{d["producto_id"]}'
        imagen = data['producto'].get('imagen') if data and data.get('producto') else None
        productos_venta.append({'producto_id': d['producto_id'], 'nombre': nombre, 'imagen': imagen,
                                'cantidad': d['cantidad'], 'precio': d['precio']})
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF invalido.', 'danger')
            return redirect(f'/entregas/asignar/{venta_id}')
        direccion = request.form.get('direccion_envio', '')
        fecha_est = request.form.get('fecha_estimada') or None
        notas = request.form.get('notas', '')
        cur = mysql.connection.cursor()
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
    return render_template('asignar_entrega.html', venta=venta, productos_venta=productos_venta, direcciones=direcciones)

# ─────────────────────────────────────────────
# INGRESOS DE PRODUCTOS
# ─────────────────────────────────────────────
@app.route('/ingresos')
def ingresos():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    sql_count = f"""SELECT COUNT(*) AS total FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        WHERE p.nombre LIKE %s"""
    sql_data = f"""SELECT i.id, i.fecha, i.notas,
               p.nombre AS proveedor_nombre,
               (SELECT SUM(di.cantidad) FROM {ALMACEN_DB}.detalle_ingreso di WHERE di.ingreso_id=i.id) AS total_items
        FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        WHERE p.nombre LIKE %s
        ORDER BY i.fecha DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, (f'%{buscar}%',), page)
    cur.close()
    return render_template('ingresos.html', ingresos=items, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages)

@app.route('/registrar-ingreso', methods=['GET','POST'])
def registrar_ingreso():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    data_prod = almacen_api("/api/productos?page=1000") or {}
    productos = data_prod.get('productos', [])
    data_prov = almacen_api("/api/proveedores") or {}
    proveedores = data_prov.get('proveedores', [])
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF invalido.', 'danger')
            return redirect('/registrar-ingreso')
        proveedor_id = request.form.get('proveedor_id') or None
        notas = request.form.get('notas', '')
        producto_ids = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')
        precios = request.form.getlist('precio_compra[]')
        cur = mysql.connection.cursor()
        cur.execute(f"INSERT INTO {ALMACEN_DB}.ingresos (proveedor_id, notas) VALUES (%s, %s)", (proveedor_id, notas))
        ingreso_id = cur.lastrowid
        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i])
                prec = float(precios[i]) if precios[i] else 0
            except (ValueError, IndexError):
                continue
            if cant > 0:
                cur.execute(f"INSERT INTO {ALMACEN_DB}.detalle_ingreso (ingreso_id, producto_id, cantidad, precio_compra) VALUES (%s,%s,%s,%s)",
                            (ingreso_id, pid, cant, prec))
                almacen_api(f"/api/productos/{pid}/incrementar-stock", method='POST', data={'cantidad': cant})
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
    cur.execute(f"""
        SELECT i.id, i.fecha, i.notas,
               p.nombre AS proveedor_nombre, p.ruc AS proveedor_ruc,
               p.celular AS proveedor_celular, p.correo AS proveedor_correo
        FROM {ALMACEN_DB}.ingresos i
        LEFT JOIN {ALMACEN_DB}.proveedores p ON i.proveedor_id=p.id
        WHERE i.id=%s
    """, (id,))
    ingreso = cur.fetchone()
    if not ingreso:
        flash('Ingreso no encontrado.', 'danger')
        return redirect('/ingresos')
    cur.execute(f"SELECT producto_id, cantidad, precio_compra FROM {ALMACEN_DB}.detalle_ingreso WHERE ingreso_id=%s", (id,))
    detalles = cur.fetchall()
    cur.close()
    items = []
    total_general = 0
    for d in detalles:
        data = almacen_api(f"/api/productos/{d['producto_id']}")
        nombre = data['producto']['nombre'] if data and data.get('producto') else f'Producto #{d["producto_id"]}'
        stock = data['producto'].get('stock', 0) if data and data.get('producto') else 0
        precio = float(d['precio_compra'])
        items.append({'cantidad': d['cantidad'], 'precio_compra': precio, 'producto_nombre': nombre, 'stock_actual': stock})
        total_general += d['cantidad'] * precio
    return render_template('comprobante_ingreso.html', ingreso=ingreso, items=items, total_general=total_general)

@app.route('/eliminar-ingreso/<int:id>')
def eliminar_ingreso(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT producto_id, cantidad FROM {ALMACEN_DB}.detalle_ingreso WHERE ingreso_id=%s", (id,))
    items = cur.fetchall()
    revertidos = 0
    for item in items:
        almacen_api(f"/api/productos/{item['producto_id']}/descontar-stock", method='POST', data={'cantidad': item['cantidad']})
        revertidos += 1
    cur.execute(f"DELETE FROM {ALMACEN_DB}.detalle_ingreso WHERE ingreso_id=%s", (id,))
    cur.execute(f"DELETE FROM {ALMACEN_DB}.ingresos WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash(f'Ingreso eliminado y stock revertido ({revertidos} productos).', 'success')
    return redirect('/ingresos')

# ─────────────────────────────────────────────
# SALIDAS DE PRODUCTOS
# ─────────────────────────────────────────────
@app.route('/salidas')
def salidas():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    sql_count = f"""SELECT COUNT(*) AS total FROM {ALMACEN_DB}.salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE COALESCE(v.nombre, '') LIKE %s"""
    sql_data = f"""SELECT s.id, s.fecha, s.notas,
               v.id AS venta_id, v.nombre AS cliente_nombre, v.total AS venta_total,
               (SELECT SUM(ds.cantidad) FROM {ALMACEN_DB}.detalle_salida ds WHERE ds.salida_id=s.id) AS total_items
        FROM {ALMACEN_DB}.salidas s
        LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE COALESCE(v.nombre, '') LIKE %s
        ORDER BY s.fecha DESC"""
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
    cur.close()
    data_prod = almacen_api("/api/productos?page=1000") or {}
    productos = data_prod.get('productos', [])
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF invalido.', 'danger')
            return redirect('/registrar-salida')
        venta_id = request.form.get('venta_id') or None
        notas = request.form.get('notas', '')
        producto_ids = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')
        errores = []
        for i in range(len(producto_ids)):
            try:
                pid = int(producto_ids[i])
                cant = int(cantidades[i])
            except (ValueError, IndexError):
                continue
            if cant > 0:
                data = almacen_api(f"/api/productos/{pid}/stock")
                stock_actual = data.get('stock', 0) if data else 0
                if stock_actual < cant:
                    errores.append(f'Producto #{pid}: stock insuficiente (hay {stock_actual}, necesitas {cant})')
        if errores:
            flash('No se puede registrar la salida: ' + '; '.join(errores), 'danger')
            return redirect('/registrar-salida')
        cur = mysql.connection.cursor()
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
                almacen_api(f"/api/productos/{pid}/descontar-stock", method='POST', data={'cantidad': cant})
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
    cur.execute(f"""
        SELECT s.id, s.fecha, s.notas,
               v.id AS venta_id, v.nombre AS cliente_nombre, v.documento AS cliente_doc,
               v.total AS venta_total, v.fecha AS venta_fecha
        FROM {ALMACEN_DB}.salidas s LEFT JOIN ventas v ON s.venta_id=v.id
        WHERE s.id=%s
    """, (id,))
    salida = cur.fetchone()
    if not salida:
        flash('Salida no encontrada.', 'danger')
        return redirect('/salidas')
    cur.execute(f"SELECT producto_id, cantidad FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=%s", (id,))
    detalles = cur.fetchall()
    cur.close()
    items = []
    for d in detalles:
        data = almacen_api(f"/api/productos/{d['producto_id']}")
        nombre = data['producto']['nombre'] if data and data.get('producto') else f'Producto #{d["producto_id"]}'
        stock = data['producto'].get('stock', 0) if data and data.get('producto') else 0
        items.append({'cantidad': d['cantidad'], 'producto_nombre': nombre, 'stock_actual': stock})
    return render_template('comprobante_salida.html', salida=salida, items=items)

@app.route('/eliminar-salida/<int:id>')
def eliminar_salida(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT producto_id, cantidad FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=%s", (id,))
    items = cur.fetchall()
    for item in items:
        almacen_api(f"/api/productos/{item['producto_id']}/incrementar-stock", method='POST', data={'cantidad': item['cantidad']})
    cur.execute(f"DELETE FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=%s", (id,))
    cur.execute(f"DELETE FROM {ALMACEN_DB}.salidas WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Salida eliminada y stock revertido.', 'success')
    return redirect('/salidas')

# ─────────────────────────────────────────────
# VERIFICAR INVENTARIO POR CANTIDAD
# ─────────────────────────────────────────────
@app.route('/verificar-inventario')
def verificar_inventario():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    filtro = request.args.get('filtro', 'todos')
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    data = almacen_api(f"/api/verificar-inventario?filtro={filtro}&buscar={buscar}&page={page}")
    if data:
        return render_template('verificar_inventario.html',
                               productos=data.get('productos', []), filtro=filtro, buscar=buscar,
                               page=data.get('page', 1), total_pages=data.get('total_pages', 1), total=data.get('total', 0),
                               has_prev=data.get('page', 1) > 1, has_next=data.get('page', 1) < data.get('total_pages', 1),
                               sin_stock=data.get('sin_stock', 0), critico=data.get('critico', 0),
                               bajo=data.get('bajo', 0), normal=data.get('normal', 0))
    return render_template('verificar_inventario.html',
                           productos=[], filtro=filtro, buscar=buscar,
                           page=1, total_pages=1, total=0, has_prev=False, has_next=False,
                           sin_stock=0, critico=0, bajo=0, normal=0)

# ─────────────────────────────────────────────
# VERIFICAR / CONSULTAR REGISTROS
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
    cur.close()
    api_stats = almacen_api("/api/verificar-registros")
    if api_stats:
        stats['total_productos'] = api_stats.get('total_productos', 0)
        stats['total_proveedores'] = api_stats.get('total_proveedores', 0)
        stats['total_pedidos'] = api_stats.get('total_pedidos', 0)
    else:
        stats['total_productos'] = stats['total_proveedores'] = stats['total_pedidos'] = 0
    datos = []
    if seccion == 'ventas':
        cur = mysql.connection.cursor()
        cur.execute("SELECT v.id, v.total, v.fecha, v.estado, v.nombre, v.documento, u.correo FROM ventas v LEFT JOIN usuarios u ON v.cliente_id=u.id ORDER BY v.fecha DESC LIMIT 50")
        datos = cur.fetchall()
        cur.close()
    elif seccion == 'ingresos':
        cur = mysql.connection.cursor()
        cur.execute(f"SELECT i.id, i.fecha, (SELECT SUM(cantidad) FROM {ALMACEN_DB}.detalle_ingreso WHERE ingreso_id=i.id) AS items FROM {ALMACEN_DB}.ingresos i ORDER BY i.fecha DESC LIMIT 50")
        datos = cur.fetchall()
        cur.close()
    elif seccion == 'salidas':
        cur = mysql.connection.cursor()
        cur.execute(f"SELECT s.id, s.fecha, v.nombre AS cliente_nombre, (SELECT SUM(cantidad) FROM {ALMACEN_DB}.detalle_salida WHERE salida_id=s.id) AS items FROM {ALMACEN_DB}.salidas s LEFT JOIN ventas v ON s.venta_id=v.id ORDER BY s.fecha DESC LIMIT 50")
        datos = cur.fetchall()
        cur.close()
    elif seccion == 'entregas':
        cur = mysql.connection.cursor()
        cur.execute("SELECT e.id, e.estado, e.fecha_entrega, v.nombre AS cliente_nombre, v.total FROM seguimiento_entregas e JOIN ventas v ON e.venta_id=v.id ORDER BY e.created_at DESC LIMIT 50")
        datos = cur.fetchall()
        cur.close()
    elif seccion == 'usuarios':
        cur = mysql.connection.cursor()
        cur.execute("SELECT id, correo, rol, estado, created_at FROM usuarios ORDER BY created_at DESC LIMIT 50")
        datos = cur.fetchall()
        cur.close()
    return render_template('verificar_registros.html', stats=stats, seccion=seccion, datos=datos)

# ─────────────────────────────────────────────
# INFORME FINAL DEL DIA
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
    cur.execute(f"SELECT i.id, i.fecha, (SELECT SUM(di.cantidad) FROM {ALMACEN_DB}.detalle_ingreso di WHERE di.ingreso_id=i.id) AS items, (SELECT COALESCE(SUM(di.cantidad*di.precio_compra),0) FROM {ALMACEN_DB}.detalle_ingreso di WHERE di.ingreso_id=i.id) AS costo FROM {ALMACEN_DB}.ingresos i WHERE DATE(i.fecha)=%s ORDER BY i.fecha", (fecha_str,))
    ingresos_dia = cur.fetchall()
    cur.execute(f"SELECT COUNT(*) AS n FROM {ALMACEN_DB}.ingresos WHERE DATE(fecha)=%s", (fecha_str,))
    num_ingresos = cur.fetchone()['n']
    cur.execute(f"SELECT s.id, s.fecha, v.nombre AS cliente_nombre, (SELECT SUM(ds.cantidad) FROM {ALMACEN_DB}.detalle_salida ds WHERE ds.salida_id=s.id) AS items FROM {ALMACEN_DB}.salidas s LEFT JOIN ventas v ON s.venta_id=v.id WHERE DATE(s.fecha)=%s ORDER BY s.fecha", (fecha_str,))
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
# ERROR HANDLER
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
# INIT DB EN PRIMER REQUEST
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
