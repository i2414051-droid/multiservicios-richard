from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from functools import wraps
from flask import flash
import uuid 
import requests
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet
from flask import send_file
import os
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
def obtener_ip():
    return request.remote_addr


TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6Impob3NzdWVicnlhbkBnbWFpbC5jb20ifQ.VvkKvQL_se-h31zZ87zXwBzH6lYy3wLb4pD0XCmhN5o"

app = Flask(__name__)
app.secret_key = "clave_secreta_123"
bcrypt = Bcrypt(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def obtener_usuario():
    if 'user_id' in session:
        return session['user_id']

    if 'guest_id' not in session:
        session['guest_id'] = str(uuid.uuid4())

    return session['guest_id']

# CONFIG MYSQL
app.config['MYSQL_HOST'] = 'mysql-proyecto.alwaysdata.net'
app.config['MYSQL_USER'] = 'proyecto'
app.config['MYSQL_PASSWORD'] = 'B12345678Jhoss'
app.config['MYSQL_DB'] = 'proyecto_multiservicios_richard'
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

mysql = MySQL(app)

@app.context_processor
def cantidad_carrito():

    try:
        usuario = obtener_usuario()

        cur = mysql.connection.cursor()

        cur.execute("""
            SELECT SUM(cantidad) AS total
            FROM carrito
            WHERE usuario_id=%s
        """, (usuario,))

        resultado = cur.fetchone()

        total = resultado['total'] if resultado['total'] else 0

        return dict(cantidad_carrito=total)

    except:
        return dict(cantidad_carrito=0)

carrito = []

@app.route('/test_db')
def test_db():
    try:
        cur = mysql.connection.cursor()
        cur.execute("SHOW TABLES;")
        return str(cur.fetchall())
    except Exception as e:
        return f"ERROR: {e}"

@app.route('/login', methods=['GET', 'POST'])
def login():

    ip = obtener_ip()

    if request.method == 'POST':

        correo = request.form['correo']
        password = request.form['password']

        cur = mysql.connection.cursor()

        # ======================================
        # VERIFICAR BLOQUEO IP
        # ======================================

        cur.execute("""
            SELECT * FROM bloqueos_ip
            WHERE ip=%s
        """, (ip,))

        bloqueo_ip = cur.fetchone()

        if bloqueo_ip and bloqueo_ip['bloqueado_hasta']:

            ahora = datetime.now()

            if ahora < bloqueo_ip['bloqueado_hasta']:

                restante = bloqueo_ip['bloqueado_hasta'] - ahora

                minutos = restante.seconds // 60
                segundos = restante.seconds % 60

                flash(
                    f'IP bloqueada. Intenta nuevamente en {minutos}m {segundos}s',
                    'danger'
                )

                return redirect('/login')

            else:

                cur.execute("""
                    DELETE FROM bloqueos_ip
                    WHERE ip=%s
                """, (ip,))

                mysql.connection.commit()

        # ======================================
        # VERIFICAR BLOQUEO USUARIO
        # ======================================

        cur.execute("""
            SELECT * FROM intentos_usuario
            WHERE correo=%s
        """, (correo,))

        bloqueo_usuario = cur.fetchone()

        if bloqueo_usuario and bloqueo_usuario['bloqueado_hasta']:

            ahora = datetime.now()

            if ahora < bloqueo_usuario['bloqueado_hasta']:

                restante = bloqueo_usuario['bloqueado_hasta'] - ahora

                minutos = restante.seconds // 60
                segundos = restante.seconds % 60

                flash(
                    f'Usuario bloqueado. Intenta nuevamente en {minutos}m {segundos}s',
                    'danger'
                )

                return redirect('/login')

            else:

                cur.execute("""
                    DELETE FROM intentos_usuario
                    WHERE correo=%s
                """, (correo,))

                mysql.connection.commit()

                bloqueo_usuario = None

        # ======================================
        # BUSCAR USUARIO
        # ======================================

        cur.execute("""
            SELECT * FROM usuarios
            WHERE correo=%s
        """, (correo,))

        usuario = cur.fetchone()

        # ======================================
        # LOGIN CORRECTO
        # ======================================

        if usuario and bcrypt.check_password_hash(usuario['password'], password):

            cur.execute("""
                DELETE FROM intentos_usuario
                WHERE correo=%s
            """, (correo,))

            mysql.connection.commit()

            session['user_id'] = usuario['id']
            session['correo'] = usuario['correo']
            session['rol'] = usuario['rol'].lower()

            flash('Bienvenido', 'success')

            if session['rol'] in ['admin', 'administrador']:
                return redirect('/admin')

            return redirect('/')

        # ======================================
        # LOGIN INCORRECTO
        # ======================================

        if bloqueo_usuario:

            intentos = bloqueo_usuario['intentos'] + 1

            restantes = 3 - intentos

            # ======================================
            # BLOQUEAR USUARIO
            # ======================================

            if intentos >= 3:

                bloqueo_hasta = datetime.now() + timedelta(minutes=5)

                cur.execute("""
                    UPDATE intentos_usuario
                    SET intentos=%s,
                        bloqueado_hasta=%s
                    WHERE correo=%s
                """, (intentos, bloqueo_hasta, correo))

                flash(
                    'Usuario bloqueado por 5 minutos.',
                    'danger'
                )

                # ======================================
                # CONTROL DE IP SOSPECHOSA
                # ======================================

                if bloqueo_ip:

                    usuarios_diferentes = bloqueo_ip['usuarios_diferentes'] + 1

                    # 🔥 SI YA SON 2 USUARIOS BLOQUEADOS
                    # EL TERCERO BLOQUEA LA IP

                    if usuarios_diferentes >= 2:

                        bloqueo_ip_hasta = datetime.now() + timedelta(minutes=10)

                        cur.execute("""
                            UPDATE bloqueos_ip
                            SET usuarios_diferentes=%s,
                                bloqueado_hasta=%s
                            WHERE ip=%s
                        """, (
                            usuarios_diferentes,
                            bloqueo_ip_hasta,
                            ip
                        ))

                        flash(
                            'IP bloqueada por actividad sospechosa.',
                            'danger'
                        )

                    else:

                        cur.execute("""
                            UPDATE bloqueos_ip
                            SET usuarios_diferentes=%s
                            WHERE ip=%s
                        """, (
                            usuarios_diferentes,
                            ip
                        ))

                else:

                    cur.execute("""
                        INSERT INTO bloqueos_ip(
                            ip,
                            usuarios_diferentes
                        )
                        VALUES(%s,1)
                    """, (ip,))

            else:

                cur.execute("""
                    UPDATE intentos_usuario
                    SET intentos=%s
                    WHERE correo=%s
                """, (intentos, correo))

                flash(
                    f'Credenciales incorrectas. Te quedan {restantes} intento(s).',
                    'warning'
                )

        else:

            cur.execute("""
                INSERT INTO intentos_usuario(
                    correo,
                    intentos
                )
                VALUES(%s,1)
            """, (correo,))

            flash(
                'Credenciales incorrectas. Te quedan 2 intento(s).',
                'warning'
            )

        mysql.connection.commit()

        return redirect('/login')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/admin')
def admin():
    print("SESSION:", session)  #  DEBUG
    if 'rol' not in session or session['rol'] not in ['admin', 'administrador']:
        return " Acceso denegado"

    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos")
    productos = cur.fetchall()

    return render_template('admin.html', productos=productos)

@app.route("/consultar/<tipo>/<numero>")
def consultar(tipo, numero):

    venta_id = request.args.get("venta_id")

    if not venta_id:
        return jsonify({"error": "venta_id no recibido"})

    if tipo not in ["dni", "ruc"]:
        return jsonify({"error": "Tipo inválido"})

    url = f"https://dniruc.apisperu.com/api/v1/{tipo}/{numero}?token={TOKEN}"

    try:
        response = requests.get(url)
        data = response.json()

        if "error" in data:
            return jsonify(data)

        cursor = mysql.connection.cursor()

        if tipo == "dni":
            nombre = f"{data.get('nombres','')} {data.get('apellidoPaterno','')} {data.get('apellidoMaterno','')}"
        else:
            nombre = data.get("razonSocial","")

        cursor.execute("""
            UPDATE ventas
            SET documento = %s,
                nombre = %s
            WHERE id = %s
        """, (numero, nombre, venta_id))

        mysql.connection.commit()
        cursor.close()

        return jsonify(data)

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": str(e)})

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        correo = request.form['correo']
        password = request.form['password']
        confirmar = request.form['confirmar']

        if password != confirmar:
            return " Contraseñas no coinciden"

        cur = mysql.connection.cursor()

        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        if cur.fetchone():
            return " Usuario ya existe"

        hash = bcrypt.generate_password_hash(password).decode('utf-8')

        #  SIEMPRE CLIENTE
        cur.execute("INSERT INTO usuarios (correo, password, rol) VALUES (%s,%s,%s)",
                    (correo, hash, 'cliente'))
        mysql.connection.commit()

        return redirect('/login')

    return render_template('registro.html')

@app.route('/agregar_producto', methods=['POST'])
def agregar_producto():

    if 'rol' not in session or session['rol'] not in ['admin', 'administrador']:
        return redirect('/login')

    nombre = request.form['nombre']
    descripcion = request.form['descripcion']
    precio = float(request.form['precio'])
    if precio < 0:
        flash('No se permiten precios negativos', 'danger')
        return redirect('/admin')
    stock = request.form['stock']

    imagen = request.files['imagen']

    if imagen and imagen.filename != '':
        filename = secure_filename(imagen.filename)
        ruta = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        imagen.save(ruta)
        imagen_db = 'uploads/' + filename
    else:
        imagen_db = None

    cur = mysql.connection.cursor()
    cur.execute("""
        INSERT INTO productos (nombre, descripcion, precio, stock, imagen)
        VALUES (%s, %s, %s, %s, %s)
    """, (nombre, descripcion, precio, stock, imagen_db))

    mysql.connection.commit()

    return redirect('/admin')

@app.route('/editar_producto/<int:id>', methods=['GET','POST'])
def editar_producto(id):

    # =========================
    # PROTECCIÓN DE ROL
    # =========================
    if 'rol' not in session or session['rol'] not in ['admin', 'administrador']:
        return redirect('/login')

    cur = mysql.connection.cursor()

    if request.method == 'POST':

        nombre = request.form['nombre']

        descripcion = request.form['descripcion']

        precio = float(request.form['precio'])

        stock = int(request.form['stock'])

        categoria = request.form['categoria']

        imagen = request.files['imagen']

        # =========================
        # VALIDACIONES
        # =========================

        if precio < 0:
            flash('No se permiten precios negativos.', 'danger')
            return redirect(f'/editar_producto/{id}')

        if stock < 0:
            flash('No se permiten valores negativos en el stock.', 'danger')
            return redirect(f'/editar_producto/{id}')

        # =========================
        # SI SUBE NUEVA IMAGEN
        # =========================

        if imagen and imagen.filename != '':

            filename = secure_filename(imagen.filename)

            ruta = os.path.join(
                app.config['UPLOAD_FOLDER'],
                filename
            )

            imagen.save(ruta)

            cur.execute("""
                UPDATE productos 
                SET nombre=%s,
                    descripcion=%s,
                    precio=%s,
                    stock=%s,
                    categoria=%s,
                    imagen=%s
                WHERE id=%s
            """, (
                nombre,
                descripcion,
                precio,
                stock,
                categoria,
                'uploads/' + filename,
                id
            ))

        else:

            # =========================
            # SIN CAMBIAR IMAGEN
            # =========================

            cur.execute("""
                UPDATE productos 
                SET nombre=%s,
                    descripcion=%s,
                    precio=%s,
                    stock=%s,
                    categoria=%s
                WHERE id=%s
            """, (
                nombre,
                descripcion,
                precio,
                stock,
                categoria,
                id
            ))

        mysql.connection.commit()

        cur.close()

        flash('Producto actualizado correctamente.', 'success')

        return redirect('/admin')


    # =========================
    # GET — cargar datos del producto
    # =========================

    cur.execute(
        "SELECT * FROM productos WHERE id=%s",
        (id,)
    )

    producto = cur.fetchone()

    return render_template(
        'editar_producto.html',
        producto=producto
    )

@app.route('/eliminar_producto/<int:id>')
def eliminar_producto(id):

    cur = mysql.connection.cursor()

    cur.execute(
        "UPDATE productos SET estado='inactivo' WHERE id=%s",
        (id,)
    )

    mysql.connection.commit()

    cur.close()

    return redirect('/admin')

@app.route('/activar_producto/<int:id>')
def activar_producto(id):

    cur = mysql.connection.cursor()

    cur.execute(
        "UPDATE productos SET estado='activo' WHERE id=%s",
        (id,)
    )

    mysql.connection.commit()

    cur.close()

    return redirect('/admin')

# INICIO
@app.route('/')
def index():

    buscar = request.args.get('buscar', '')

    categoria = request.args.get('categoria', 'Todos')

    cur = mysql.connection.cursor()

    sql = """
        SELECT *
        FROM productos
        WHERE nombre LIKE %s
        AND estado='activo'
    """

    valores = [f"%{buscar}%"]

    if categoria != "Todos":

        sql += " AND categoria=%s"

        valores.append(categoria)

    cur.execute(sql, tuple(valores))

    productos = cur.fetchall()

    return render_template(
        'index.html',
        productos=productos
    )

# AGREGAR AL CARRITO
@app.route('/agregar/<int:id>')
def agregar(id):
    usuario = obtener_usuario()  #  CLAVE (usuario o invitado)

    cur = mysql.connection.cursor()

    # Verificar si ya existe en carrito
    cur.execute("SELECT * FROM carrito WHERE usuario_id=%s AND producto_id=%s", (usuario, id))
    item = cur.fetchone()

    if item:
        cur.execute("""
            UPDATE carrito 
            SET cantidad = cantidad + 1 
            WHERE usuario_id=%s AND producto_id=%s
        """, (usuario, id))
    else:
        cur.execute("""
            INSERT INTO carrito (usuario_id, producto_id, cantidad)
            VALUES (%s,%s,1)
        """, (usuario, id))

    mysql.connection.commit()
    flash(
        ' Producto agregado al carrito',
        'success'
    )

    return redirect('/')

# VER CARRITO
@app.route('/carrito')
def ver_carrito():
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT c.id, c.producto_id, p.nombre, p.precio, c.cantidad
        FROM carrito c
        JOIN productos p ON c.producto_id = p.id
        WHERE c.usuario_id=%s
    """, (usuario,))

    productos = cur.fetchall()

    # ✅ CORREGIDO
    total = 0
    for p in productos:
        total += p['precio'] * p['cantidad']

    return render_template('carrito.html', productos=productos, total=total)

# ELIMINAR

@app.route('/aumentar-cantidad/<int:id_producto>')
def aumentar_cantidad(id_producto):
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute("""
        UPDATE carrito
        SET cantidad = cantidad + 1
        WHERE producto_id=%s AND usuario_id=%s
    """, (id_producto, usuario))
    mysql.connection.commit()
    return redirect('/carrito')

@app.route('/reducir-cantidad/<int:id_producto>')
def reducir_cantidad(id_producto):
    usuario = obtener_usuario()
    cur = mysql.connection.cursor()
    cur.execute("""
        UPDATE carrito
        SET cantidad = cantidad - 1
        WHERE producto_id=%s AND usuario_id=%s
    """, (id_producto, usuario))
    cur.execute("DELETE FROM carrito WHERE cantidad <= 0")
    mysql.connection.commit()
    return redirect('/carrito')

# AJAX - actualizar cantidad sin recargar página
@app.route('/actualizar-cantidad/<int:id_producto>', methods=['POST'])
def actualizar_cantidad(id_producto):
    usuario = obtener_usuario()
    accion  = request.json.get('accion')  # 'aumentar' | 'reducir'

    cur = mysql.connection.cursor()

    if accion == 'aumentar':
        cur.execute("""
            UPDATE carrito
            SET cantidad = cantidad + 1
            WHERE producto_id=%s AND usuario_id=%s
        """, (id_producto, usuario))

    elif accion == 'reducir':
        cur.execute("""
            UPDATE carrito
            SET cantidad = cantidad - 1
            WHERE producto_id=%s AND usuario_id=%s
        """, (id_producto, usuario))
        cur.execute("""
            DELETE FROM carrito
            WHERE producto_id=%s AND usuario_id=%s AND cantidad <= 0
        """, (id_producto, usuario))

    mysql.connection.commit()

    cur.execute("""
        SELECT c.cantidad, p.precio
        FROM carrito c
        JOIN productos p ON c.producto_id = p.id
        WHERE c.producto_id=%s AND c.usuario_id=%s
    """, (id_producto, usuario))
    fila = cur.fetchone()

    cur.execute("""
        SELECT SUM(c.cantidad * p.precio) AS total
        FROM carrito c
        JOIN productos p ON c.producto_id = p.id
        WHERE c.usuario_id=%s
    """, (usuario,))
    resultado = cur.fetchone()
    total = resultado['total'] if resultado['total'] else 0
    cur.close()

    if fila:
        return jsonify({
            'eliminado': False,
            'cantidad':  fila['cantidad'],
            'subtotal':  round(fila['cantidad'] * float(fila['precio']), 2),
            'total':     round(float(total), 2)
        })
    else:
        return jsonify({'eliminado': True, 'total': round(float(total), 2)})

@app.route('/eliminar_carrito/<int:id>')
def eliminar_carrito(id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM carrito WHERE id=%s", (id,))
    mysql.connection.commit()

    return redirect('/carrito')

# COMPRAR
@app.route('/comprar')
def comprar():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = int(session['user_id'])
    cur = mysql.connection.cursor()

    #  MOVER CARRITO INVITADO
    if 'guest_id' in session:
        guest_id = session['guest_id']

        cur.execute("""
            UPDATE carrito 
            SET usuario_id=%s 
            WHERE usuario_id=%s
        """, (user_id, guest_id))

        mysql.connection.commit()

    # Verificar carrito
    cur.execute("""
        SELECT * FROM carrito WHERE usuario_id=%s
    """, (user_id,))
    
    items = cur.fetchall()

    if not items:
        return " Tu carrito está vacío"

    #  AHORA VA AL FORMULARIO DNI/RUC
    return redirect(f'/procesar_compra?doc=&nombre=')

@app.route('/procesar_compra')
def procesar_compra():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = int(session['user_id'])

    cur = mysql.connection.cursor()

    # Obtener carrito
    cur.execute("""
        SELECT p.id, p.nombre, p.precio, c.cantidad
        FROM carrito c
        JOIN productos p ON c.producto_id = p.id
        WHERE c.usuario_id=%s
    """, (user_id,))

    items = cur.fetchall()

    if not items:
        return "Carrito vacío"

    total = 0

    #  VALIDAR STOCK
    for item in items:
        cur.execute("SELECT stock FROM productos WHERE id=%s", (item['id'],))
        stock = cur.fetchone()['stock']

        if stock < item['cantidad']:
            return f" Stock insuficiente: {item['nombre']}"

        total += item['precio'] * item['cantidad']

    #  INSERT VENTA
    cur.execute("""
        INSERT INTO ventas (cliente_id, total)
        VALUES (%s,%s)
    """, (user_id, total))

    venta_id = cur.lastrowid

    # ✅ DETALLE + ACTUALIZAR STOCK
    for item in items:
        cur.execute("""
            INSERT INTO detalle_venta (venta_id, producto_id, cantidad, precio)
            VALUES (%s,%s,%s,%s)
        """, (venta_id, item['id'], item['cantidad'], item['precio']))

        cur.execute("""
            UPDATE productos
            SET stock = stock - %s
            WHERE id=%s
        """, (item['cantidad'], item['id']))

    # ✅ LIMPIAR CARRITO
    cur.execute("DELETE FROM carrito WHERE usuario_id=%s", (user_id,))
    mysql.connection.commit()

    return redirect(f'/confirmacion/{venta_id}')

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
    doc = request.args.get('doc')
    nombre = request.args.get('nombre')

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d
        JOIN productos p ON d.producto_id = p.id
        WHERE d.venta_id=%s
    """, (venta_id,))

    productos = cur.fetchall()

    total = sum(p['cantidad'] * p['precio'] for p in productos)

    return render_template('preview_boleta.html',
        productos=productos,
        total=total,
        doc=doc,
        nombre=nombre,
        venta_id=venta_id
    )

@app.route('/guardar_boleta')
def guardar_boleta():
    venta_id = request.args.get('venta_id')
    doc = request.args.get('doc')
    nombre = request.args.get('nombre')

    cur = mysql.connection.cursor()

    cur.execute("""
        UPDATE ventas 
        SET documento=%s, nombre=%s
        WHERE id=%s
    """, (doc, nombre, venta_id))

    mysql.connection.commit()

    return redirect(f'/boleta_pdf/{venta_id}')

@app.route('/confirmacion/<int:id>')
def confirmacion(id):
    cur = mysql.connection.cursor()

    cur.execute("SELECT total, fecha FROM ventas WHERE id=%s", (id,))
    venta = cur.fetchone()

    return render_template('confirmacion.html', venta=venta, id=id)

@app.route('/historial')
def historial():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']
    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT id, total, fecha
        FROM ventas
        WHERE cliente_id=%s
        ORDER BY fecha DESC
    """, (user_id,))

    ventas = cur.fetchall()

    historial = []
    for v in ventas:
        cur.execute("""
            SELECT p.id AS producto_id,
                   p.nombre,
                   p.descripcion,
                   d.cantidad,
                   d.precio
            FROM detalle_venta d
            JOIN productos p ON d.producto_id = p.id
            WHERE d.venta_id=%s
        """, (v['id'],))

        productos = cur.fetchall()

        historial.append({
            'id':        v['id'],
            'total':     v['total'],
            'fecha':     v['fecha'],
            'productos': productos
        })

    cur.close()
    return render_template('historial.html', historial=historial)

@app.route('/boleta_pdf/<int:venta_id>')
def boleta_pdf(venta_id):

    cur = mysql.connection.cursor()

    # Obtener datos venta
    cur.execute("SELECT documento, nombre, total, fecha FROM ventas WHERE id=%s", (venta_id,))
    venta = cur.fetchone()

    if not venta:
        return "Venta no encontrada"

    documento, nombre, total, fecha = venta

    # Obtener detalle
    cur.execute("""
        SELECT p.nombre, d.cantidad, d.precio
        FROM detalle_venta d
        JOIN productos p ON d.producto_id = p.id
        WHERE d.venta_id=%s
    """, (venta_id,))

    productos = cur.fetchall()
    total = sum(p['cantidad'] * p['precio'] for p in productos)

    # Crear PDF
    filename = f"boleta_{venta_id}.pdf"
    filepath = os.path.join("static", filename)

    doc = SimpleDocTemplate(filepath)
    elements = []

    styles = getSampleStyleSheet()

    elements.append(Paragraph("<b>MULTISERVICIOS RICHARD</b>", styles['Title']))
    elements.append(Paragraph("RUC: 20123456789", styles['Normal']))
    elements.append(Spacer(1, 0.3 * inch))

    elements.append(Paragraph(f"<b>Boleta N°:</b> {venta_id}", styles['Normal']))
    elements.append(Paragraph(f"<b>Cliente:</b> {nombre}", styles['Normal']))
    elements.append(Paragraph(f"<b>DNI/RUC:</b> {documento}", styles['Normal']))
    elements.append(Paragraph(f"<b>Fecha:</b> {fecha}", styles['Normal']))
    elements.append(Spacer(1, 0.3 * inch))

    # Tabla productos
    data = [["Producto", "Cant.", "Precio", "Subtotal"]]

    for p in productos:
        subtotal = p['cantidad'] * p['precio']
        data.append([
            p['nombre'],
            p['cantidad'],
            f"S/ {p['precio']:.2f}",
            f"S/ {subtotal:.2f}"
        ])

    data.append(["", "", "TOTAL", f"S/ {total:.2f}"])

    table = Table(data, colWidths=[200, 60, 80, 80])

    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('ALIGN', (1,1), (-1,-1), 'CENTER'),
    ]))

    elements.append(table)

    elements.append(Spacer(1, 0.5 * inch))
    elements.append(Paragraph("Gracias por su compra", styles['Normal']))

    doc.build(elements)

    return send_file(filepath, as_attachment=True)


@app.route('/historial-compras')
def historial_compras():
    if 'rol' not in session or session['rol'] not in ['admin', 'administrador']:
        return redirect('/')

    buscar = request.args.get('buscar', '')
    cur = mysql.connection.cursor()

    # Obtener ventas con datos del cliente
    cur.execute("""
        SELECT v.id, u.correo, u.id AS cliente_id,
               v.total, v.fecha, v.documento, v.nombre AS titular
        FROM ventas v
        JOIN usuarios u ON v.cliente_id = u.id
        WHERE u.correo LIKE %s
        ORDER BY v.fecha DESC
    """, (f'%{buscar}%',))

    ventas_raw = cur.fetchall()

    historial = []
    for v in ventas_raw:
        cur.execute("""
            SELECT p.id AS producto_id,
                   p.nombre,
                   p.descripcion,
                   d.cantidad,
                   d.precio
            FROM detalle_venta d
            JOIN productos p ON d.producto_id = p.id
            WHERE d.venta_id = %s
        """, (v['id'],))
        productos = cur.fetchall()
        historial.append({
            'id':         v['id'],
            'correo':     v['correo'],
            'cliente_id': v['cliente_id'],
            'titular':    v['titular'],
            'documento':  v['documento'],
            'total':      v['total'],
            'fecha':      v['fecha'],
            'productos':  productos
        })

    cur.close()
    return render_template('historial_compras_admin.html',
                           historial=historial,
                           buscar=buscar)


# ======================================
# PERMISOS
# ======================================

@app.route('/permisos', methods=['GET', 'POST'])
def permisos():
    if 'rol' not in session or session['rol'] not in ['admin', 'administrador']:
        return redirect('/login')

    cur = mysql.connection.cursor()

    if request.method == 'POST':
        user_id  = request.form.get('user_id')
        nuevo_rol = request.form.get('rol')

        if nuevo_rol in ['admin', 'cliente']:
            cur.execute(
                "UPDATE usuarios SET rol=%s WHERE id=%s",
                (nuevo_rol, user_id)
            )
            mysql.connection.commit()
            flash('Permisos actualizados correctamente.', 'success')
        else:
            flash('Rol no válido.', 'danger')

        cur.close()
        return redirect('/permisos')

    buscar = request.args.get('buscar', '')
    cur.execute("""
        SELECT id, correo, rol
        FROM usuarios
        WHERE correo LIKE %s
        ORDER BY
            CASE rol
                WHEN 'admin'         THEN 0
                WHEN 'administrador' THEN 1
                ELSE 2
            END,
            correo ASC
    """, (f'%{buscar}%',))

    usuarios = cur.fetchall()
    cur.close()
    return render_template('permisos.html', usuarios=usuarios, buscar=buscar)

if __name__ == '__main__':
    app.run(debug=True)