import os, requests as http_requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
import logging
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambiar-esta-clave-segura')
bcrypt = Bcrypt(app)

UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', '')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB_ALMACEN', 'bd_almacen')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

MS_VENTAS_URL = os.environ.get('MS_VENTAS_URL', 'http://localhost:5000')

app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER',   'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', '')
app.config['MAIL_TIMEOUT'] = int(os.environ.get('MAIL_TIMEOUT', 10))
mail = Mail(app)

CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']

logging.basicConfig(level=logging.ERROR)
import os, requests as http_requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
import logging
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambiar-esta-clave-segura')
bcrypt = Bcrypt(app)

UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', '')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB_ALMACEN', 'bd_almacen')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

MS_VENTAS_URL = os.environ.get('MS_VENTAS_URL', 'http://localhost:5000')

app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER',   'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', '')
app.config['MAIL_TIMEOUT'] = int(os.environ.get('MAIL_TIMEOUT', 10))
mail = Mail(app)

CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']

logging.basicConfig(level=logging.ERROR)

def init_db():
    try:
        cur = mysql.connection.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS productos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(200) NOT NULL,
            descripcion TEXT,
            precio DECIMAL(10,2) NOT NULL,
            stock INT DEFAULT 0,
            categoria VARCHAR(100),
            imagen VARCHAR(300),
            estado VARCHAR(20) DEFAULT 'activo',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        try:
            cur.execute("ALTER TABLE productos ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        except Exception:
            pass
        cur.execute("""CREATE TABLE IF NOT EXISTS proveedores (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(200) NOT NULL,
            celular VARCHAR(30),
            correo VARCHAR(200),
            dni VARCHAR(20),
            ruc VARCHAR(20),
            direccion VARCHAR(300),
            categoria VARCHAR(100),
            notas TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS productos_para_pedir (
            id INT AUTO_INCREMENT PRIMARY KEY,
            producto_id INT NOT NULL,
            cantidad_pedido INT DEFAULT 1,
            proveedor_id INT,
            estado VARCHAR(20) DEFAULT 'pendiente',
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (producto_id) REFERENCES productos(id),
            FOREIGN KEY (proveedor_id) REFERENCES proveedores(id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ingresos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            proveedor_id INT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total DECIMAL(10,2) DEFAULT 0,
            notas TEXT,
            FOREIGN KEY (proveedor_id) REFERENCES proveedores(id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS detalle_ingreso (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ingreso_id INT NOT NULL,
            producto_id INT NOT NULL,
            cantidad INT NOT NULL,
            precio DECIMAL(10,2) NOT NULL,
            FOREIGN KEY (ingreso_id) REFERENCES ingresos(id),
            FOREIGN KEY (producto_id) REFERENCES productos(id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS salidas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            venta_id INT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total DECIMAL(10,2) DEFAULT 0,
            notas TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS detalle_salida (
            id INT AUTO_INCREMENT PRIMARY KEY,
            salida_id INT NOT NULL,
            producto_id INT NOT NULL,
            cantidad INT NOT NULL,
            precio DECIMAL(10,2) NOT NULL,
            FOREIGN KEY (salida_id) REFERENCES salidas(id),
            FOREIGN KEY (producto_id) REFERENCES productos(id)
        )""")
        mysql.connection.commit()
        cur.close()
        print("[MS Almacen] Tablas creadas/verificadas OK")
    except Exception as e:
        print(f"[MS Almacen] Error creando tablas: {e}")

def api_ventas(endpoint):
    try:
        r = http_requests.get(f"{MS_VENTAS_URL}{endpoint}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[api_ventas] Error {endpoint}: {e}")
    return None

def verificar_stock_bajo(cur, producto_id):
    try:
        cur.execute("SELECT nombre, stock, categoria FROM productos WHERE id=%s", (producto_id,))
        p = cur.fetchone()
        if not p:
            return
        if p['stock'] <= 0:
            cur.execute("UPDATE productos SET estado='inactivo' WHERE id=%s", (producto_id,))
        if p['stock'] > 1:
            return
        cur.execute("SELECT id FROM productos_para_pedir WHERE producto_id=%s AND estado='pendiente'", (producto_id,))
        if cur.fetchone():
            return
        cur.execute("SELECT id, nombre, celular, correo FROM proveedores WHERE categoria=%s LIMIT 1", (p['categoria'],))
        proveedor = cur.fetchone()
        proveedor_id = proveedor['id'] if proveedor else None
        cur.execute("INSERT INTO productos_para_pedir (producto_id, cantidad_pedido, proveedor_id) VALUES (%s, 1, %s)",
                    (producto_id, proveedor_id))
    except Exception as e:
        print(f"[stock_bajo] {e}")

def enviar_email_proveedor(proveedor, productos_lista):
    try:
        if not proveedor.get('correo'):
            return False
        lineas = "\n".join([
            f"- {p['nombre']} (Categoria: {p.get('categoria','')}, Qty pedido: {p.get('cantidad_pedido',1)})"
            for p in productos_lista
        ])
        msg = Message(
            subject='Pedido de reabastecimiento - Multiservicios Richard',
            recipients=[proveedor['correo']],
            body=f"Estimado/a {proveedor['nombre']},\n\nLe informamos que los siguientes productos necesitan reabastecimiento:\n\n{lineas}\n\nPor favor contactenos para coordinar la entrega.\n\nMultiservicios Richard"
        )
        mail.send(msg)
        return True
    except Exception as e:
        print(f"[email_proveedor] {e}")
        return False

@app.context_processor
def inject_categorias():
    return dict(categorias=CATEGORIAS)

@app.route('/test_db')
def test_db():
    try:
        cur = mysql.connection.cursor()
        cur.execute("SHOW TABLES;")
        return jsonify({'tables': cur.fetchall(), 'service': 'ms_gestion_almacen'})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/init-db')
def ruta_init_db():
    init_db()
    flash('Tablas de almacen creadas/verificadas.', 'success')
    return redirect('/admin')

@app.route('/api/productos')
def api_productos():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos")
    data = cur.fetchall()
    cur.close()
    return jsonify(data)

@app.route('/api/productos/buscar')
def api_productos_buscar():
    buscar = request.args.get('buscar', '')
    categoria = request.args.get('categoria', 'Todos')
    cur = mysql.connection.cursor()
    sql = "SELECT * FROM productos WHERE nombre LIKE %s AND estado='activo'"
    vals = [f'%{buscar}%']
    if categoria != 'Todos':
        sql += ' AND categoria=%s'
        vals.append(categoria)
    cur.execute(sql, tuple(vals))
    data = cur.fetchall()
    cur.close()
    return jsonify(data)

@app.route('/api/productos/<int:producto_id>')
def api_producto(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos WHERE id=%s", (producto_id,))
    p = cur.fetchone()
    cur.close()
    if not p:
        return jsonify({'error': 'Producto no encontrado'}), 404
    return jsonify(p)

@app.route('/api/productos', methods=['POST'])
def api_producto_crear():
    data = request.json
    cur = mysql.connection.cursor()
    cur.execute("""INSERT INTO productos (nombre, descripcion, precio, stock, categoria, imagen)
        VALUES (%s,%s,%s,%s,%s,%s)""",
        (data['nombre'], data['descripcion'], data['precio'],
         data['stock'], data['categoria'], data.get('imagen')))
    mysql.connection.commit()
    nuevo_id = cur.lastrowid
    verificar_stock_bajo(cur, nuevo_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True, 'id': nuevo_id})

@app.route('/api/productos/<int:producto_id>/editar', methods=['POST'])
def api_producto_editar(producto_id):
    data = request.json
    cur = mysql.connection.cursor()
    if data.get('imagen'):
        cur.execute("""UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
            stock=%s, categoria=%s, imagen=%s WHERE id=%s""",
            (data['nombre'], data['descripcion'], data['precio'],
             data['stock'], data['categoria'], data['imagen'], producto_id))
    else:
        cur.execute("""UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
            stock=%s, categoria=%s WHERE id=%s""",
            (data['nombre'], data['descripcion'], data['precio'],
             data['stock'], data['categoria'], producto_id))
    mysql.connection.commit()
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/productos/<int:producto_id>/inactivar', methods=['POST'])
def api_producto_inactivar(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos SET estado='inactivo' WHERE id=%s", (producto_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/productos/<int:producto_id>/activar', methods=['POST'])
def api_producto_activar(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos SET estado='activo' WHERE id=%s", (producto_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/productos/<int:producto_id>/eliminar', methods=['POST'])
def api_producto_eliminar(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM productos WHERE id=%s", (producto_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/productos/<int:producto_id>/decrementar_stock', methods=['POST'])
def api_producto_decrementar_stock(producto_id):
    data = request.json
    cantidad = data.get('cantidad', 1)
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos SET stock=stock-%s WHERE id=%s", (cantidad, producto_id))
    mysql.connection.commit()
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/productos/stock_bajo')
def api_productos_stock_bajo():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, nombre, stock, categoria, imagen FROM productos WHERE estado='activo' AND stock <= 5 ORDER BY stock ASC LIMIT 10")
    data = cur.fetchall()
    cur.close()
    return jsonify(data)

@app.route('/api/stats')
def api_stats():
    cur = mysql.connection.cursor()
    cur.execute("SELECT COUNT(*) AS total FROM productos WHERE estado='activo'")
    pa = cur.fetchone()['total'] or 0
    cur.execute("SELECT COUNT(*) AS total FROM productos WHERE estado='activo' AND stock <= 5")
    sb = cur.fetchone()['total'] or 0
    cur.execute("SELECT COUNT(*) AS total FROM productos_para_pedir WHERE estado='pendiente'")
    pp = cur.fetchone()['total'] or 0
    cur.execute("SELECT COUNT(*) AS total FROM proveedores")
    tp = cur.fetchone()['total'] or 0
    cur.close()
    return jsonify({'productos_activos': pa, 'stock_bajo': sb, 'pedidos_pendientes': pp, 'total_proveedores': tp})

@app.route('/api/proveedores')
def api_proveedores():
    buscar = request.args.get('buscar', '')
    categoria = request.args.get('categoria', '')
    cur = mysql.connection.cursor()
    sql = "SELECT * FROM proveedores WHERE nombre LIKE %s"
    vals = [f'%{buscar}%']
    if categoria:
        sql += " AND categoria=%s"
        vals.append(categoria)
    sql += " ORDER BY nombre ASC"
    cur.execute(sql, tuple(vals))
    data = cur.fetchall()
    cur.close()
    return jsonify(data)

@app.route('/api/proveedores/<int:proveedor_id>')
def api_proveedor(proveedor_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM proveedores WHERE id=%s", (proveedor_id,))
    p = cur.fetchone()
    cur.close()
    if not p:
        return jsonify({'error': 'Proveedor no encontrado'}), 404
    return jsonify(p)

@app.route('/api/proveedores', methods=['POST'])
def api_proveedor_crear():
    data = request.json
    cur = mysql.connection.cursor()
    cur.execute("""INSERT INTO proveedores (nombre, celular, correo, dni, ruc, direccion, categoria, notas)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (data['nombre'], data['celular'], data['correo'], data['dni'],
         data['ruc'], data['direccion'], data['categoria'], data['notas']))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/proveedores/<int:proveedor_id>/editar', methods=['POST'])
def api_proveedor_editar(proveedor_id):
    data = request.json
    cur = mysql.connection.cursor()
    cur.execute("""UPDATE proveedores SET nombre=%s, celular=%s, correo=%s, dni=%s,
        ruc=%s, direccion=%s, categoria=%s, notas=%s WHERE id=%s""",
        (data['nombre'], data['celular'], data['correo'], data['dni'],
         data['ruc'], data['direccion'], data['categoria'], data['notas'], proveedor_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/proveedores/<int:proveedor_id>/eliminar', methods=['POST'])
def api_proveedor_eliminar(proveedor_id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM proveedores WHERE id=%s", (proveedor_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/pedidos')
def api_pedidos():
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT pp.id, pp.cantidad_pedido, pp.fecha, pp.estado,
               p.nombre AS producto_nombre, p.stock AS stock_actual, p.categoria,
               pr.id AS proveedor_id, pr.nombre AS proveedor_nombre,
               pr.celular AS proveedor_celular, pr.correo AS proveedor_correo
        FROM productos_para_pedir pp
        JOIN productos p ON pp.producto_id=p.id
        LEFT JOIN proveedores pr ON pp.proveedor_id=pr.id
        WHERE pp.estado='pendiente'
        ORDER BY pp.fecha DESC
    """)
    data = cur.fetchall()
    cur.close()
    for d in data:
        if isinstance(d.get('fecha'), datetime):
            d['fecha'] = d['fecha'].isoformat()
    return jsonify(data)

@app.route('/api/pedidos/<int:pedido_id>/cancelar', methods=['POST'])
def api_pedido_cancelar(pedido_id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado='cancelado' WHERE id=%s", (pedido_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/pedidos/<int:pedido_id>/actualizar', methods=['POST'])
def api_pedido_actualizar(pedido_id):
    data = request.json
    cantidad = int(data.get('cantidad', 1))
    if cantidad < 1:
        cantidad = 1
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET cantidad_pedido=%s WHERE id=%s", (cantidad, pedido_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/pedidos/<int:pedido_id>/marcar-enviado', methods=['POST'])
def api_pedido_marcar_enviado(pedido_id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado='enviado' WHERE id=%s", (pedido_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})

@app.route('/api/pedidos/proveedor/<int:proveedor_id>')
def api_pedidos_proveedor(proveedor_id):
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT p.nombre, p.categoria, pp.cantidad_pedido
        FROM productos_para_pedir pp
        JOIN productos p ON pp.producto_id=p.id
        WHERE pp.proveedor_id=%s AND pp.estado='pendiente'
    """, (proveedor_id,))
    data = cur.fetchall()
    cur.close()
    return jsonify(data)

@app.route('/admin')
def admin():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos")
    productos = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS total FROM productos_para_pedir WHERE estado='pendiente'")
    r = cur.fetchone()
    pedidos_pendientes = r['total'] if r else 0
    cur.execute("SELECT COUNT(*) AS total FROM proveedores")
    r2 = cur.fetchone()
    total_proveedores = r2['total'] if r2 else 0
    cur.close()
    return render_template('admin.html', productos=productos,
                           pedidos_pendientes=pedidos_pendientes,
                           total_proveedores=total_proveedores)

@app.route('/agregar_producto', methods=['POST'])
def agregar_producto():
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
    cur.execute("""INSERT INTO productos (nombre, descripcion, precio, stock, categoria, imagen)
        VALUES (%s,%s,%s,%s,%s,%s)""",
        (nombre, descripcion, precio, stock, categoria, imagen_db))
    mysql.connection.commit()
    nuevo_id = cur.lastrowid
    verificar_stock_bajo(cur, nuevo_id)
    mysql.connection.commit()
    cur.close()
    return redirect('/admin')

@app.route('/editar_producto/<int:id>', methods=['GET','POST'])
def editar_producto(id):
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
            cur.execute("""UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s, imagen=%s WHERE id=%s""",
                (nombre, descripcion, precio, stock, categoria, 'uploads/'+fn, id))
        else:
            cur.execute("""UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s WHERE id=%s""",
                (nombre, descripcion, precio, stock, categoria, id))
        mysql.connection.commit()
        verificar_stock_bajo(cur, id)
        mysql.connection.commit()
        cur.close()
        flash('Producto actualizado correctamente.', 'success')
        return redirect('/admin')
    cur.execute("SELECT * FROM productos WHERE id=%s", (id,))
    producto = cur.fetchone()
    cur.close()
    return render_template('editar_producto.html', producto=producto, categorias=CATEGORIAS)

@app.route('/eliminar_producto/<int:id>')
def eliminar_producto(id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos SET estado='inactivo' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    return redirect('/admin')

@app.route('/activar_producto/<int:id>')
def activar_producto(id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos SET estado='activo' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    return redirect('/admin')

@app.route('/eliminar_producto_definitivo/<int:id>')
def eliminar_producto_definitivo(id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM productos WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Producto eliminado permanentemente.', 'success')
    return redirect('/admin')

@app.route('/proveedores', methods=['GET','POST'])
def proveedores():
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
        cur.execute("""INSERT INTO proveedores (nombre, celular, correo, dni, ruc, direccion, categoria, notas)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (nombre, celular, correo, dni, ruc, direccion, categoria, notas))
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
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        cur.execute("""UPDATE proveedores SET nombre=%s, celular=%s, correo=%s, dni=%s,
            ruc=%s, direccion=%s, categoria=%s, notas=%s WHERE id=%s""",
            (request.form.get('nombre'), request.form.get('celular'),
             request.form.get('correo'), request.form.get('dni'),
             request.form.get('ruc'),    request.form.get('direccion'),
             request.form.get('categoria'), request.form.get('notas'), id))
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
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM proveedores WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Proveedor eliminado.', 'success')
    return redirect('/proveedores')

@app.route('/productos-para-pedir')
def productos_para_pedir():
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT pp.id, pp.cantidad_pedido, pp.fecha, pp.estado,
               p.nombre AS producto_nombre, p.stock AS stock_actual, p.categoria,
               pr.id AS proveedor_id, pr.nombre AS proveedor_nombre,
               pr.celular AS proveedor_celular, pr.correo AS proveedor_correo
        FROM productos_para_pedir pp
        JOIN productos p ON pp.producto_id=p.id
        LEFT JOIN proveedores pr ON pp.proveedor_id=pr.id
        WHERE pp.estado='pendiente'
        ORDER BY pp.fecha DESC
    """)
    pedidos = cur.fetchall()
    cur.close()
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
    from urllib.parse import quote
    for pid, data in proveedores_dict.items():
        if data['proveedor_celular']:
            numero = ''.join(filter(str.isdigit, data['proveedor_celular'] or ''))
            if not numero.startswith('51'):
                numero = '51' + numero
            lista = "\n".join([
                f"- {p['producto_nombre']} x{p['cantidad_pedido']} (Stock actual: {p.get('stock_actual',0)})"
                for p in data['productos']
            ])
            msg = f"Hola {data['proveedor_nombre']}, necesitamos reponer los siguientes productos:\n\n{lista}\n\nGracias - Multiservicios Richard"
            data['whatsapp_url'] = f"https://wa.me/{numero}?text={quote(msg)}"
        else:
            data['whatsapp_url'] = None
    return render_template('productos_para_pedir.html',
                           pedidos=pedidos,
                           proveedores_pedidos=proveedores_dict)

@app.route('/productos-para-pedir/eliminar/<int:id>')
def eliminar_pedido(id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado='cancelado' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Pedido eliminado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/actualizar/<int:id>', methods=['POST'])
def actualizar_pedido(id):
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
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado='enviado' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Pedido marcado como enviado.', 'success')
    return redirect('/productos-para-pedir')

@app.route('/productos-para-pedir/enviar-email/<int:proveedor_id>')
def enviar_email_a_proveedor(proveedor_id):
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
        flash('Error al enviar email. Verifica la configuracion SMTP.', 'danger')
    return redirect('/productos-para-pedir')

@app.errorhandler(Exception)
def manejar_error(e):
    if isinstance(e, HTTPException):
        return e
    try:
        app.logger.error('Error no controlado', exc_info=e)
    except Exception:
        pass
    return "Ocurrio un error interno. Revisa los logs.", 500

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
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
