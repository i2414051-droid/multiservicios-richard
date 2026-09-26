# Multiservicios Richard

Sistema web de **multiservicios** (tienda en línea de materiales + gestión de almacén). El repositorio contiene dos partes:

1. **Monolito (raíz)** — es lo que se despliega en Render (el enlace público). Una sola app Flask que atiende portada, tienda, admin, boletas, comprobantes y almacén, conectada directamente a la base de datos real.
2. **Microservicios (carpetas `ms_*`)** — la misma lógica separada en dos servicios independientes para correr localmente (ventas en `:5000` y almacén en `:5001`).

---

## Estructura

```
├── app.py                  ← Monolito (despliegue en Render via Procfile)
├── Procfile                ← web: gunicorn app:app
├── requirements.txt        ← Dependencias del monolito
├── static/                 ← CSS, JS, imagenes, uploads
├── templates/              ← HTML (portada, tienda, admin, boletas...)
├── .env.example            ← Plantilla de variables (sin credenciales reales)
│
├── ms_clientes_ventas/     ← Micro: ventas + tienda (puerto 5000)
│   ├── app.py
│   ├── Procfile
│   └── templates/
├── ms_gestion_almacen/     ← Micro: gestion de almacen (puerto 5001)
│   ├── app.py
│   ├── Procfile
│   └── templates/
│
├── arrancar/               ← Scripts para levantar los micros en local
│   ├── arrancar_ventas.ps1   (Python en :5000)
│   └── arrancar_almacen.ps1  (Python en :5001)
│
└── README.md
```

---

## Variables de entorno (`.env`)

Copia `.env.example` a `.env` y completa los valores reales:

```ini
# Base de datos principal (ventas)
MYSQL_HOST=mysql-proyecto.alwaysdata.net
MYSQL_PORT=3306
MYSQL_USER=proyecto
MYSQL_PASSWORD=tu_password_real
MYSQL_DB=proyecto_multiservicios_richard
MYSQL_CHARSET=utf8mb4
MYSQL_CONNECT_TIMEOUT=20

# Segunda base de datos (almacen)
MYSQL_DB_ALMACEN=proyecto_gestion_almacen

# Depuración: ponlo a 1 para ver el error exacto en pantalla (solo temporal)
DEBUG_ERRORS=0

# Microservicios internos
MS_ALMACEN_URL=http://localhost:5001
MS_VENTAS_URL=http://localhost:5000

# Correo SMTP (Gmail recomendado)
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=tu_correo@gmail.com
MAIL_PASSWORD=tu_app_password_gmail

# API Peru (DNI/RUC)
APIPERU_TOKEN=tu_token_aqui
```

> **Importante:** `.env` está en `.gitignore` — **nunca se sube a GitHub**. Solo se sube `.env.example` (plantilla sin credenciales). Las credenciales que usa Render se definen en las variables de entorno del propio servicio en el panel de Render.

---

## Correr en local (microservicios)

Desde la carpeta `arrancar/`:

```powershell
# Puerto 5000 — ventas / tienda
.\arrancar_ventas.ps1

# Puerto 5001 — almacen
.\arrancar_almacen.ps1
```

Luego abre:
- Tienda: http://localhost:5000
- API almacen: http://localhost:5001

> Los scripts cargan el `.env` de la raíz y levantan cada micro con su `Procfile` interno.

---

## Correr en local (monolito / como Render)

```powershell
pip install -r requirements.txt
gunicorn app:app
```

o

```powershell
python app.py
```

---

## Despliegue en Render

1. Conecta el repo `multiservicios-richard` (rama `main`) a un **Web Service** de Render.
2. El `Procfile` de la raíz ya incluye el puerto correcto: `web: gunicorn app:app --bind 0.0.0.0:$PORT`.
3. En **Environment** agrega estas variables (las mismas de tu `.env`):
   ```
   SECRET_KEY=tu_clave_secreta
   MYSQL_HOST=mysql-proyecto.alwaysdata.net
   MYSQL_PORT=3306
   MYSQL_USER=proyecto
   MYSQL_PASSWORD=tu_password_real
   MYSQL_DB=proyecto_multiservicios_richard
   MYSQL_DB_ALMACEN=proyecto_gestion_almacen
   MYSQL_CHARSET=utf8mb4
   ```
4. Guarda y despliega. El enlace quedará: `https://TU-SERVICIO.onrender.com`.

> **Importante:** sin las variables de entorno, la app intenta conectar a `localhost` y responde 500 en todas las páginas. Revisa que estén bien escritas (sin espacios) en Render → tu servicio → Environment.

## Solución de problemas (500 en producción)

Si el sitio responde *"Ocurrió un error interno. Revisa los logs."*, es un error en tiempo de ejecución (casi siempre la conexión a MySQL). Para ver la causa exacta:

1. En Render → tu servicio → **Logs → Runtime** busca la línea `Error no controlado:` con su `Traceback`.
2. Temporalmente puedes activar la variable `DEBUG_ERRORS=1` (Render → Environment → redeploy) y el error se mostrará en pantalla. Cuando termines, vuelve a `0`.
3. Errores típicos con alwaysdata:
   - `Can't connect to MySQL server ... (timed out)` → el usuario MySQL tiene **restricción por IP** en alwaysdata (panel → Databases → MySQL → tu usuario): quítala o añade las IPs de salida de Render, y verifica que no haya firewall.
   - `Access denied for user ...` → usuario/contraseña incorrectos, o al usuario le falta el permiso (usa opción **GRANT ALL PRIVILEGES** en alwaysdata sobre ambas bases de datos).
   - `Unknown database ...` → el nombre de la BD no coincide. En alwaysdata, las bases suelen llamarse `usuario_nombre` (revísalo en el panel → Databases).
4. El usuario MySQL de alwaysdata debe poder acceder a **ambas** bases de datos: `proyecto_multiservicios_richard` y `proyecto_gestion_almacen`.

---

## Bases de datos (alwaysdata)

- Host: `mysql-proyecto.alwaysdata.net`
- BD ventas: `proyecto_multiservicios_richard` (usuarios, ventas, detalle_venta, carrito, clientes, direcciones, seguimiento_entregas, entrega_productos, intentos_usuario, bloqueos_ip)
- BD almacen: `proyecto_gestion_almacen` (productos, ingresos, detalle_ingreso, salidas, detalle_salida, proveedores, productos_para_pedir)

---

## Notas

- El monolito de la raíz sirve **estática + portada + tienda + admin** en un solo puerto y es lo que se ve en el enlace de Render.
- Los microservicios comparten la misma BD pero cada uno corre en su propio puerto (5000/5001); la comunicacion entre ellos usa `X-Internal-Key`.
