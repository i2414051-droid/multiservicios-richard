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
    return redirect('/procesar_compra')

@app.route('/procesar_compra')
def procesar_compra():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = int(session['user_id'])
    cur = mysql.connection.cursor()

    try:
        cur.execute("SELECT producto_id, cantidad FROM carrito WHERE usuario_id=%s", (user_id,))
        items = cur.fetchall()
        if not items:
            return redirect('/carrito')

        # Verificar stock y obtener precios vía API de Almacén
        total = 0
        items_con_datos = []
        for item in items:
            p = api_almacen(f"/api/productos/{item['producto_id']}")
            if not p:
                return f"Producto #{item['producto_id']} no encontrado en almacén"
            stock = p.get('stock', 0)
            if stock < item['cantidad']:
                return f"Stock insuficiente: {p['nombre']}"
            total += p['precio'] * item['cantidad']
            items_con_datos.append({**item, 'precio': p['precio'], 'nombre': p['nombre']})

        cur.execute("INSERT INTO ventas (cliente_id, total) VALUES(%s,%s)", (user_id, total))
        venta_id = cur.lastrowid

        for item in items_con_datos:
            cur.execute("INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio) VALUES(%s,%s,%s,%s)",
                        (venta_id, item['producto_id'], item['cantidad'], item['precio']))
            # Descargar stock vía API de Almacén
            api_almacen_post(f"/api/productos/{item['producto_id']}/decrementar_stock", {'cantidad': item['cantidad']})

        cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (user_id,))
        mysql.connection.commit()

        correo_cliente = session.get('correo','')
        if correo_cliente:
            def _enviar_boleta():
                with app.app_context():
                    enviar_boleta_cliente(correo_cliente, venta_id)
            threading.Thread(target=_enviar_boleta, daemon=True).start()

        return redirect(f'/confirmacion/{venta_id}')
    except Exception as e:
        mysql.connection.rollback()
        print(f"[procesar_compra] {e}")
        flash('Ocurrió un error al procesar tu compra. Inténtalo de nuevo.', 'danger')
        return redirect('/carrito')
    finally:
        cur.close()

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
