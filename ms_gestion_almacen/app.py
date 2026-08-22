# ─────────────────────────────────────────────
# MS GESTIÓN DE ALMACÉN (puerto 5001)
# Productos, proveedores, ingresos, salidas,
# pedidos a proveedores e inventario.
# BD dedicada: bd_almacen
# ─────────────────────────────────────────────
import os, secrets, threading
from datetime import datetime
from decimal import Decimal

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash)
from flask.json.provider import DefaultJSONProvider
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
import requests as http_requests

import logging
from werkzeug.exceptions import HTTPException

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambiar-esta-clave-segura')
bcrypt = Bcrypt(app)

UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


class AlmacenJSONProvider(DefaultJSONProvider):
    """Serializa Decimal y datetime para las respuestas JSON de la API."""
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


app.json_provider_class = AlmacenJSONProvider

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

# ─────────────────────────────────────────────
# MYSQL — BD Almacén (productos, proveedores, pedidos)
# ─────────────────────────────────────────────
app.config['MYSQL_HOST']        = os.environ.get('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER']        = os.environ.get('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD']    = os.environ.get('MYSQL_PASSWORD', '')
app.config['MYSQL_DB']          = os.environ.get('MYSQL_DB_ALMACEN', 'bd_almacen')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'
mysql = MySQL(app)

MAIN_DB = os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard')

# ─────────────────────────────────────────────
# MS VENTAS (BD principal: ventas, usuarios, entregas)
# ─────────────────────────────────────────────
MS_VENTAS_URL = os.environ.get('MS_VENTAS_URL', 'http://localhost:5000')

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

CATEGORIAS = ['Herramientas', 'Electricos', 'Accesorios', 'Repuestos', 'Otros']

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


def render_paginated(template, cur, sql_count, sql_data, params, page, extra=None):
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, params, page)
    ctx = {
        'items': items, 'total': total, 'page': page,
        'total_pages': total_pages, 'has_prev': page > 1, 'has_next': page < total_pages,
    }
    if extra:
        ctx.update(extra)
    return render_template(template, **ctx)


# ─────────────────────────────────────────────
# INIT TABLAS — BD Almacén
# ─────────────────────────────────────────────
def init_db():
    try:
        cur = mysql.connection.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS productos (
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
            cur.execute("ALTER TABLE productos ADD COLUMN estado VARCHAR(20) DEFAULT 'activo'")
        except Exception:
            pass
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS historial_stock (
                id             INT AUTO_INCREMENT PRIMARY KEY,
                producto_id    INT NOT NULL,
                tipo           VARCHAR(20) NOT NULL,
                cantidad       INT NOT NULL,
                stock_anterior INT NOT NULL DEFAULT 0,
                stock_nuevo    INT NOT NULL DEFAULT 0,
                notas          VARCHAR(255),
                fecha          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movimientos_stock (
                id               INT AUTO_INCREMENT PRIMARY KEY,
                producto_id      INT NOT NULL,
                tipo_movimiento  VARCHAR(20) NOT NULL,
                cantidad         INT NOT NULL,
                referencia_tipo  VARCHAR(30),
                referencia_id    INT,
                notas            VARCHAR(255),
                fecha            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingresos (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                proveedor_id INT,
                notas        TEXT,
                fecha        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS detalle_ingreso (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                ingreso_id    INT NOT NULL,
                producto_id   INT NOT NULL,
                cantidad      INT NOT NULL,
                precio_compra DECIMAL(10,2) DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS salidas (
                id       INT AUTO_INCREMENT PRIMARY KEY,
                venta_id INT,
                notas    TEXT,
                fecha    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        # Migración única: si la tabla del almacén está vacía pero existe en
        # la BD principal con datos, se copian para no perder nada.
        for tabla in ['productos', 'proveedores', 'productos_para_pedir']:
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {tabla}")
                n_al = cur.fetchone()['n']
                cur.execute(f"SELECT COUNT(*) AS n FROM {MAIN_DB}.{tabla}")
                n_orig = cur.fetchone()['n']
                if n_orig and not n_al:
                    cur.execute(f"INSERT INTO {tabla} SELECT * FROM {MAIN_DB}.{tabla}")
            except Exception:
                pass

        mysql.connection.commit()
        cur.close()
        print("[MS Almacen] Tablas creadas/verificadas OK en bd_almacen")
    except Exception as e:
        print(f"[MS Almacen] Error creando tablas: {e}")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def api_ventas(endpoint):
    """Llama GET a la API del microservicio de Ventas (BD principal)."""
    try:
        r = http_requests.get(f"{MS_VENTAS_URL}{endpoint}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[api_ventas] Error {endpoint}: {e}")
    return None


def contar_via_api(endpoint):
    """Cuenta registros devueltos por la API de Ventas (lista o {'total': n})."""
    data = api_ventas(endpoint)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return data.get('total', 0)
    return 0


def obtener_venta_api(venta_id):
    """Obtiene una venta desde MS Ventas o None si no existe/error."""
    venta = api_ventas(f"/api/ventas/{venta_id}")
    if isinstance(venta, dict) and 'error' not in venta:
        return venta
    return None


def obtener_usuario_api(user_id):
    """Obtiene un usuario desde MS Ventas o None si no existe/error."""
    usuario = api_ventas(f"/api/usuarios/{user_id}")
    if isinstance(usuario, dict) and 'error' not in usuario:
        return usuario
    return None


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
        cur.execute("SELECT nombre, stock, categoria FROM productos WHERE id=%s", (producto_id,))
        p = cur.fetchone()
        if not p:
            return
        if p['stock'] <= 0:
            cur.execute("UPDATE productos SET estado='inactivo' WHERE id=%s", (producto_id,))
        if p['stock'] > 1:
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


def registrar_cambio_stock(cur, producto_id, stock_anterior, stock_nuevo, tipo,
                           referencia_tipo=None, referencia_id=None, notas=None):
    """Registra el cambio de stock en historial_stock y movimientos_stock."""
    try:
        cantidad = abs(int(stock_nuevo) - int(stock_anterior))
        cur.execute("""
            INSERT INTO historial_stock (producto_id, tipo, cantidad, stock_anterior, stock_nuevo, notas)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (producto_id, tipo, cantidad, int(stock_anterior), int(stock_nuevo), notas))
        cur.execute("""
            INSERT INTO movimientos_stock (producto_id, tipo_movimiento, cantidad, referencia_tipo, referencia_id, notas)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (producto_id, tipo, cantidad, referencia_tipo, referencia_id, notas))
    except Exception as e:
        print(f"[registrar_cambio_stock] {e}")


# ─────────────────────────────────────────────
# CONTEXT PROCESSOR
# ─────────────────────────────────────────────
@app.context_processor
def inject_categorias():
    return dict(categorias=CATEGORIAS)


# ─────────────────────────────────────────────
# TEST / INIT
# ─────────────────────────────────────────────
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
    flash('Tablas de almacén creadas/verificadas.', 'success')
    return redirect('/admin')


# ─────────────────────────────────────────────
# ADMIN PANEL (productos)
# ─────────────────────────────────────────────
@app.route('/admin')
def admin():
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()

    where = "WHERE p.nombre LIKE %s"
    params = [f'%{buscar}%']
    sql_count = "SELECT COUNT(*) AS total FROM productos p " + where
    sql_data = """SELECT p.* FROM productos p """ + where + """
                   ORDER BY p.id DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)

    cur.execute("SELECT COUNT(*) AS total FROM productos_para_pedir WHERE estado='pendiente'")
    r = cur.fetchone()
    pedidos_pendientes = r['total'] if r else 0

    cur.execute("SELECT COUNT(*) AS total FROM proveedores")
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
# PRODUCTOS CRUD (HTML)
# ─────────────────────────────────────────────
@app.route('/agregar_producto', methods=['POST'])
def agregar_producto():
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
    cur.execute("""
        INSERT INTO productos (nombre, descripcion, precio, stock, categoria, imagen)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (nombre, descripcion, precio, stock, categoria, imagen_db))
    mysql.connection.commit()
    nuevo_id = cur.lastrowid
    registrar_cambio_stock(cur, nuevo_id, 0, stock, 'entrada',
                           referencia_tipo='producto',
                           notas='Stock inicial del producto')
    verificar_stock_bajo(cur, nuevo_id)
    mysql.connection.commit()
    cur.close()
    flash('Producto agregado correctamente.', 'success')
    return redirect('/admin')


@app.route('/editar_producto/<int:id>', methods=['GET','POST'])
def editar_producto(id):
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

        cur.execute("SELECT stock FROM productos WHERE id=%s", (id,))
        previo = cur.fetchone()
        stock_anterior = previo['stock'] if previo else 0

        if imagen and imagen.filename:
            fn = secure_filename(imagen.filename)
            imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
            cur.execute("""
                UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s, imagen=%s WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, 'uploads/'+fn, id))
        else:
            cur.execute("""
                UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
                stock=%s, categoria=%s WHERE id=%s
            """, (nombre, descripcion, precio, stock, categoria, id))

        mysql.connection.commit()
        if stock != stock_anterior:
            tipo = 'entrada' if stock > stock_anterior else 'salida'
            registrar_cambio_stock(cur, id, stock_anterior, stock, tipo,
                                   referencia_tipo='edicion',
                                   notas='Ajuste manual desde edición de producto')
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
    flash('Producto inactivado.', 'success')
    return redirect('/admin')


@app.route('/activar_producto/<int:id>')
def activar_producto(id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos SET estado='activo' WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Producto activado.', 'success')
    return redirect('/admin')


@app.route('/eliminar_producto_definitivo/<int:id>')
def eliminar_producto_definitivo(id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM productos WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Producto eliminado permanentemente.', 'success')
    return redirect('/admin')


# ─────────────────────────────────────────────
# PROVEEDORES (HTML)
# ─────────────────────────────────────────────
@app.route('/proveedores', methods=['GET','POST'])
def proveedores():
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
        cur.execute("""
            INSERT INTO proveedores (nombre, celular, correo, dni, ruc, direccion, categoria, notas)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (nombre, celular, correo, dni, ruc, direccion, categoria, notas))
        mysql.connection.commit()
        flash(f'Proveedor "{nombre}" agregado correctamente.', 'success')
        return redirect('/proveedores')

    buscar = request.args.get('buscar','')
    cat_filtro = request.args.get('categoria','')
    page = int(request.args.get('page', 1))
    where = "WHERE nombre LIKE %s"
    vals = [f'%{buscar}%']
    if cat_filtro:
        where += " AND categoria=%s"
        vals.append(cat_filtro)
    sql_count = "SELECT COUNT(*) AS total FROM proveedores " + where
    sql_data = "SELECT * FROM proveedores " + where + " ORDER BY nombre ASC"
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(vals), page)
    cur.close()
    return render_template('proveedores.html', proveedores=items,
                           categorias=CATEGORIAS, buscar=buscar, cat_filtro=cat_filtro,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages)


@app.route('/proveedores/editar/<int:id>', methods=['GET','POST'])
def editar_proveedor(id):
    cur = mysql.connection.cursor()
    if request.method == 'POST':
        if not validate_csrf():
            flash('Token CSRF inválido.', 'danger')
            return redirect(f'/proveedores/editar/{id}')
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
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM proveedores WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Proveedor eliminado.', 'success')
    return redirect('/proveedores')


# ─────────────────────────────────────────────
# PRODUCTOS PARA PEDIR (HTML)
# ─────────────────────────────────────────────
@app.route('/productos-para-pedir')
def productos_para_pedir():
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
    cur.close()

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
    try:
        cantidad = int(request.form.get('cantidad', 1))
    except (ValueError, TypeError):
        cantidad = 1
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
        cur.close()
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
# INGRESOS DE PRODUCTOS (Comprobante de ingreso)
# ─────────────────────────────────────────────
@app.route('/ingresos')
def ingresos():
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    sql_count = """SELECT COUNT(*) AS total FROM ingresos i
        LEFT JOIN proveedores p ON i.proveedor_id=p.id
        WHERE COALESCE(p.nombre, '') LIKE %s"""
    sql_data = """SELECT i.id, i.fecha, i.notas,
               p.nombre AS proveedor_nombre,
               (SELECT SUM(di.cantidad) FROM detalle_ingreso di WHERE di.ingreso_id=i.id) AS total_items
        FROM ingresos i
        LEFT JOIN proveedores p ON i.proveedor_id=p.id
        WHERE COALESCE(p.nombre, '') LIKE %s
        ORDER BY i.fecha DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, (f'%{buscar}%',), page)
    cur.close()
    return render_template('ingresos.html', ingresos=items, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages)


@app.route('/registrar-ingreso', methods=['GET','POST'])
def registrar_ingreso():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, nombre, stock, precio FROM productos WHERE estado='activo' ORDER BY nombre")
    productos = cur.fetchall()
    cur.execute("SELECT id, nombre FROM proveedores ORDER BY nombre")
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
                cur.execute("SELECT stock FROM productos WHERE id=%s", (pid,))
                previo = cur.fetchone()
                stock_anterior = previo['stock'] if previo else 0
                cur.execute("INSERT INTO detalle_ingreso (ingreso_id, producto_id, cantidad, precio_compra) VALUES (%s,%s,%s,%s)",
                            (ingreso_id, pid, cant, prec))
                cur.execute("UPDATE productos SET stock=stock+%s WHERE id=%s", (cant, pid))
                registrar_cambio_stock(cur, pid, stock_anterior, stock_anterior + cant, 'entrada',
                                       referencia_tipo='ingreso', referencia_id=ingreso_id,
                                       notas=f'Ingreso #{ingreso_id}')
                verificar_stock_bajo(cur, pid)

        mysql.connection.commit()
        cur.close()
        flash(f'Ingreso #{ingreso_id} registrado correctamente.', 'success')
        return redirect(f'/comprobante-ingreso/{ingreso_id}')

    return render_template('registrar_ingreso.html', productos=productos, proveedores=proveedores)


@app.route('/comprobante-ingreso/<int:id>')
def comprobante_ingreso(id):
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT i.id, i.fecha, i.notas,
               p.nombre AS proveedor_nombre, p.ruc AS proveedor_ruc,
               p.celular AS proveedor_celular, p.correo AS proveedor_correo
        FROM ingresos i
        LEFT JOIN proveedores p ON i.proveedor_id=p.id
        WHERE i.id=%s
    """, (id,))
    ingreso = cur.fetchone()
    if not ingreso:
        cur.close()
        flash('Ingreso no encontrado.', 'danger')
        return redirect('/ingresos')
    cur.execute("""
        SELECT di.cantidad, di.precio_compra,
               pr.nombre AS producto_nombre, pr.stock AS stock_actual
        FROM detalle_ingreso di
        JOIN productos pr ON di.producto_id=pr.id
        WHERE di.ingreso_id=%s
    """, (id,))
    items = cur.fetchall()
    total_general = sum(i['cantidad'] * float(i['precio_compra']) for i in items)
    cur.close()
    return render_template('comprobante_ingreso.html', ingreso=ingreso, items=items, total_general=total_general)


@app.route('/eliminar-ingreso/<int:id>')
def eliminar_ingreso(id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT producto_id, cantidad FROM detalle_ingreso WHERE ingreso_id=%s", (id,))
    items = cur.fetchall()
    revertidos = 0
    for item in items:
        cur.execute("SELECT stock FROM productos WHERE id=%s", (item['producto_id'],))
        prod = cur.fetchone()
        stock_actual = prod['stock'] if prod else 0
        if stock_actual >= item['cantidad']:
            cur.execute("UPDATE productos SET stock=stock-%s WHERE id=%s", (item['cantidad'], item['producto_id']))
            registrar_cambio_stock(cur, item['producto_id'], stock_actual, stock_actual - item['cantidad'], 'salida',
                                   referencia_tipo='ingreso_eliminado', referencia_id=id,
                                   notas=f'Reversión de ingreso #{id}')
        else:
            cur.execute("UPDATE productos SET stock=0 WHERE id=%s", (item['producto_id'],))
            registrar_cambio_stock(cur, item['producto_id'], stock_actual, 0, 'ajuste',
                                   referencia_tipo='ingreso_eliminado', referencia_id=id,
                                   notas=f'Reversión de ingreso #{id} (stock insuficiente)')
        revertidos += 1
    cur.execute("DELETE FROM detalle_ingreso WHERE ingreso_id=%s", (id,))
    cur.execute("DELETE FROM ingresos WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash(f'Ingreso eliminado y stock revertido ({revertidos} productos).', 'success')
    return redirect('/ingresos')


# ─────────────────────────────────────────────
# REGISTRO DE SALIDA DE PRODUCTOS
# ─────────────────────────────────────────────
@app.route('/salidas')
def salidas():
    buscar = request.args.get('buscar', '')
    page = int(request.args.get('page', 1))
    cur = mysql.connection.cursor()
    sql_count = "SELECT COUNT(*) AS total FROM salidas s WHERE s.notas LIKE %s OR %s=''"
    sql_data = """SELECT s.id, s.fecha, s.notas, s.venta_id,
               (SELECT SUM(ds.cantidad) FROM detalle_salida ds WHERE ds.salida_id=s.id) AS total_items
        FROM salidas s
        WHERE s.notas LIKE %s OR %s=''
        ORDER BY s.fecha DESC"""
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, (f'%{buscar}%', buscar), page)

    # Enriquecer con datos de la venta vía API de MS Ventas
    for s in items:
        s['cliente_nombre'] = None
        s['venta_total'] = None
        if s.get('venta_id'):
            venta = obtener_venta_api(s['venta_id'])
            if venta:
                s['cliente_nombre'] = venta.get('nombre')
                s['venta_total'] = venta.get('total')
    cur.close()
    return render_template('salidas.html', salidas=items, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages)


@app.route('/registrar-salida', methods=['GET','POST'])
def registrar_salida():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, nombre, stock FROM productos WHERE estado='activo' ORDER BY nombre")
    productos = cur.fetchall()
    cur.close()

    # Ventas disponibles desde el MS de Ventas (BD principal vía API)
    ventas_api = api_ventas('/api/ventas') or []
    ventas = []
    for v in ventas_api[:50]:
        if v.get('estado') not in ('en espera', 'entregado'):
            continue
        completa = obtener_venta_api(v.get('id')) if v.get('id') else None
        usuario = obtener_usuario_api(v.get('cliente_id')) if v.get('cliente_id') else None
        ventas.append({
            'id': v['id'],
            'nombre': v.get('nombre') or (completa or {}).get('nombre')
                      or (usuario or {}).get('correo') or f"Venta #{v['id']}",
            'total': v.get('total'),
            'fecha': v.get('fecha'),
            'estado': v.get('estado'),
            'correo': (usuario or {}).get('correo')
        })

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
                cur.execute("SELECT stock, nombre FROM productos WHERE id=%s", (pid,))
                prod = cur.fetchone()
                stock_actual = prod['stock'] if prod else 0
                if stock_actual < cant:
                    nombre_prod = prod['nombre'] if prod else f'ID {pid}'
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
                cur.execute("SELECT stock FROM productos WHERE id=%s", (pid,))
                previo = cur.fetchone()
                stock_anterior = previo['stock'] if previo else 0
                cur.execute("UPDATE productos SET stock=stock-%s WHERE id=%s AND stock>=%s", (cant, pid, cant))
                registrar_cambio_stock(cur, pid, stock_anterior, stock_anterior - cant, 'salida',
                                       referencia_tipo='salida', referencia_id=salida_id,
                                       notas=f'Salida #{salida_id}')
                verificar_stock_bajo(cur, pid)

        mysql.connection.commit()
        cur.close()
        flash(f'Salida #{salida_id} registrada correctamente.', 'success')
        return redirect(f'/comprobante-salida/{salida_id}')

    return render_template('registrar_salida.html', ventas=ventas, productos=productos)


@app.route('/comprobante-salida/<int:id>')
def comprobante_salida(id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, fecha, notas, venta_id FROM salidas WHERE id=%s", (id,))
    salida = cur.fetchone()
    if not salida:
        cur.close()
        flash('Salida no encontrada.', 'danger')
        return redirect('/salidas')
    venta = obtener_venta_api(salida['venta_id']) if salida.get('venta_id') else None
    salida['cliente_nombre'] = (venta or {}).get('nombre')
    salida['cliente_doc'] = (venta or {}).get('documento')
    salida['venta_total'] = (venta or {}).get('total')
    salida['venta_fecha'] = (venta or {}).get('fecha')
    cur.execute("""
        SELECT ds.cantidad, pr.nombre AS producto_nombre, pr.stock AS stock_actual
        FROM detalle_salida ds
        JOIN productos pr ON ds.producto_id=pr.id
        WHERE ds.salida_id=%s
    """, (id,))
    items = cur.fetchall()
    cur.close()
    return render_template('comprobante_salida.html', salida=salida, items=items)


@app.route('/eliminar-salida/<int:id>')
def eliminar_salida(id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT producto_id, cantidad FROM detalle_salida WHERE salida_id=%s", (id,))
    items = cur.fetchall()
    for item in items:
        cur.execute("SELECT stock FROM productos WHERE id=%s", (item['producto_id'],))
        previo = cur.fetchone()
        stock_anterior = previo['stock'] if previo else 0
        cur.execute("UPDATE productos SET stock=stock+%s WHERE id=%s", (item['cantidad'], item['producto_id']))
        registrar_cambio_stock(cur, item['producto_id'], stock_anterior, stock_anterior + item['cantidad'], 'entrada',
                               referencia_tipo='salida_eliminada', referencia_id=id,
                               notas=f'Reversión de salida #{id}')
    cur.execute("DELETE FROM detalle_salida WHERE salida_id=%s", (id,))
    cur.execute("DELETE FROM salidas WHERE id=%s", (id,))
    mysql.connection.commit()
    cur.close()
    flash('Salida eliminada y stock revertido.', 'success')
    return redirect('/salidas')


# ─────────────────────────────────────────────
# VERIFICAR INVENTARIO POR CANTIDAD
# ─────────────────────────────────────────────
@app.route('/verificar-inventario')
def verificar_inventario():
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
    sql_count = "SELECT COUNT(*) AS total FROM productos p " + where
    sql_data = """SELECT p.id, p.nombre, p.stock, p.precio, p.categoria, p.imagen, p.estado
        FROM productos p """ + where + " ORDER BY p.stock ASC"
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)

    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock=0")
    sin_stock = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock BETWEEN 1 AND 3")
    critico = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock BETWEEN 4 AND 10")
    bajo = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock > 10")
    normal = cur.fetchone()['n']
    cur.close()
    return render_template('verificar_inventario.html',
                           productos=items, filtro=filtro, buscar=buscar,
                           page=page, total_pages=total_pages, total=total,
                           has_prev=page > 1, has_next=page < total_pages,
                           sin_stock=sin_stock, critico=critico, bajo=bajo, normal=normal)


# ─────────────────────────────────────────────
# VERIFICAR / CONSULTAR REGISTROS
# ─────────────────────────────────────────────
@app.route('/verificar-registros')
def verificar_registros():
    seccion = request.args.get('seccion', 'resumen')
    cur = mysql.connection.cursor()
    stats = {}

    # Conteos locales (BD Almacén)
    cur.execute("SELECT COUNT(*) AS n FROM productos")
    stats['total_productos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM proveedores")
    stats['total_proveedores'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos_para_pedir")
    stats['total_pedidos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM ingresos")
    stats['total_ingresos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_ingreso")
    stats['total_detalle_ingresos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM salidas")
    stats['total_salidas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_salida")
    stats['total_detalle_salidas'] = cur.fetchone()['n']

    # Conteos de la BD principal vía API del MS Ventas
    stats['total_ventas'] = contar_via_api('/api/ventas')
    stats['total_detalle_ventas'] = contar_via_api('/api/detalle-ventas')
    stats['total_entregas'] = contar_via_api('/api/entregas')
    stats['total_usuarios'] = contar_via_api('/api/usuarios')

    datos = []
    if seccion == 'ventas':
        ventas_api = api_ventas('/api/ventas') or []
        for v in reversed(ventas_api[-50:]):
            completa = obtener_venta_api(v.get('id')) if v.get('id') else None
            usuario = obtener_usuario_api(v.get('cliente_id')) if v.get('cliente_id') else None
            datos.append({
                'id': v.get('id'), 'total': v.get('total'), 'fecha': v.get('fecha'),
                'estado': v.get('estado'),
                'nombre': v.get('nombre') or (completa or {}).get('nombre'),
                'documento': v.get('documento') or (completa or {}).get('documento'),
                'correo': (usuario or {}).get('correo')
            })
    elif seccion == 'usuarios':
        datos = api_ventas('/api/usuarios') or []
        if isinstance(datos, dict):
            datos = datos.get('usuarios', [])
        datos = datos[:50]
    elif seccion == 'ingresos':
        cur.execute("""SELECT i.id, i.fecha, p.nombre AS proveedor_nombre,
                (SELECT SUM(cantidad) FROM detalle_ingreso WHERE ingreso_id=i.id) AS items
                FROM ingresos i LEFT JOIN proveedores p ON i.proveedor_id=p.id
                ORDER BY i.fecha DESC LIMIT 50""")
        datos = cur.fetchall()
    elif seccion == 'salidas':
        cur.execute("""SELECT s.id, s.fecha, s.venta_id,
                (SELECT SUM(cantidad) FROM detalle_salida WHERE salida_id=s.id) AS items
                FROM salidas s ORDER BY s.fecha DESC LIMIT 50""")
        filas = cur.fetchall()
        for f_ in filas:
            venta = obtener_venta_api(f_.get('venta_id')) if f_.get('venta_id') else None
            f_['cliente_nombre'] = (venta or {}).get('nombre')
        datos = filas
    elif seccion == 'entregas':
        datos = api_ventas('/api/entregas') or []
        if isinstance(datos, dict):
            datos = datos.get('entregas', [])
        datos = datos[:50]

    cur.close()
    return render_template('verificar_registros.html', stats=stats, seccion=seccion, datos=datos)


# ─────────────────────────────────────────────
# INFORME FINAL DEL DÍA
# ─────────────────────────────────────────────
@app.route('/informe-diario')
def informe_diario():
    hoy = datetime.now().strftime('%Y-%m-%d')
    fecha_str = request.args.get('fecha', hoy)
    cur = mysql.connection.cursor()

    # Ventas del día desde la API del MS Ventas (BD principal)
    ventas_todas = api_ventas('/api/ventas') or []
    ventas_dia = sorted(
        [v for v in ventas_todas if str(v.get('fecha', ''))[:10] == fecha_str],
        key=lambda v: v.get('fecha') or ''
    )
    for v in ventas_dia:
        if not v.get('nombre') or not v.get('documento'):
            completa = obtener_venta_api(v.get('id'))
            if completa:
                v.setdefault('nombre', completa.get('nombre'))
                v.setdefault('documento', completa.get('documento'))
    ingresos_ventas = float(sum(float(v.get('total') or 0) for v in ventas_dia))
    num_ventas = len(ventas_dia)

    # Ingresos del día (BD Almacén local)
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

    # Salidas del día (BD Almacén local) + nombre de cliente vía API
    cur.execute("""
        SELECT s.id, s.fecha, s.venta_id,
               (SELECT SUM(ds.cantidad) FROM detalle_salida ds WHERE ds.salida_id=s.id) AS items
        FROM salidas s
        WHERE DATE(s.fecha)=%s ORDER BY s.fecha
    """, (fecha_str,))
    salidas_dia = cur.fetchall()
    for s in salidas_dia:
        s['cliente_nombre'] = None
        if s.get('venta_id'):
            venta = obtener_venta_api(s['venta_id'])
            if venta:
                s['cliente_nombre'] = venta.get('nombre')
    cur.execute("SELECT COUNT(*) AS n FROM salidas WHERE DATE(fecha)=%s", (fecha_str,))
    num_salidas = cur.fetchone()['n']

    # Entregas del día vía API del MS Ventas
    entregas_todas = api_ventas('/api/entregas') or []
    if isinstance(entregas_todas, dict):
        entregas_todas = entregas_todas.get('entregas', [])
    entregas_dia = [
        e for e in entregas_todas
        if str(e.get('created_at', ''))[:10] == fecha_str
        or str(e.get('fecha_entrega', ''))[:10] == fecha_str
    ]
    num_entregadas = sum(1 for e in entregas_dia if e.get('estado') == 'entregado')
    num_pendientes = sum(1 for e in entregas_dia if e.get('estado') != 'entregado')

    cur.close()
    return render_template('informe_diario.html',
                           fecha=fecha_str,
                           ventas_dia=ventas_dia, ingresos_ventas=ingresos_ventas, num_ventas=num_ventas,
                           ingresos_dia=ingresos_dia, num_ingresos=num_ingresos,
                           salidas_dia=salidas_dia, num_salidas=num_salidas,
                           entregas_dia=entregas_dia, num_entregadas=num_entregadas, num_pendientes=num_pendientes)


# ═════════════════════════════════════════════
# REST API — PRODUCTOS (para MS Clientes/Ventas)
# ═════════════════════════════════════════════
@app.route('/api/productos')
def api_productos():
    """Listado de productos con búsqueda y paginación opcional."""
    buscar = request.args.get('buscar', '')
    categoria = request.args.get('categoria', '')
    estado = request.args.get('estado', '')
    page_arg = request.args.get('page')
    try:
        per_page = min(int(request.args.get('per_page', PER_PAGE)), 100)
    except (ValueError, TypeError):
        per_page = PER_PAGE
    cur = mysql.connection.cursor()
    where = ["1=1"]
    vals = []
    if buscar:
        where.append("nombre LIKE %s")
        vals.append(f'%{buscar}%')
    if categoria and categoria != 'Todos':
        where.append("categoria=%s")
        vals.append(categoria)
    if estado:
        where.append("estado=%s")
        vals.append(estado)
    sql = "SELECT * FROM productos WHERE " + " AND ".join(where) + " ORDER BY id DESC"
    cur.execute(sql, tuple(vals))
    data = cur.fetchall()
    cur.close()
    if page_arg:
        total = len(data)
        total_pages = max(1, -(-total // per_page))
        try:
            page = max(1, min(int(page_arg), total_pages))
        except (ValueError, TypeError):
            page = 1
        data = data[(page - 1) * per_page:page * per_page]
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


@app.route('/api/productos/stock_bajo')
def api_productos_stock_bajo():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, nombre, stock, categoria, imagen FROM productos WHERE estado='activo' AND stock <= 5 ORDER BY stock ASC LIMIT 10")
    data = cur.fetchall()
    cur.close()
    return jsonify(data)


@app.route('/api/productos/<int:producto_id>', methods=['GET'])
def api_producto(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos WHERE id=%s", (producto_id,))
    p = cur.fetchone()
    cur.close()
    if not p:
        return jsonify({'error': 'Producto no encontrado'}), 404
    return jsonify(p)


@app.route('/api/productos/<int:producto_id>/stock', methods=['GET'])
def api_producto_stock(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, nombre, stock, estado FROM productos WHERE id=%s", (producto_id,))
    p = cur.fetchone()
    cur.close()
    if not p:
        return jsonify({'error': 'Producto no encontrado'}), 404
    return jsonify(p)


@app.route('/api/productos', methods=['POST'])
def api_producto_crear():
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("""INSERT INTO productos (nombre, descripcion, precio, stock, categoria, imagen)
        VALUES (%s,%s,%s,%s,%s,%s)""",
        (data['nombre'], data['descripcion'], data['precio'],
         data['stock'], data['categoria'], data.get('imagen')))
    mysql.connection.commit()
    nuevo_id = cur.lastrowid
    registrar_cambio_stock(cur, nuevo_id, 0, int(data['stock']), 'entrada',
                           referencia_tipo='producto', notas='Creación vía API')
    verificar_stock_bajo(cur, nuevo_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True, 'id': nuevo_id})


@app.route('/api/productos/<int:producto_id>', methods=['PUT'])
def api_producto_actualizar(producto_id):
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos WHERE id=%s", (producto_id,))
    actual = cur.fetchone()
    if not actual:
        cur.close()
        return jsonify({'error': 'Producto no encontrado'}), 404
    nombre = data.get('nombre', actual['nombre'])
    descripcion = data.get('descripcion', actual['descripcion'])
    precio = data.get('precio', actual['precio'])
    stock = int(data.get('stock', actual['stock']))
    categoria = data.get('categoria', actual['categoria'])
    imagen = data.get('imagen', actual['imagen'])
    estado = data.get('estado', actual['estado'])
    cur.execute("""UPDATE productos SET nombre=%s, descripcion=%s, precio=%s,
        stock=%s, categoria=%s, imagen=%s, estado=%s WHERE id=%s""",
        (nombre, descripcion, precio, stock, categoria, imagen, estado, producto_id))
    mysql.connection.commit()
    if stock != int(actual['stock']):
        tipo = 'entrada' if stock > int(actual['stock']) else 'salida'
        registrar_cambio_stock(cur, producto_id, actual['stock'], stock, tipo,
                               referencia_tipo='api_edicion', notas='Actualización vía API')
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/productos/<int:producto_id>/editar', methods=['POST'])
def api_producto_editar(producto_id):
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("SELECT stock FROM productos WHERE id=%s", (producto_id,))
    previo = cur.fetchone()
    stock_anterior = previo['stock'] if previo else 0
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
    if previo and int(data.get('stock', stock_anterior)) != int(stock_anterior):
        nuevo_stock = int(data.get('stock', stock_anterior))
        tipo = 'entrada' if nuevo_stock > int(stock_anterior) else 'salida'
        registrar_cambio_stock(cur, producto_id, stock_anterior, nuevo_stock, tipo,
                               referencia_tipo='api_edicion', notas='Edición vía API')
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/productos/<int:producto_id>', methods=['DELETE'])
def api_producto_eliminar_def(producto_id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM productos WHERE id=%s", (producto_id,))
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


@app.route('/api/productos/<int:producto_id>/estado', methods=['PUT'])
def api_producto_estado(producto_id):
    data = request.get_json(silent=True) or {}
    nuevo_estado = data.get('estado')
    cur = mysql.connection.cursor()
    cur.execute("SELECT estado FROM productos WHERE id=%s", (producto_id,))
    actual = cur.fetchone()
    if not actual:
        cur.close()
        return jsonify({'error': 'Producto no encontrado'}), 404
    if nuevo_estado not in ('activo', 'inactivo'):
        nuevo_estado = 'inactivo' if actual['estado'] == 'activo' else 'activo'
    cur.execute("UPDATE productos SET estado=%s WHERE id=%s", (nuevo_estado, producto_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True, 'estado': nuevo_estado})


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


@app.route('/api/productos/<int:producto_id>/descontar-stock', methods=['POST'])
def api_producto_descontar_stock(producto_id):
    data = request.get_json(silent=True) or {}
    try:
        cantidad = int(data.get('cantidad', 1))
    except (ValueError, TypeError):
        cantidad = 1
    if cantidad < 1:
        return jsonify({'error': 'Cantidad inválida'}), 400
    cur = mysql.connection.cursor()
    cur.execute("SELECT stock FROM productos WHERE id=%s", (producto_id,))
    prod = cur.fetchone()
    if not prod:
        cur.close()
        return jsonify({'error': 'Producto no encontrado'}), 404
    stock_anterior = int(prod['stock'])
    if stock_anterior < cantidad:
        cur.close()
        return jsonify({'error': 'Stock insuficiente', 'stock': stock_anterior}), 400
    cur.execute("UPDATE productos SET stock=stock-%s WHERE id=%s AND stock>=%s",
                (cantidad, producto_id, cantidad))
    stock_nuevo = stock_anterior - cantidad
    registrar_cambio_stock(cur, producto_id, stock_anterior, stock_nuevo, 'salida',
                           referencia_tipo=data.get('referencia_tipo'),
                           referencia_id=data.get('referencia_id'),
                           notas=data.get('notas') or 'Descuento de stock vía API')
    mysql.connection.commit()
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True, 'stock': stock_nuevo})


@app.route('/api/productos/<int:producto_id>/decrementar_stock', methods=['POST'])
def api_producto_decrementar_stock(producto_id):
    data = request.get_json(silent=True) or {}
    cantidad = data.get('cantidad', 1)
    cur = mysql.connection.cursor()
    cur.execute("SELECT stock FROM productos WHERE id=%s", (producto_id,))
    previo = cur.fetchone()
    stock_anterior = previo['stock'] if previo else 0
    cur.execute("UPDATE productos SET stock=stock-%s WHERE id=%s", (cantidad, producto_id))
    stock_nuevo = max(0, int(stock_anterior) - int(cantidad))
    registrar_cambio_stock(cur, producto_id, stock_anterior, stock_nuevo, 'salida',
                           referencia_tipo=(data or {}).get('referencia_tipo'),
                           referencia_id=(data or {}).get('referencia_id'),
                           notas='Venta (descuento de stock)')
    mysql.connection.commit()
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/productos/<int:producto_id>/incrementar-stock', methods=['POST'])
def api_producto_incrementar_stock(producto_id):
    data = request.get_json(silent=True) or {}
    try:
        cantidad = int(data.get('cantidad', 1))
    except (ValueError, TypeError):
        cantidad = 1
    if cantidad < 1:
        return jsonify({'error': 'Cantidad inválida'}), 400
    cur = mysql.connection.cursor()
    cur.execute("SELECT stock FROM productos WHERE id=%s", (producto_id,))
    prod = cur.fetchone()
    if not prod:
        cur.close()
        return jsonify({'error': 'Producto no encontrado'}), 404
    stock_anterior = int(prod['stock'])
    cur.execute("UPDATE productos SET stock=stock+%s WHERE id=%s", (cantidad, producto_id))
    stock_nuevo = stock_anterior + cantidad
    registrar_cambio_stock(cur, producto_id, stock_anterior, stock_nuevo, 'entrada',
                           referencia_tipo=data.get('referencia_tipo'),
                           referencia_id=data.get('referencia_id'),
                           notas=data.get('notas') or 'Incremento de stock vía API')
    mysql.connection.commit()
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True, 'stock': stock_nuevo})


@app.route('/api/productos/<int:producto_id>/verificar-stock-bajo', methods=['POST'])
def api_producto_verificar_stock_bajo(producto_id):
    cur = mysql.connection.cursor()
    verificar_stock_bajo(cur, producto_id)
    mysql.connection.commit()
    cur.execute("SELECT id, nombre, stock, estado FROM productos WHERE id=%s", (producto_id,))
    p = cur.fetchone()
    pendiente = False
    cur.execute("SELECT id FROM productos_para_pedir WHERE producto_id=%s AND estado='pendiente'", (producto_id,))
    if cur.fetchone():
        pendiente = True
    cur.close()
    if not p:
        return jsonify({'error': 'Producto no encontrado'}), 404
    return jsonify({'ok': True, 'producto': p, 'pedido_pendiente': pendiente})


# ─────────────────────────────────────────────
# REST API — STATS
# ─────────────────────────────────────────────
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
    cur.execute("""
        SELECT id, nombre, stock, categoria, imagen
        FROM productos
        WHERE estado='activo' AND stock <= 5
        ORDER BY stock ASC LIMIT 10
    """)
    psb = cur.fetchall()
    cur.close()
    return jsonify({'productos_activos': pa, 'stock_bajo': sb,
                    'pedidos_pendientes': pp, 'total_proveedores': tp,
                    'productos_stock_bajo': psb})


# ─────────────────────────────────────────────
# REST API — DETALLE DE VENTA (cross-reference con MS Ventas)
# ─────────────────────────────────────────────
@app.route('/api/detalle-venta/<int:venta_id>')
def api_detalle_venta(venta_id):
    # El detalle (producto_id, cantidad, precio) vive en la BD principal del
    # MS Ventas; los nombres de los productos se resuelven localmente.
    detalle = api_ventas(f'/api/detalle-venta/{venta_id}')
    if detalle is None or not isinstance(detalle, list):
        return jsonify({'error': 'Detalle de venta no disponible'}), 404
    cur = mysql.connection.cursor()
    resultado = []
    for d in detalle:
        cur.execute("SELECT id, nombre FROM productos WHERE id=%s", (d.get('producto_id'),))
        p = cur.fetchone()
        resultado.append({
            'producto_id': d.get('producto_id'),
            'nombre': p['nombre'] if p else f'Producto #{d.get("producto_id")}',
            'cantidad': d.get('cantidad'),
            'precio': d.get('precio')
        })
    cur.close()
    return jsonify(resultado)


# ─────────────────────────────────────────────
# REST API — PROVEEDORES
# ─────────────────────────────────────────────
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
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("""INSERT INTO proveedores (nombre, celular, correo, dni, ruc, direccion, categoria, notas)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (data['nombre'], data['celular'], data['correo'], data['dni'],
         data['ruc'], data['direccion'], data['categoria'], data['notas']))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/proveedores/<int:proveedor_id>', methods=['PUT'])
def api_proveedor_actualizar(proveedor_id):
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM proveedores WHERE id=%s", (proveedor_id,))
    actual = cur.fetchone()
    if not actual:
        cur.close()
        return jsonify({'error': 'Proveedor no encontrado'}), 404
    cur.execute("""UPDATE proveedores SET nombre=%s, celular=%s, correo=%s, dni=%s,
        ruc=%s, direccion=%s, categoria=%s, notas=%s WHERE id=%s""",
        (data.get('nombre', actual['nombre']),
         data.get('celular', actual['celular']),
         data.get('correo', actual['correo']),
         data.get('dni', actual['dni']),
         data.get('ruc', actual['ruc']),
         data.get('direccion', actual['direccion']),
         data.get('categoria', actual['categoria']),
         data.get('notas', actual['notas']),
         proveedor_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/proveedores/<int:proveedor_id>/editar', methods=['POST'])
def api_proveedor_editar(proveedor_id):
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("""UPDATE proveedores SET nombre=%s, celular=%s, correo=%s, dni=%s,
        ruc=%s, direccion=%s, categoria=%s, notas=%s WHERE id=%s""",
        (data['nombre'], data['celular'], data['correo'], data['dni'],
         data['ruc'], data['direccion'], data['categoria'], data['notas'], proveedor_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/proveedores/<int:proveedor_id>', methods=['DELETE'])
def api_proveedor_eliminar(proveedor_id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM proveedores WHERE id=%s", (proveedor_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


# ─────────────────────────────────────────────
# REST API — PRODUCTOS PARA PEDIR / PEDIDOS
# ─────────────────────────────────────────────
PEDIDOS_SQL = """
    SELECT pp.id, pp.cantidad_pedido, pp.fecha, pp.estado,
           p.nombre AS producto_nombre, p.stock AS stock_actual, p.categoria,
           pr.id AS proveedor_id, pr.nombre AS proveedor_nombre,
           pr.celular AS proveedor_celular, pr.correo AS proveedor_correo
    FROM productos_para_pedir pp
    JOIN productos p ON pp.producto_id=p.id
    LEFT JOIN proveedores pr ON pp.proveedor_id=pr.id
"""


def _serializar_pedidos(data):
    for d in data:
        if isinstance(d.get('fecha'), datetime):
            d['fecha'] = d['fecha'].isoformat()
    return data


@app.route('/api/pedidos')
def api_pedidos():
    cur = mysql.connection.cursor()
    cur.execute(PEDIDOS_SQL + " WHERE pp.estado='pendiente' ORDER BY pp.fecha DESC")
    data = cur.fetchall()
    cur.close()
    return jsonify(_serializar_pedidos(data))


@app.route('/api/productos-para-pedir')
def api_productos_para_pedir():
    estado = request.args.get('estado', '')
    cur = mysql.connection.cursor()
    if estado:
        cur.execute(PEDIDOS_SQL + " WHERE pp.estado=%s ORDER BY pp.fecha DESC", (estado,))
    else:
        cur.execute(PEDIDOS_SQL + " ORDER BY pp.fecha DESC")
    data = cur.fetchall()
    cur.close()
    return jsonify(_serializar_pedidos(data))


@app.route('/api/productos-para-pedir/<int:pedido_id>', methods=['PUT'])
def api_pedido_actualizar_put(pedido_id):
    data = request.get_json(silent=True) or {}
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM productos_para_pedir WHERE id=%s", (pedido_id,))
    actual = cur.fetchone()
    if not actual:
        cur.close()
        return jsonify({'error': 'Pedido no encontrado'}), 404
    cantidad = int(data.get('cantidad', actual['cantidad_pedido']))
    if cantidad < 1:
        cantidad = 1
    proveedor_id = data.get('proveedor_id', actual['proveedor_id'])
    cur.execute("UPDATE productos_para_pedir SET cantidad_pedido=%s, proveedor_id=%s WHERE id=%s",
                (cantidad, proveedor_id, pedido_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/productos-para-pedir/<int:pedido_id>/estado', methods=['PUT'])
def api_pedido_estado_put(pedido_id):
    data = request.get_json(silent=True) or {}
    estado = data.get('estado', 'pendiente')
    if estado not in ('pendiente', 'enviado', 'cancelado'):
        estado = 'pendiente'
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado=%s WHERE id=%s", (estado, pedido_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True, 'estado': estado})


@app.route('/api/pedidos/<int:pedido_id>/cancelar', methods=['POST'])
def api_pedido_cancelar(pedido_id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE productos_para_pedir SET estado='cancelado' WHERE id=%s", (pedido_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'ok': True})


@app.route('/api/pedidos/<int:pedido_id>/actualizar', methods=['POST'])
def api_pedido_actualizar(pedido_id):
    data = request.get_json(silent=True) or {}
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


# ─────────────────────────────────────────────
# REST API — VERIFICACIONES E INFORMES
# ─────────────────────────────────────────────
@app.route('/api/verificar-inventario')
def api_verificar_inventario():
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
    sql_count = "SELECT COUNT(*) AS total FROM productos p " + where
    sql_data = """SELECT p.id, p.nombre, p.stock, p.precio, p.categoria, p.imagen, p.estado
        FROM productos p """ + where + " ORDER BY p.stock ASC"
    items, total, page, total_pages = paginate_query(cur, sql_count, sql_data, tuple(params), page)

    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock=0")
    sin_stock = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock BETWEEN 1 AND 3")
    critico = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock BETWEEN 4 AND 10")
    bajo = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos WHERE estado='activo' AND stock > 10")
    normal = cur.fetchone()['n']
    cur.close()
    return jsonify({'filtro': filtro, 'buscar': buscar,
                    'productos': items, 'total': total,
                    'page': page, 'total_pages': total_pages,
                    'has_prev': page > 1, 'has_next': page < total_pages,
                    'sin_stock': sin_stock, 'critico': critico,
                    'bajo': bajo, 'normal': normal})


@app.route('/api/verificar-registros')
def api_verificar_registros():
    cur = mysql.connection.cursor()
    stats = {}
    cur.execute("SELECT COUNT(*) AS n FROM productos")
    stats['total_productos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM proveedores")
    stats['total_proveedores'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM productos_para_pedir")
    stats['total_pedidos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM ingresos")
    stats['total_ingresos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_ingreso")
    stats['total_detalle_ingresos'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM salidas")
    stats['total_salidas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM detalle_salida")
    stats['total_detalle_salidas'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM historial_stock")
    stats['total_historial_stock'] = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM movimientos_stock")
    stats['total_movimientos_stock'] = cur.fetchone()['n']
    cur.close()
    return jsonify(stats)


@app.route('/api/informe-diario')
def api_informe_diario():
    hoy = datetime.now().strftime('%Y-%m-%d')
    fecha_str = request.args.get('fecha', hoy)
    cur = mysql.connection.cursor()

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
        SELECT s.id, s.fecha, s.venta_id,
               (SELECT SUM(ds.cantidad) FROM detalle_salida ds WHERE ds.salida_id=s.id) AS items
        FROM salidas s
        WHERE DATE(s.fecha)=%s ORDER BY s.fecha
    """, (fecha_str,))
    salidas_dia = cur.fetchall()
    for s in salidas_dia:
        if isinstance(s.get('fecha'), datetime):
            s['fecha'] = s['fecha'].isoformat()
    cur.execute("SELECT COUNT(*) AS n FROM salidas WHERE DATE(fecha)=%s", (fecha_str,))
    num_salidas = cur.fetchone()['n']

    cur.execute("""
        SELECT h.id, h.producto_id, p.nombre AS producto_nombre, h.tipo, h.cantidad,
               h.stock_anterior, h.stock_nuevo, h.notas, h.fecha
        FROM historial_stock h JOIN productos p ON h.producto_id=p.id
        WHERE DATE(h.fecha)=%s ORDER BY h.fecha
    """, (fecha_str,))
    historial_dia = cur.fetchall()
    for h in historial_dia:
        if isinstance(h.get('fecha'), datetime):
            h['fecha'] = h['fecha'].isoformat()

    cur.close()
    return jsonify({'fecha': fecha_str,
                    'ingresos_dia': ingresos_dia, 'num_ingresos': num_ingresos,
                    'salidas_dia': salidas_dia, 'num_salidas': num_salidas,
                    'movimientos_stock': historial_dia})


# ─────────────────────────────────────────────
# ERROR HANDLER (loguea el error exacto)
# ─────────────────────────────────────────────
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
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
