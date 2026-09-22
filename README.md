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
MYSQL_USER=proyecto
MYSQL_PASSWORD=tu_password_real
MYSQL_DB=proyecto_multiservicios_richard

# Segunda base de datos (almacen)
MYSQL_DB_ALMACEN=proyecto_gestion_almacen

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
2. Render usa automáticamente el `Procfile` de la raíz: `web: gunicorn app:app`.
3. En **Environment** agrega estas variables (las mismas de tu `.env`):
   ```
   MYSQL_HOST=mysql-proyecto.alwaysdata.net
   MYSQL_USER=proyecto
   MYSQL_PASSWORD=tu_password_real
   ```
4. Guarda y despliega. El enlace quedará: `https://TU-SERVICIO.onrender.com`.

---

## Bases de datos (alwaysdata)

- Host: `mysql-proyecto.alwaysdata.net`
- BD ventas: `proyecto_multiservicios_richard` (usuarios, ventas, detalle_venta, carrito, clientes, direcciones, seguimiento_entregas, entrega_productos, intentos_usuario, bloqueos_ip)
- BD almacen: `proyecto_gestion_almacen` (productos, ingresos, detalle_ingreso, salidas, detalle_salida, proveedores, productos_para_pedir)

---

## Notas

- El monolito de la raíz sirve **estática + portada + tienda + admin** en un solo puerto y es lo que se ve en el enlace de Render.
- Los microservicios comparten la misma BD pero cada uno corre en su propio puerto (5000/5001); la comunicacion entre ellos usa `X-Internal-Key`.
