import os, uuid, io
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
app.secret_key = os.environ.get('SECRET_KEY', 'clave_secreta_richard_2024')
bcrypt = Bcrypt(app)

UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────
# MYSQL (env vars para Render)
# ─────────────────────────────────────────────
app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST',     'mysql-proyecto.alwaysdata.net')
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER',     'proyecto')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', 'B12345678Jhoss')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB',       'proyecto_multiservicios_richard')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

# ─────────────────────────────────────────────
# FLASK-MAIL (env vars para Render)
# ─────────────────────────────────────────────
app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER',   'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', '')
mail = Mail(app)

TOKEN = os.environ.get('APIPERU_TOKEN',
    'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.'
    'eyJlbWFpbCI6Impob3NzdWVicnlhbkBnbWFpbC5jb20ifQ.'
    'VvkKvQL_se-h31zZ87zXwBzH6lYy3wLb4pD0XCmhN5o')

CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']

# ─────────────────────────────────────────────
# INIT TABLAS NUEVAS mysql
# ─────────────────────────────────────────────
def init_db():
    try:
        cur = mysql.connection.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS proveedores (
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS productos_para_pedir (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                producto_id     INT NOT NULL,
                cantidad_pedido INT DEFAULT 1,
                proveedor_id    INT,
                estado          VARCHAR(20) DEFAULT 'pendiente',
                fecha           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        mysql.connection.commit()
        cur.close()
    except Exception as e:
        print(f"[init_db] {e}")

# ─────────────────────────────────────────────
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

    cur.execute("""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d JOIN productos p ON d.producto_id=p.id
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
    """Si stock = 0 auto-agrega a productos_para_pedir y notifica al proveedor."""
    try:
        cur.execute("SELECT nombre, stock, categoria FROM productos WHERE id=%s", (producto_id,))
        p = cur.fetchone()
        if not p or p['stock'] == 0:
            return
        # ¿Ya existe pendiente?
        cur.execute("""
            SELECT id FROM productos_para_pedir
            WHERE producto_id=%s AND estado='pendiente'
        """, (producto_id,))
        if cur.fetchone():
            return
        # Proveedor por categoría
        cur.execute("""
            SELECT id, nombre, celular, correo
            FROM proveedores WHERE categoria=%s LIMIT 1
        """, (p['categoria'],))
        proveedor = cur.fetchone()
        proveedor_id = proveedor['id'] if proveedor else None

        cur.execute("""
            INSERT INTO productos_para_pedir (producto_id, cantidad_pedido, proveedor_id)
            VALUES (%s, 1, %s)
        """, (producto_id, proveedor_id))

        # Email al proveedor
        if proveedor and proveedor.get('correo'):
            enviar_email_proveedor(proveedor, [{
                'nombre': p['nombre'],
                'categoria': p['categoria'],
                'cantidad_pedido': 1
            }])
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
    return redirect('/admin')

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
                return redirect('/admin')
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
# ADMIN PANEL
# ─────────────────────────────────────────────
@app.route('/admin')
def admin():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return "Acceso denegado"
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos")
    productos = cur.fetchall()

    # Contadores para badges
    cur.execute("SELECT COUNT(*) AS total FROM productos_para_pedir WHERE estado='pendiente'")
    r = cur.fetchone()
    pedidos_pendientes = r['total'] if r else 0

    cur.execute("SELECT COUNT(*) AS total FROM proveedores")
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
        fn = secure_filename(imagen.filename)
        imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
        imagen_db = 'uploads/' + fn

    cur = mysql.connection.cursor()

    cur.execute("""
        INSERT INTO productos (nombre, descripcion, precio, stock, categoria, imagen, estado)
        VALUES (%s,%s,%s,%s,%s,%s,'activo')
    """, (nombre, descripcion, precio, stock, categoria, imagen_db))

    mysql.connection.commit()
    cur.close()

    return redirect('/admin')


# ─────────────────────────────────────────────

@app.route('/editar_producto/<int:id>', methods=['GET','POST'])
def editar_producto(id):

    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')

    cur = mysql.connection.cursor()

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

        if imagen and imagen.filename:
            fn = secure_filename(imagen.filename)
            imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))

            cur.execute("""
                UPDATE productos
                SET nombre=%s, descripcion=%s, precio=%s,
                    stock=%s, categoria=%s, imagen=%s
                WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, 'uploads/'+fn, id))

        else:
            cur.execute("""
                UPDATE productos
                SET nombre=%s, descripcion=%s, precio=%s,
                    stock=%s, categoria=%s
                WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, id))

        mysql.connection.commit()
        cur.close()

        flash('Producto actualizado correctamente.', 'success')
        return redirect('/admin')

    cur.execute("SELECT * FROM productos WHERE id=%s", (id,))
    producto = cur.fetchone()

    cur.close()

    return render_template('editar_producto.html', producto=producto, categorias=CATEGORIAS)


# ─────────────────────────────────────────────

@app.route('/eliminar_producto/<int:id>')
def eliminar_producto(id):

    cur = mysql.connection.cursor()

    cur.execute("""
        UPDATE productos
        SET estado='inactivo'
        WHERE id=%s
    """, (id,))

    mysql.connection.commit()
    cur.close()

    return redirect('/admin')


# ─────────────────────────────────────────────

@app.route('/activar_producto/<int:id>')
def activar_producto(id):

    cur = mysql.connection.cursor()

    # SOLO ACTIVAR (SIN LÓGICA COMPLEJA)
    cur.execute("""
        UPDATE productos
        SET estado='activo'
        WHERE id=%s
    """, (id,))

    mysql.connection.commit()
    cur.close()

    return redirect('/admin')
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
    sql = "SELECT * FROM productos WHERE nombre LIKE %s AND estado='activo' AND stock > 0"      #modificado 27/07/26 para productos con stock 0 desaparezcan automáticamente de la tienda
    vals = [f'%{buscar}%']
    if categoria != 'Todos':
        sql += ' AND categoria=%s'
        vals.append(categoria)
    cur.execute(sql, tuple(vals))
    productos = cur.fetchall()
    return render_template('index.html', productos=productos, categorias=CATEGORIAS)

@app.route('/agregar/<int:id>')
def agregar(id):

    usuario = obtener_usuario()
    if not usuario:
        return redirect('/login')

    cur = mysql.connection.cursor()

    # Ver si el producto ya está en el carrito
    cur.execute("""
        SELECT * FROM carrito 
        WHERE usuario_id=%s AND producto_id=%s
    """, (usuario, id))

    item = cur.fetchone()

    if item:
        # Si existe, aumentar cantidad
        cur.execute("""
            UPDATE carrito 
            SET cantidad = cantidad + 1
            WHERE usuario_id=%s AND producto_id=%s
        """, (usuario, id))
    else:
        # Si no existe, insertar nuevo
        cur.execute("""
            INSERT INTO carrito (usuario_id, producto_id, cantidad)
            VALUES (%s, %s, 1)
        """, (usuario, id))

    mysql.connection.commit()

    flash('Producto agregado al carrito', 'success')
    return redirect('/')

@app.route('/carrito')
def ver_carrito():

    usuario = obtener_usuario()
    if not usuario:
        return redirect('/login')

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT c.id, c.producto_id, p.nombre, p.precio, c.cantidad
        FROM carrito c 
        JOIN productos p ON c.producto_id = p.id
        WHERE c.usuario_id = %s
    """, (usuario,))

    productos = cur.fetchall()

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
    cur.execute("SELECT c.cantidad, p.precio FROM carrito c JOIN productos p ON c.producto_id=p.id WHERE c.producto_id=%s AND c.usuario_id=%s", (id_producto, usuario))
    fila = cur.fetchone()
    cur.execute("SELECT SUM(c.cantidad*p.precio) AS total FROM carrito c JOIN productos p ON c.producto_id=p.id WHERE c.usuario_id=%s", (usuario,))
    res = cur.fetchone()
    total = res['total'] if res['total'] else 0
    cur.close()
    if fila:
        return jsonify({'eliminado':False,'cantidad':fila['cantidad'],'subtotal':round(fila['cantidad']*float(fila['precio']),2),'total':round(float(total),2)})
    return jsonify({'eliminado':True,'total':round(float(total),2)})

@app.route('/eliminar_carrito/<int:id>')
def eliminar_carrito(id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM carrito WHERE id=%s", (id,))
    mysql.connection.commit()
    return redirect('/carrito')

# ─────────────────────────────────────────────
# COMPRA
# ─────────────────────────────────────────────
@app.route('/comprar')
def comprar():

    print("SESSION COMPLETA:", session)

    if 'user_id' not in session:
        return redirect('/login')

    user_id = int(session['user_id'])

    print("USER ID:", user_id)

    cur = mysql.connection.cursor()

    cur.execute("SELECT * FROM carrito WHERE usuario_id=%s", (user_id,))
    carrito = cur.fetchall()

    print("CARRITO:", carrito)

    if not carrito:
        return "Tu carrito está vacío"

    return redirect('/procesar_compra')

@app.route('/procesar_compra')
def procesar_compra():
    if 'user_id' not in session:
        return redirect('/login')
    user_id = int(session['user_id'])
    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT p.id, p.nombre, p.precio, c.cantidad
        FROM carrito c JOIN productos p ON c.producto_id=p.id
        WHERE c.usuario_id=%s
    """, (user_id,))
    items = cur.fetchall()
    if not items:
        return "Carrito vacío"

    total = 0
    for item in items:
        cur.execute("SELECT stock FROM productos WHERE id=%s", (item['id'],))
        stock = cur.fetchone()['stock']
        if stock < item['cantidad']:
            return f"Stock insuficiente: {item['nombre']}"
        total += item['precio'] * item['cantidad']

    cur.execute("INSERT INTO ventas (cliente_id, total) VALUES(%s,%s)", (user_id, total))
    venta_id = cur.lastrowid

    for item in items:
        cur.execute("INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio) VALUES(%s,%s,%s,%s)",
                    (venta_id, item['id'], item['cantidad'], item['precio']))
        cur.execute("UPDATE productos SET stock=stock-%s WHERE id=%s", (item['cantidad'], item['id']))
        # Verificar stock bajo después de cada compra
        verificar_stock_bajo(cur, item['id'])

    cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (user_id,))
    mysql.connection.commit()

    # Auto-enviar boleta al cliente por email
    correo_cliente = session.get('correo','')
    if correo_cliente:
        enviar_boleta_cliente(correo_cliente, venta_id)

    return redirect(f'/confirmacion/{venta_id}')

# ─────────────────────────────────────────────
# BOLETA
# ─────────────────────────────────────────────
@app.route('/boleta')
def boleta():
    if 'user_id' not in session:
        return redirect('/login')
    return render_template('boleta.html')

@app.route('/boleta/<int:venta_id>')
def boleta_form(venta_id):
    return render_template('boleta_form.html', venta_id=venta_id)

@app.route('/preview_boleta')
def preview_boleta():
    venta_id = request.args.get('venta_id')
    doc      = request.args.get('doc')
    nombre   = request.args.get('nombre')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d JOIN productos p ON d.producto_id=p.id
        WHERE d.venta_id=%s
    """, (venta_id,))
    productos = cur.fetchall()
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
        cur.execute("""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['id'],))
        hist.append({'id':v['id'],'total':v['total'],'fecha':v['fecha'],'productos':cur.fetchall()})
    cur.close()
    return render_template('historial.html', historial=hist)

# ─────────────────────────────────────────────
# ELIMINAR HISTORIAL DE COMPRAS
# ─────────────────────────────────────────────
@app.route('/eliminar_historial')
def eliminar_historial():

    # SOLO ADMIN
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')

    cur = mysql.connection.cursor()

    # ELIMINAR DETALLE DE VENTAS
    cur.execute("DELETE FROM detalle_venta")

    # ELIMINAR VENTAS
    cur.execute("DELETE FROM ventas")

    mysql.connection.commit()
    cur.close()

    return redirect('/historial-compras')


@app.route('/historial-compras')
def historial_compras():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/')
    buscar = request.args.get('buscar','')
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT v.id, u.correo, u.id AS cliente_id, v.total, v.fecha, v.documento, v.nombre AS titular
        FROM ventas v JOIN usuarios u ON v.cliente_id=u.id
        WHERE u.correo LIKE %s ORDER BY v.fecha DESC
    """, (f'%{buscar}%',))
    ventas_raw = cur.fetchall()
    historial = []
    for v in ventas_raw:
        cur.execute("""
            SELECT p.id AS producto_id, p.nombre, p.descripcion, d.cantidad, d.precio
            FROM detalle_venta d JOIN productos p ON d.producto_id=p.id
            WHERE d.venta_id=%s
        """, (v['id'],))
        historial.append({**v, 'productos': cur.fetchall()})
    cur.close()
    return render_template('historial_compras_admin.html', historial=historial, buscar=buscar)
# ─────────────────────────────────────────────
# ELIMINAR COMPRA DEL CLIENTE
# ─────────────────────────────────────────────
@app.route('/eliminar_compra/<int:id>')
def eliminar_compra(id):

    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    cur = mysql.connection.cursor()

    # VERIFICAR QUE LA COMPRA PERTENECE AL USUARIO
    cur.execute("""
        SELECT id
        FROM ventas
        WHERE id=%s AND cliente_id=%s
    """, (id, user_id))

    venta = cur.fetchone()

    if venta:

        # ELIMINAR DETALLES
        cur.execute(
            "DELETE FROM detalle_venta WHERE venta_id=%s",
            (id,)
        )

        # ELIMINAR VENTA
        cur.execute(
            "DELETE FROM ventas WHERE id=%s",
            (id,)
        )

        mysql.connection.commit()

    cur.close()

    return redirect('/historial')


# ─────────────────────────────────────────────
# PERMISOS
# ─────────────────────────────────────────────
@app.route('/permisos', methods=['GET','POST'])
def permisos():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    if request.method == 'POST':
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
        SELECT id, correo, rol FROM usuarios WHERE correo LIKE %s
        ORDER BY CASE rol WHEN 'admin' THEN 0 WHEN 'administrador' THEN 1 ELSE 2 END, correo ASC
    """, (f'%{buscar}%',))
    usuarios = cur.fetchall()
    cur.close()
    return render_template('permisos.html', usuarios=usuarios, buscar=buscar)

# ─────────────────────────────────────────────
# PROVEEDORES
# ─────────────────────────────────────────────
@app.route('/proveedores', methods=['GET','POST'])
def proveedores():
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        nombre    = request.form.get('nombre','')
        celular   = request.form.get('celular','')
        correo    = request.form.get('correo','')
        dni       = request.form.get('dni','')
        ruc       = request.form.get('ruc','')
        direccion = request.form.get('direccion','')
        categoria = request.form.get('categoria','')
        notas     = request.form.get('notas','')
        cur.execute("""
            INSERT INTO proveedores (nombre, celular, correo, dni, ruc, direccion, categoria, notas)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (nombre, celular, correo, dni, ruc, direccion, categoria, notas))
        mysql.connection.commit()
        flash(f'Proveedor "{nombre}" agregado correctamente.', 'success')
        return redirect('/proveedores')

    buscar = request.args.get('buscar','')
    cat_filtro = request.args.get('categoria','')
    sql = "SELECT * FROM proveedores WHERE nombre LIKE %s"
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
        cur.execute("""
            UPDATE proveedores SET nombre=%s, celular=%s, correo=%s, dni=%s,
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
    cur.execute("SELECT * FROM proveedores WHERE id=%s", (id,))
    p = cur.fetchone()
    cur.close()
    return render_template('proveedores.html', editar=p, categorias=CATEGORIAS,
                            proveedores=[], buscar='', cat_filtro='')

@app.route('/proveedores/eliminar/<int:id>')
def eliminar_proveedor(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM proveedores WHERE id=%s", (id,))
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
    cur.execute("""
        SELECT pp.id, pp.cantidad_pedido, pp.fecha, pp.estado,
               p.nombre AS producto_nombre, p.stock AS stock_actual,
               p.categoria,
               pr.id AS proveedor_id, pr.nombre AS proveedor_nombre,
               pr.celular AS proveedor_celular, pr.correo AS proveedor_correo
        FROM productos_para_pedir pp
        JOIN productos p ON pp.producto_id=p.id
        LEFT JOIN proveedores pr ON pp.proveedor_id=pr.id
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
    cur.execute("UPDATE productos_para_pedir SET estado='cancelado' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Pedido eliminado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/actualizar/<int:id>', methods=['POST'])
def actualizar_pedido(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cantidad = int(request.form.get('cantidad', 1))
    if cantidad < 1:
        cantidad = 1
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET cantidad_pedido=%s WHERE id=%s", (cantidad, id))
    mysql.connection.commit()
    cur.close()
    flash('Cantidad actualizada.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/marcar-enviado/<int:id>')
def marcar_enviado(id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado='enviado' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Pedido marcado como enviado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/enviar-email/<int:proveedor_id>')
def enviar_email_a_proveedor(proveedor_id):
    if 'rol' not in session or session['rol'] not in ['admin','administrador']:
        return redirect('/login')
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM proveedores WHERE id=%s", (proveedor_id,))
    proveedor = cur.fetchone()
    if not proveedor:
        flash('Proveedor no encontrado.', 'danger')
        return redirect('/productos-para-pedir')
    cur.execute("""
        SELECT p.nombre, p.categoria, pp.cantidad_pedido
        FROM productos_para_pedir pp
        JOIN productos p ON pp.producto_id=p.id
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
# RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
