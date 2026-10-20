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

**Login independiente de cada micro (Arreglo 2):**
- **Ventas (:5000):** login propio (`/login`) contra la tabla `usuarios` de la BD de ventas.
- **Almacén (:5001):** login propio (`/login`) con las credenciales de las variables `ALMACEN_ADMIN_USER` / `ALMACEN_ADMIN_PASSWORD` (por defecto `admin` / `almacen123`).

> Cada micro usa su **propia cookie de sesión** (ventas: `session`; almacén: `almacen_session`), así que son independientes: si el micro de Ventas se cae, el de Almacén sigue funcionando y viceversa.

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
- BD ventas: `proyecto_multiservicios_richard` (usuarios, ventas, detalle_venta, carrito, direcciones, seguimiento_entregas, entrega_productos, intentos_usuario, bloqueos_ip, cancelaciones, motivos_cancelacion, historial_estado_cliente, kpi_error_events, kpi_incidencias, kpi_sesiones_compra, kpi_busquedas)
- BD almacen: `proyecto_gestion_almacen` (productos, ingresos, detalle_ingreso, salidas, detalle_salida, proveedores, productos_para_pedir, movimientos_stock, historial_stock)

> Tablas retiradas (respaldo en `tablas_eliminadas_respaldo_*.sql`): `clientes`, `cancelacion`, `estados_cliente`.
> El estado de cada cliente vive en `usuarios.estado`; el catálogo de estados válidos está en el dict `ESTADOS_CLIENTE` de `app.py` (la tabla `estados_cliente` se eliminó: no se leía en ningún request y el app la repoblaba sola al arrancar).

---

## Modulo de KPIs (`kpi/`)

Panel en `/panel` (boton **KPIs** de Accesos Rapidos en `/dashboard`). Se recalcula
**en vivo** en cada peticion y se refresca solo cada 30 s.

### Como esta conectado

| Capa | Pieza | Que hace |
|---|---|---|
| Back-end | `kpi/repository.py` | Todo el SQL. Lee de las 2 bases (`ventas` de una, `almacen.productos` de la otra) |
| Back-end | `kpi/service.py` | Formulas de los indicadores y semaforo |
| Back-end | `kpi/settings.py` | Umbrales y catalogos (lo unico que hay que tocar para mover un objetivo) |
| Back-end | `kpi/schema.py` | Crea `kpi_incidencias` y anade `created_at` a `carrito` y `bloqueos_ip` |
| Front-end | `templates/kpi_biz/` | Panel (5 secciones) y macros de las tarjetas |
| Front-end | `static/js/kpi-biz-dashboard.js` | Cambio de periodo y registro de incidencias sin recargar |

### De donde sale cada dato

| Indicador | Tabla |
|---|---|
| Volumen de ventas / Ingresos brutos | `ventas` |
| Cancelacion por error del cliente / Indice de motivos | `cancelaciones` (LEFT JOIN `ventas`) |
| Frecuencia de compra por ciclo | `ventas` agrupado por `cliente_id` |
| Quiebre de stock / Dias de inventario | `almacen.productos` + `detalle_venta` |
| Devoluciones por incompatibilidad | `kpi_incidencias` (registro manual) |
| Bloqueo de cuentas / IPs bloqueadas | `intentos_usuario`, `bloqueos_ip` |
| Intentos fallidos de login | `intentos_usuario` |
| Abandono de carrito / Motivos | `carrito`, `kpi_incidencias` |

### El reloj: manda la base de datos

alwaysdata corre MySQL en **CEST (UTC+2)** y el contenedor de Render en **UTC**. Por eso
el modulo **no** usa `datetime.now()`: usa `SELECT NOW()`. Con el reloj local las
ventas mas recientes caian fuera del periodo y el panel mostraba cero pedidos con
ventas registradas. Las columnas que el KPI lee las escribe MySQL
(`CURRENT_TIMESTAMP`); `kpi_incidencias.fecha` y `carrito.created_at` tambien.

### Lo que el sistema no registra solo

Devoluciones, errores de picking, mermas, incidencias de transporte y motivos de
abandono no se deducen de ninguna tabla: se registran en la **seccion 5 del panel**
(Registro de Incidencias), que alimenta las secciones 2 y 4.

### Configuracion (variables de entorno)

```ini
KPI_PRESET=30d                      # periodo por defecto (hoy | 7d | 30d | mes)
KPI_CICLO_MANTENIMIENTO_DIAS=90      # ciclo de mantenimiento (frecuencia de compra)
KPI_CATEGORIA_REPUESTOS=Repuestos   # admite varias separadas por coma
KPI_CATEGORIA_ACCESORIOS=Accesorios
KPI_UMBRAL_BLOQUEO_LOGIN=5          # fallos que se consideran bloqueo
```

> Si un indicador de inventario sale en "sin datos", el panel lo dice: avisa de que
> no hay productos en la categoria configurada y lista las categorias que si existen
> en el catalogo.

### API (solo administradores)

```
GET    /api/kpi-biz              snapshot completo en JSON
GET    /api/kpi-biz/panel        fragmento HTML del panel (lo reinyecta el JS)
POST   /api/kpi-biz/incidencia   registrar incidencia
DELETE /api/kpi-biz/incidencia/<id>
GET    /api/kpi-biz/incidencias-recentes
```

---

### Registro del recorrido del cliente (`kpi/registro.py`)

El sistema no registraba ninguna interaccion del cliente salvo el carrito. Con este
modulo queda el rastro de lo que hace, para poder medir despues:

| Tabla | Una fila por | Se llena en |
|---|---|---|
| `kpi_sesiones_compra` | intencion de compra | `GET /agregar/<id>`, `GET /checkout`, `POST /procesar_compra` |
| `kpi_busquedas` | busqueda escrita en la tienda | `GET /?buscar=...`, `GET /producto/<id>` |

La sesion de compra avanza por los pasos `carrito -> checkout -> formulario -> venta`.
Si el formulario falla, `motivo_fallo` guarda el motivo (`documento_invalido`,
`stock_insuficiente`, `campos_obligatorios`, `carrito_vacio`, `telefono_invalido`,
`csrf_invalido`, `error_sistema`): saber **donde** se queda la gente no sirve de nada
sin saber **por que**.

`kpi_busquedas` guarda el texto, cuantos resultados dio y si el cliente acabo
comprando. Las busquedas **con 0 resultados** son la lista de lo que la gente pide y
no tenemos: la entrada para decidir que variedad traer.

> Ninguna de estas escrituras puede romper la tienda: todas van envueltas en
> try/except y usan la conexion propia del modulo de KPIs.

## Notas

- El monolito de la raíz sirve **estática + portada + tienda + admin** en un solo puerto y es lo que se ve en el enlace de Render.- Los microservicios comparten la misma BD pero cada uno corre en su propio puerto (5000/5001); la comunicacion entre ellos usa `X-Internal-Key`.
