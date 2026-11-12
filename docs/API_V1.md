# API v1 — Arquitectura e inventario de endpoints

Documento de diseño para exponer el monolito de `monolito/` como API JSON
consumible por la futura app móvil, sin romper la web actual.

- **Estado:** Fase 0, 1, 2 (núcleo) y 3 implementadas; falta integración contra MySQL real y el cargo Culqi.
- **Última actualización:** 2026-11-11.

---

## 1. Diagnóstico del punto de partida

| Métrica | Valor |
|---|---|
| Funciones de vista | 105 |
| Rutas únicas | 106 (123 combinaciones método + ruta) |
| Endpoints que ya devuelven JSON | 14 |
| Autenticación | 100 % por cookie de sesión (`user_id`, `rol`, `correo`) |
| Autorización | Inline en cada vista (`if 'user_id' not in session`, `_es_admin()`) |
| CSRF | Global en todo POST/PUT/PATCH/DELETE |
| Carrito | `session['guest_id']` para invitados, `user_id` para logueados |
| Respuestas | `render_template`, `redirect`, `flash`; solo 14 `jsonify` |
| Bases de datos | MySQL `ventas` + `almacen` |

Problemas para un cliente móvil:

1. Las rutas devuelven HTML o redirecciones, no JSON.
2. La identidad vive en una cookie; un móvil no maneja cookies de navegador ni CSRF.
3. Hay escrituras por `GET` (`/agregar/<id>`, `/eliminar_carrito/<id>`, etc.).
4. La lógica de negocio está dentro de las vistas (`procesar_compra` = 393 líneas).
5. `/api/endpoints` es un catálogo manual que se desincroniza.
6. `/init-db` y `/test_db` están expuestos.

**Decisión:** no reescribir el monolito. Se añade `/api/v1` al lado, reutilizando
la misma lógica mediante una capa `services/`. La web sigue funcionando igual.

---

## 2. Arquitectura

```
┌──────────────────────┐        ┌──────────────────────────┐
│  Web actual (HTML)   │        │  App móvil (Flutter/RN)  │
│  cookie + CSRF       │        │  Bearer JWT              │
└──────────┬───────────┘        └────────────┬─────────────┘
           │                                 │
           └──────────────┬──────────────────┘
                          ▼
                 ┌─────────────────┐
                 │   app.py        │  composition root
                 │   - vistas web  │
                 │   - /api/v1     │
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │   services/     │  lógica de negocio compartida
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │ repositories/   │  SQL
                 └────────┬────────┘
                          ▼
                    MySQL ventas + almacén
```

### 2.1 Autenticación híbrida (decisión aprobada)

| Cliente | Mecanismo | Motivo |
|---|---|---|
| Web (navegador) | Cookie de sesión + CSRF | Ya funciona; `HttpOnly`; no hay que reescribir 105 vistas |
| App móvil / API | JWT access + refresh rotativo | Estándar para APIs; el cliente adjunta el header; sin CSRF |

- `access_token`: 30 minutos.
- `refresh_token`: 30 días, rotativo, revocable y guardado **hasheado** en BD.
- Claims: `sub`, `rol`, `nombre`, `jti`, `iat`, `exp`, `typ`.
- Al cambiar contraseña o bloquear la cuenta se revocan todos los refresh.
- La API **no** usa cookies; la web **no** usa Bearer.
- `/api/v1/*` queda exento del CSRF global.

### 2.2 Carrito (decisión aprobada)

- **Se exige login para agregar al carrito.**
- No hay carrito de invitado en la API.
- `POST /api/v1/carrito/items` sin token responde `401`.
- Se elimina la necesidad de `X-Guest-Id` y de `POST /carrito/merge`.

### 2.3 Contrato

**Éxito**

```json
{ "ok": true, "data": {}, "meta": { "page": 1, "per_page": 20, "total": 57, "pages": 3 } }
```

**Error**

```json
{
  "ok": false,
  "error": {
    "code": "STOCK_INSUFICIENTE",
    "message": "Solo hay 3 unidades disponibles.",
    "details": { "producto_id": 12, "stock": 3 }
  }
}
```

| Tema | Regla |
|---|---|
| Dinero | `Decimal` → string `"12.50"`; `total_centimos` entero para Culqi |
| Fechas | ISO 8601 en hora de Lima (`-05:00`) |
| Paginación | `?page=1&per_page=20`, máximo 100 |
| Imágenes | URL absoluta; nunca el blob `imagen_datos` |
| Archivos | `multipart/form-data`; PDFs como `application/pdf` |
| Idempotencia | `Idempotency-Key` obligatorio en `POST /pedidos` |
| Escrituras | `POST` crear, `PATCH` actualizar, `DELETE` borrar |
| Documentación | OpenAPI 3.0 en `/api/v1/openapi.json` y Swagger en `/api/v1/docs` |

### 2.4 Estructura de carpetas

```
monolito/
├── app.py                      # composition root
├── api/
│   ├── __init__.py             # create_api(app)
│   ├── deps.py                 # current_user, require_role, paginación
│   ├── errors.py               # ApiError + handlers uniformes
│   ├── security.py             # JWT, refresh tokens, revocación
│   ├── serializers.py          # entidad → dict
│   ├── openapi.py              # generación del spec
│   └── v1/
│       ├── auth.py
│       ├── catalogo.py
│       ├── carrito.py
│       ├── pedidos.py
│       ├── direcciones.py
│       ├── reclamos.py
│       ├── admin_catalogo.py
│       ├── admin_ventas.py
│       ├── admin_clientes.py
│       ├── admin_almacen.py
│       ├── admin_reportes.py
│       ├── admin_kpi.py
│       └── sistema.py
├── services/
├── repositories/
└── tests/api/
```

---

## 3. Inventario de endpoints

Notación: **P** público, **C** cliente autenticado, **A** admin.

### 3.1 Autenticación y cuenta

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| POST | `/api/v1/auth/register` | P | `/registro` |
| POST | `/api/v1/auth/login` | P | `/login` |
| POST | `/api/v1/auth/refresh` | P | nuevo |
| POST | `/api/v1/auth/logout` | C | `/logout` |
| POST | `/api/v1/auth/password/forgot` | P | `/recuperar-password` |
| POST | `/api/v1/auth/password/reset` | P | `/restablecer-password/<token>` |
| GET | `/api/v1/auth/me` | C | `/perfil` |
| PATCH | `/api/v1/auth/me` | C | `/perfil` |
| PUT | `/api/v1/auth/me/password` | C | `/cambiar-password` |
| GET | `/api/v1/auth/me/estado` | C | `/api/mi-estado` |
| POST | `/api/v1/auth/me/revision` | C | `/perfil/solicitar-revision` |
| POST | `/api/v1/auth/google` | P | `/auth/google/callback` (fase posterior) |

### 3.2 Catálogo público

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/categorias` | P | `/api/categorias` |
| GET | `/api/v1/productos` | P | `/api/productos` |
| GET | `/api/v1/productos/{id}` | P | `/api/productos/{id}` |
| GET | `/api/v1/productos/{id}/imagenes` | P | nuevo |
| GET | `/api/v1/config/tienda` | P | `/ajustes` (subconjunto público) |
| GET | `/api/v1/health` | P | `/api/health` |
| GET | `/api/v1/openapi.json` | P | nuevo |
| GET | `/api/v1/docs` | P | nuevo |

### 3.3 Carrito

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/carrito` | C | `/api/carrito` |
| GET | `/api/v1/carrito/resumen` | C | `/carrito` |
| POST | `/api/v1/carrito/items` | C | `/agregar/{id}` |
| PATCH | `/api/v1/carrito/items/{producto_id}` | C | `/actualizar-cantidad/{id}` |
| DELETE | `/api/v1/carrito/items/{producto_id}` | C | `/eliminar_carrito/{id}` |
| DELETE | `/api/v1/carrito` | C | nuevo |

### 3.4 Checkout y pedidos

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/checkout/opciones` | C | `/checkout` |
| POST | `/api/v1/checkout/validar` | C | nuevo |
| POST | `/api/v1/pedidos` | C | `/procesar_compra` |
| GET | `/api/v1/pedidos` | C | `/historial` |
| GET | `/api/v1/pedidos/{id}` | C | `/confirmacion/{id}` |
| POST | `/api/v1/pedidos/{id}/cancelar` | C | `/mis-entregas/cancelar/{venta_id}` |
| GET | `/api/v1/pedidos/{id}/seguimiento` | C | `/mis-entregas` |
| GET | `/api/v1/pedidos/{id}/boleta` | C | `/boleta/{venta_id}` |
| GET | `/api/v1/pedidos/{id}/boleta.pdf` | C | `/boleta_pdf/{venta_id}` |
| POST | `/api/v1/pedidos/{id}/boleta` | C | `/guardar_boleta` |
| POST | `/api/v1/consultas/documento` | P | `/api/consultar-documento` |
| POST | `/api/v1/geocodificar` | C | `/api/geocodificar` |
| POST | `/api/v1/geocodificar/inverso` | C | `/api/geocodificar-inverso` |

### 3.5 Direcciones

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/direcciones` | C | `/direcciones` |
| POST | `/api/v1/direcciones` | C | `/direcciones/nueva` |
| GET | `/api/v1/direcciones/{id}` | C | nuevo |
| PATCH | `/api/v1/direcciones/{id}` | C | `/direcciones/editar/{id}` |
| DELETE | `/api/v1/direcciones/{id}` | C | `/direcciones/eliminar/{id}` |
| POST | `/api/v1/direcciones/{id}/predeterminada` | C | nuevo |

### 3.6 Reclamos (cliente)

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/reclamos` | C | `/mis-reclamaciones` |
| POST | `/api/v1/reclamos` | P/C | `/libro-de-reclamaciones` |
| GET | `/api/v1/reclamos/{id}` | C | nuevo |
| GET | `/api/v1/reclamos/{id}/constancia` | P | `/libro-de-reclamaciones/constancia/{token}` |

### 3.7 Admin — catálogo y ventas

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/admin/productos` | A | `/admin` |
| POST | `/api/v1/admin/productos` | A | `/agregar_producto` |
| GET | `/api/v1/admin/productos/{id}` | A | `/editar_producto` |
| PATCH | `/api/v1/admin/productos/{id}` | A | `/editar_producto` |
| POST | `/api/v1/admin/productos/{id}/imagen` | A | `/editar_producto` |
| DELETE | `/api/v1/admin/productos/{id}/imagen` | A | `/editar_producto` (quitar principal) |
| DELETE | `/api/v1/admin/productos/{id}/imagenes/{imagen_id}` | A | `/editar_producto` (galería) |
| POST | `/api/v1/admin/productos/{id}/activar` | A | `/activar_producto` |
| DELETE | `/api/v1/admin/productos/{id}` | A | `/eliminar_producto` |
| DELETE | `/api/v1/admin/productos/{id}?definitivo=true` | A | `/eliminar_producto_definitivo` |
| GET | `/api/v1/admin/ventas` | A | `/historial-compras` |
| GET | `/api/v1/admin/ventas/{id}` | A | detalle |
| PATCH | `/api/v1/admin/ventas/{id}/estado` | A | `/historial-compras/estado/{id}` |
| PATCH | `/api/v1/admin/ventas/{id}/visibilidad` | A | `/historial-compras/ocultar|mostrar` |
| DELETE | `/api/v1/admin/ventas/ocultas` | A | `/historial-compras/limpiar` |
| GET | `/api/v1/admin/ventas/export.csv` | A | nuevo |
| GET | `/api/v1/admin/ventas/export.pdf` | A | `/historial-compras/pdf` |

### 3.8 Admin — clientes y permisos

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/admin/clientes` | A | `/clientes` |
| GET | `/api/v1/admin/clientes/{id}` | A | nuevo |
| PATCH | `/api/v1/admin/clientes/{id}/estado` | A | `/clientes/{id}/estado` |
| GET | `/api/v1/admin/clientes/{id}/historial` | A | `/clientes/{id}/historial` |
| GET | `/api/v1/admin/usuarios` | A | `/permisos` |
| PATCH | `/api/v1/admin/usuarios/{id}/rol` | A | `/permisos` |

### 3.9 Admin — proveedores y pedidos a proveedor

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/admin/proveedores` | A | `/proveedores` |
| POST | `/api/v1/admin/proveedores` | A | `/proveedores` |
| PATCH | `/api/v1/admin/proveedores/{id}` | A | `/proveedores/editar/{id}` |
| DELETE | `/api/v1/admin/proveedores/{id}` | A | `/proveedores/eliminar/{id}` |
| GET | `/api/v1/admin/pedidos-compra` | A | `/productos-para-pedir` |
| PATCH | `/api/v1/admin/pedidos-compra/{id}` | A | `/productos-para-pedir/actualizar/{id}` |
| POST | `/api/v1/admin/pedidos-compra/{id}/proveedor` | A | `.../proveedor/{id}` |
| DELETE | `/api/v1/admin/pedidos-compra/{id}/proveedor` | A | `.../quitar-proveedor/{id}` |
| POST | `/api/v1/admin/pedidos-compra/marcar-enviado` | A | `.../marcar-enviado` |

**Implementado en la Fase 5.**

### 3.10 Admin — ingresos, salidas y entregas

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/admin/ingresos` | A | `/ingresos` |
| POST | `/api/v1/admin/ingresos` | A | `/registrar-ingreso` |
| GET | `/api/v1/admin/ingresos/{id}` | A | `/comprobante-ingreso/{id}` |
| POST | `/api/v1/admin/ingresos/{id}/anular` | A | `/ingresos/anular/{id}` |
| GET | `/api/v1/admin/ingresos/{id}.pdf` | A | `/ingresos/pdf/{id}` |
| GET | `/api/v1/admin/ingresos.pdf` | A | `/ingresos/pdf` |
| GET | `/api/v1/admin/salidas` | A | `/salidas` |
| POST | `/api/v1/admin/salidas` | A | `/registrar-salida` |
| GET | `/api/v1/admin/salidas/{id}` | A | `/comprobante-salida/{id}` |
| DELETE | `/api/v1/admin/salidas/{id}` | A | `/eliminar-salida/{id}` |
| GET | `/api/v1/admin/salidas/{id}.pdf` | A | nuevo |
| GET | `/api/v1/admin/entregas` | A | `/entregas` |
| POST | `/api/v1/admin/entregas` | A | `/entregas/crear/{venta_id}` |
| GET | `/api/v1/admin/entregas/{id}` | A | `/entregas/detalle/{id}` |
| PATCH | `/api/v1/admin/entregas/{id}` | A | `/entregas/actualizar/{id}` |
| DELETE | `/api/v1/admin/entregas/{id}` | A | `/entregas/eliminar/{id}` |
| POST | `/api/v1/admin/entregas/{id}/productos` | A | `/entregas/agregar-productos/{id}` |
| POST | `/api/v1/admin/ventas/{id}/entrega` | A | `/entregas/asignar/{venta_id}` |

**Implementado en la Fase 5.**

### 3.11 Admin — inventario, reportes y KPI

| Método | Ruta | Auth | Origen actual |
|---|---|---|---|
| GET | `/api/v1/admin/inventario/verificacion` | A | `/verificar-inventario` |
| GET | `/api/v1/admin/inventario/registros` | A | `/verificar-registros` |
| GET | `/api/v1/admin/informe-diario` | A | `/informe-diario` |
| GET | `/api/v1/admin/informe-diario.pdf` | A | `/informe-diario/pdf` |
| GET | `/api/v1/admin/reportes/cancelaciones` | A | `/reporte-cancelaciones` |
| GET | `/api/v1/admin/reportes/cancelaciones.csv` | A | `/reporte-cancelaciones.csv` |
| GET | `/api/v1/admin/reportes/cancelaciones.pdf` | A | `/reporte-cancelaciones.pdf` |
| GET | `/api/v1/admin/dashboard/grafico` | A | `/api/dashboard/grafico` |
| GET | `/api/v1/admin/kpi` | A | `/api/kpi-biz` |
| GET | `/api/v1/admin/kpi/incidencias` | A | `/api/kpi-biz/incidencias-recientes` |
| POST | `/api/v1/admin/kpi/incidencias` | A | `/api/kpi-biz/incidencia` |
| DELETE | `/api/v1/admin/kpi/incidencias/{id}` | A | `/api/kpi-biz/incidencia/{id}` |
| GET | `/api/v1/admin/ajustes` | A | `/ajustes` |
| PATCH | `/api/v1/admin/ajustes` | A | `/ajustes` |

**Implementado en la Fase 5:** `GET /admin/inventario/verificacion`. El resto
(`inventario/registros`, informes, reportes, KPI y ajustes) pertenece a la
Fase 6.

**Total: ~103 endpoints.**

### 3.12 Rutas que no se migran

| Ruta | Motivo |
|---|---|
| `/Iniciar Sesion`, `/Cerrar Sesion` | Alias con espacios |
| `/test_db`, `/init-db` | Diagnóstico/administración expuesta |
| `/api/endpoints` | Se reemplaza por OpenAPI |
| `/agregar/<id>`, `/eliminar_carrito/<id>`, `/aumentar-cantidad/<id>` | GET que escriben |
| `/preview_boleta`, `/reclamaciones/<id>/imprimir` | Vistas HTML de impresión |
| `/boleta`, `/checkout` (formularios) | La app usa sus propias pantallas |

---

## 4. Plan por fases

| Fase | Alcance | Estado |
|---|---|---|
| 0 | Scaffolding `api/`, errores, serializers, JWT + refresh, `/auth/*`, OpenAPI, health | Completada (falta prueba contra MySQL real) |
| 1 | Catálogo, categorías, imágenes, configuración, carrito | Completada (pruebas simuladas; falta MySQL real) |
| 2 | Checkout, pedidos, Culqi, idempotencia, historial, seguimiento, boletas | Completada sin cobro real; Culqi pendiente |
| 3 | Perfil, direcciones, reclamos | Completada (perfil en Fase 0; pruebas simuladas) |
| 4 | Admin: productos, ventas, clientes, permisos | Completada (pruebas simuladas; falta MySQL real) |
| 5 | Admin: proveedores, ingresos, salidas, entregas, inventario | Pendiente |
| 6 | Reportes, dashboard, KPI, ajustes | Pendiente |
| 7 | Hardening, tests, documentación, deprecación de `/api/*` | Pendiente |

---

## 5. Decisiones cerradas

1. JWT con refresh rotativo para la API móvil.
2. Híbrido: web con cookie + CSRF; API con Bearer.
3. Login obligatorio para agregar al carrito.
4. PDFs binarios; imágenes por URL.
5. Construir todos los endpoints ahora; integrarlos a la app después.
6. Todo dentro del monolito Flask.
7. Rutas en español bajo `/api/v1`.
8. Google OAuth se implementa en una fase posterior.

---

## 6. Fase 0 implementada

### 6.1 Archivos nuevos

```
monolito/
├── api/
│   ├── __init__.py          create_api(app, mysql, bcrypt)
│   ├── deps.py              get_mysql, get_bcrypt, cuerpo_json, paginacion
│   ├── errors.py            ApiError, ok(), fail(), handlers, mapeo JWT
│   ├── security.py          JWTManager, refresh rotativo, guards por rol
│   ├── serializers.py       usuario, tokens, dinero, fechas
│   ├── openapi.py           spec 3.0.3 generado desde las rutas + Swagger UI
│   ├── schema.sql           DDL de auth_refresh_tokens / api_idempotencia
│   └── v1/
│       ├── __init__.py      blueprint /api/v1
│       ├── auth.py          registro, login, refresh, logout, cuenta
│       └── sistema.py       health, version
├── services/
│   ├── __init__.py
│   ├── seguridad.py         política de contraseña, bloqueo, reCAPTCHA
│   └── auth_service.py      SQL de usuarios y refresh tokens
└── tests/
    ├── conftest.py
    └── api/test_auth_contract.py
```

### 6.2 Endpoints disponibles

| Método | Ruta | Auth |
|---|---|---|
| POST | `/api/v1/auth/register` | Público |
| POST | `/api/v1/auth/login` | Público |
| POST | `/api/v1/auth/refresh` | Refresh token |
| POST | `/api/v1/auth/logout` | Cliente |
| POST | `/api/v1/auth/password/forgot` | Público |
| POST | `/api/v1/auth/password/reset` | Público |
| GET | `/api/v1/auth/me` | Cliente |
| PATCH | `/api/v1/auth/me` | Cliente |
| PUT | `/api/v1/auth/me/password` | Cliente |
| GET | `/api/v1/auth/me/estado` | Cliente |
| POST | `/api/v1/auth/me/revision` | Cliente |
| GET | `/api/v1/health` | Público |
| GET | `/api/v1/version` | Público |
| GET | `/api/v1/openapi.json` | Público |
| GET | `/api/v1/docs` | Público (Swagger UI) |

### 6.3 Flujo de tokens

1. `login` o `register` devuelve `access_token` (30 min) y `refresh_token` (30 días).
2. El refresh se guarda **hasheado con SHA-256** en `auth_refresh_tokens`
   junto con `jti`, `device_id`, `user_agent`, `ip` y `expira_en`.
3. `POST /auth/refresh` recibe el refresh en `Authorization: Bearer`, verifica
   que la fila siga vigente, **revoca la anterior** y emite un par nuevo
   (rotación). Un refresh reutilizado o revocado devuelve `401`.
4. `POST /auth/logout` revoca el refresh enviado en el body. Con
   `{"todas": true}` revoca todos los refresh y sube `usuarios.token_version`,
   lo que invalida también los access tokens ya emitidos.
5. Cambiar o restablecer la contraseña incrementa `usuarios.token_version` y
   revoca todos los refresh. Los access tokens viejos dejan de servir de
   inmediato porque el guard compara `ver` contra la BD.
6. La web no usa nada de esto: sigue con cookie de sesión y CSRF.

### 6.4 Esquema

`api/security.py:asegurar_esquema()` crea de forma perezosa e idempotente:

- `auth_refresh_tokens` (refresh rotativo, índice por usuario y expiración).
- `api_idempotencia` (reservada para `POST /pedidos` en la Fase 2).
- `usuarios.token_version` (`ALTER TABLE` tolerante a que ya exista).

También está el DDL manual en `monolito/api/schema.sql`.

### 6.5 Web intacta

- `/api/v1/*` queda exento del CSRF global (`_exigir_csrf`).
- El resto de `/api/*` y todas las vistas HTML conservan su comportamiento.
- Los límites de intentos de la API se agregaron a `_LIMITES`:
  login 12/min, registro 10/min, refresh 30/min, recuperación 5/min.
- El 429 de `/api/v1` usa el mismo envelope de error que el resto de la API.

---

## 7. Fase 1 implementada

### 7.1 Archivos

```
monolito/
├── services/
│   └── tienda_service.py       SQL de catálogo, carrito, ajustes y envío
├── api/v1/
│   ├── catalogo.py             categorías, productos, imágenes, configuración
│   └── carrito.py              carrito autenticado
└── tests/api/
    └── test_catalogo_carrito.py
```

### 7.2 Catálogo público

| Método | Ruta | Respuesta |
|---|---|---|
| GET | `/api/v1/categorias` | Categorías fijas + conteo de activos y disponibles |
| GET | `/api/v1/productos` | Lista paginada; `buscar`, `categoria`, `orden`, `solo_disponibles` |
| GET | `/api/v1/productos/{id}` | Ficha con galería |
| GET | `/api/v1/productos/{id}/imagenes` | Solo URLs de imágenes |
| GET | `/api/v1/config/tienda` | Moneda, empresa, envío y categorías |

`GET /productos` acepta `page`, `per_page` (máx. 100), `buscar`, `categoria`,
`orden` (`nuevo`, `precio_asc`, `precio_desc`, `nombre_asc`, `nombre_desc`) y
`solo_disponibles`. Devuelve `data` como array y `meta` con
`page/per_page/total/pages`.

Cada producto incluye:

```json
{
  "id": 12,
  "nombre": "Taladro",
  "precio": "120.50",
  "precio_centimos": 12050,
  "stock": 5,
  "categoria": "Herramientas",
  "disponible": true,
  "imagen": "https://host/static/imagenes/thumbs/taladro.jpg",
  "imagen_original": "https://host/static/imagenes/taladro.jpg",
  "imagenes": ["https://host/static/imagenes/taladro.jpg"],
  "imagenes_thumb": ["https://host/static/imagenes/thumbs/taladro.jpg"],
  "imagenes_extra": []
}
```

Nunca se devuelve `imagen_datos` ni `imagen_mime`; si la imagen no existe en
disco, la ruta `static` existente sigue sirviendo el blob desde la BD.

### 7.3 Carrito autenticado

Todos los endpoints exigen `Authorization: Bearer <access_token>` y responden
`401` sin token. No hay carrito de invitado ni `POST /carrito/merge`.

| Método | Ruta | Cuerpo | Respuesta |
|---|---|---|---|
| GET | `/api/v1/carrito` | — | `items`, totales y reglas de envío |
| GET | `/api/v1/carrito/resumen` | — | Totales sin `items` |
| POST | `/api/v1/carrito/items` | `{"producto_id": 12, "cantidad": 2}` | `201` con `item` y `carrito` |
| PATCH | `/api/v1/carrito/items/{producto_id}` | `{"cantidad": 3}` | `item` y `carrito`; `0` elimina |
| DELETE | `/api/v1/carrito/items/{producto_id}` | — | Idempotente; `eliminado` |
| DELETE | `/api/v1/carrito` | — | `eliminados` y carrito vacío |

Reglas:

- Solo productos `estado='activo'` y con `stock > 0`.
- La cantidad se suma a lo que ya hay en el carrito.
- Tope de 500 unidades por producto.
- La cantidad nunca supera el stock; si se intenta, `409 STOCK_INSUFICIENTE`
  con `details.stock`, `details.en_carrito` y `details.maximo`.
- `PATCH` con `cantidad: 0` elimina la línea.
- La tabla `carrito` no tiene clave única histórica: la API agrupa duplicados y
  los normaliza al escribir.
- El producto se bloquea con `SELECT ... FOR UPDATE` durante la escritura para
  serializar compras concurrentes del mismo producto.
- El carrito usa `str(usuario_id)` porque `carrito.usuario_id` es `VARCHAR(200)`.

Totales:

- `total` y `total_centimos`: suma de todas las líneas.
- `total_disponible`: solo líneas comprables (activas y con stock suficiente).
- `envio.costo`: `15.00` por defecto; `envio.gratis` si `total >= 300.00`.
- `envio.minimo_delivery`: `50.00`; `envio.elegible_delivery` indica si aplica.
- `envio.distritos`: `Chilca`, `El Tambo`, `Huancayo`.
- `total_con_envio`: total + envío estimado si el delivery es elegible; si no,
  queda igual al total (recojo en tienda).

### 7.4 Pruebas

`monolito/tests/api/test_catalogo_carrito.py` agrega 27 casos sin MySQL:

- categorías, listado, filtros, paginación y ficha de producto,
- URLs absolutas de imágenes y ausencia de blobs,
- configuración y reglas de envío,
- `401` sin token,
- carrito vacío, agregar, acumular, actualizar, eliminar y vaciar,
- stock insuficiente, sin stock, producto inactivo/inexistente y tope de 500,
- agrupación de líneas duplicadas históricas.

Total actual: **46 pruebas** (`19` de auth + `27` de catálogo/carrito).

Integración pendiente: las pruebas reales contra MySQL siguen bloqueadas por
`MYSQL_PASSWORD=tu_password_aqui` en `.env`.

---

## 8. Fase 2 implementada (sin cobro real)

### 8.1 Alcance

- Se aceptan `transferencia` y `contra_entrega`; la venta nace con
  `estado_pago='pendiente'`.
- `tarjeta` y `yape` responden `409 PAGO_NO_DISPONIBLE` hasta que se habilite
  el cargo Culqi (flujo 3DS aparte).
- En esta fase no se guarda ningún dato de tarjeta ni token de Culqi.

### 8.2 Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/checkout/opciones` | Carrito, direcciones, métodos de pago y reglas de envío |
| POST | `/api/v1/checkout/validar` | Valida y calcula totales sin escribir |
| POST | `/api/v1/pedidos` | Crea el pedido; exige `Idempotency-Key` |
| GET | `/api/v1/pedidos` | Historial paginado del usuario |
| GET | `/api/v1/pedidos/{id}` | Detalle con items y seguimiento |
| POST | `/api/v1/pedidos/{id}/cancelar` | Cancela, repone stock y registra motivo |
| GET | `/api/v1/pedidos/{id}/seguimiento` | Estado de entrega |
| GET | `/api/v1/pedidos/{id}/boleta` | Boleta en JSON |
| GET | `/api/v1/pedidos/{id}/boleta.pdf` | Boleta binaria `application/pdf` |
| POST | `/api/v1/pedidos/{id}/boleta` | Actualiza documento/nombre de la boleta |

### 8.3 Reglas de creación

- `Idempotency-Key` obligatorio (8 a 120 caracteres). Repetir la misma clave
  con el mismo cuerpo devuelve la venta ya creada; con otro cuerpo responde
  `409 IDEMPOTENCIA_CONFLICTO`.
- El stock se descuenta con `UPDATE ... SET stock=stock-%s WHERE stock>=%s`;
  si otro cliente compró antes, se aborta la transacción (`409
  STOCK_INSUFICIENTE`) y no se crea la venta.
- `delivery` exige subtotal >= `delivery_minimo` (50), dirección y un punto
  dentro de Chilca/El Tambo/Huancayo, o un distrito de cobertura.
- `recojo` usa la dirección de la tienda y costo de envío 0.
- Al crear la venta se vacía el carrito y se completan los datos del perfil
  con `IFNULL`, igual que la web.
- La cancelación repone stock, marca `ventas.estado='cancelado'` y guarda el
  motivo en `cancelaciones`.

### 8.4 Archivos

```
monolito/
├── services/
│   └── pedidos_service.py      checkout, alta, historial y cancelación
├── api/v1/
│   └── pedidos.py              endpoints de la Fase 2
└── tests/api/
    └── test_pedidos_contract.py
```

### 8.5 Pruebas

`monolito/tests/api/test_pedidos_contract.py` agrega 23 casos:

- checkout/opciones y validación (recojo, delivery mínimo, pago bloqueado),
- alta con y sin `Idempotency-Key`, replay y conflicto,
- sin stock y carrito vacío,
- delivery con coordenadas y envío,
- historial, detalle y ownership,
- cancelación, motivo inválido y pedido entregado,
- seguimiento y boletas (JSON, PDF y actualización).

Total actual: **69 pruebas** (`19` auth + `27` catálogo/carrito + `23` pedidos).

### 8.6 Pendiente de la Fase 2

- Cargo Culqi real (tarjeta/Yape, 3DS) cuando haya claves y se cierre el flujo.
- `POST /consultas/documento` (RENIEC/SUNAT) y geocodificación.

---

## 9. Fase 3 implementada (perfil, direcciones y reclamos)

### 9.1 Perfil

El perfil ya quedó en la Fase 0: `GET`/`PATCH /api/v1/auth/me`,
`PUT /api/v1/auth/me/password`, `GET /api/v1/auth/me/estado` y
`POST /api/v1/auth/me/revision`. La Fase 3 no agrega nada nuevo aquí.

### 9.2 Direcciones

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/direcciones` | Lista con la predeterminada primero |
| POST | `/api/v1/direcciones` | Crea una dirección |
| GET | `/api/v1/direcciones/{id}` | Detalle |
| PATCH | `/api/v1/direcciones/{id}` | Actualiza solo los campos enviados |
| DELETE | `/api/v1/direcciones/{id}` | Elimina (idempotente) |
| POST | `/api/v1/direcciones/{id}/predeterminada` | Marca como predeterminada |

Reglas:

- `direccion` obligatoria (5 a 300 caracteres); distrito y referencia opcionales.
- Marcar una como predeterminada desmarca las demás del mismo usuario.
- Cada dirección pertenece al `usuario_id` (guardado como texto); pedir una
  ajena responde `404`, sin revelar si existe.

### 9.3 Reclamos (Libro de Reclamaciones)

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| GET | `/api/v1/reclamos` | C | Hojas registradas con el correo de la cuenta |
| POST | `/api/v1/reclamos` | P/C | Registra la hoja y devuelve el token |
| GET | `/api/v1/reclamos/{id}` | C | Detalle (solo del correo de la cuenta) |
| GET | `/api/v1/reclamos/{id}/constancia` | P/C | Constancia por `?token=` o sesión del dueño |

Reglas:

- Misma validación que el formulario web (nombre, DNI/CE/pasaporte, domicilio,
  correo, detalle ≥15, pedido ≥5, monto opcional).
- Se genera un `token` para la constancia y un `codigo` legible `LR-<año>-<id>`.
- El plazo de respuesta es `DIAS_RESPUESTA_RECLAMO` (15 días por defecto);
  `dias_restantes` y `vencido` se calculan al momento de responder.
- La constancia exige el token del envío o la sesión del dueño; sin ninguno de
  los dos responde `403 CONSTANCIA_NO_AUTORIZADA`.

### 9.4 Archivos

```
monolito/
├── services/
│   ├── direcciones_service.py
│   └── reclamos_service.py
├── api/v1/
│   ├── direcciones.py
│   └── reclamos.py
└── tests/api/
    └── test_direcciones_reclamos.py
```

### 9.5 Pruebas

`monolito/tests/api/test_direcciones_reclamos.py` agrega 20 casos: direcciones
(listado, alta, validación, predeterminada, edición parcial, borrado y
ownership) y reclamos (alta pública, validación, listado por correo, detalle,
constancia por token/sesión y vencimiento).

Total acumulado tras la Fase 3: **89 pruebas**.

---

## 10. Fase 4 implementada (admin: productos, ventas, clientes y permisos)

Toda la API de administración vive bajo `requiere_admin` (rol `admin`,
`administrador` o `superadmin`). Responde `403 SIN_PERMISO` con JWT de cliente.
El "administrador principal" se configura con `SUPERADMIN_CORREO`
(por defecto `admin@mail.com`) y conserva las reglas de la web.

### 10.1 Productos

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/admin/productos` | Lista activos e inactivos (`buscar`, `page`, `per_page`) |
| POST | `/api/v1/admin/productos` | Crea el producto (JSON, sin imagen) |
| GET | `/api/v1/admin/productos/{id}` | Detalle con la galería y sus ids |
| PATCH | `/api/v1/admin/productos/{id}` | Actualiza solo los campos enviados |
| POST | `/api/v1/admin/productos/{id}/imagen` | Sube 1..N imágenes (`multipart/form-data`) |
| DELETE | `/api/v1/admin/productos/{id}/imagen` | Quita la principal (promueve la primera de la galería) |
| DELETE | `/api/v1/admin/productos/{id}/imagenes/{imagen_id}` | Elimina una imagen de la galería |
| POST | `/api/v1/admin/productos/{id}/activar` | Vuelve a `activo` |
| DELETE | `/api/v1/admin/productos/{id}` | Desactiva (`estado='inactivo'`) |
| DELETE | `/api/v1/admin/productos/{id}?definitivo=true` | Borra el producto y su galería |

Reglas:

- `POST /admin/productos` **no exige imagen** (a diferencia del formulario web):
  se crea el producto y luego se sube la foto con `POST .../imagen`. Así el
  cliente móvil puede crear y adjuntar en dos pasos sin depender de multipart
  para todo el alta.
- La imagen se valida por sus **primeros bytes** (JPG, PNG, GIF, WEBP o AVIF),
  nunca por la extensión ni el `Content-Type`; el tope es 4 (`MAX_IMAGENES`).
- `principal=true` reemplaza la imagen principal; el resto se agrega a la
  galería por `orden`.
- `PATCH` no toca las imágenes; la galería se gestiona con los endpoints
  dedicados.
- Tras crear o cambiar el stock se sincroniza `productos_para_pedir`, igual que
  la web.

### 10.2 Ventas

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/admin/ventas` | Filtros: `buscar`, `motivo`, `estado`, `pago`, `entrega`, `distrito`, `categoria`, `ocultas` |
| GET | `/api/v1/admin/ventas/{id}` | Detalle con items |
| PATCH | `/api/v1/admin/ventas/{id}/estado` | `entregado`, `en espera` o `cancelado` |
| PATCH | `/api/v1/admin/ventas/{id}/visibilidad` | `{ "oculta": true/false }` |
| DELETE | `/api/v1/admin/ventas/ocultas` | Borra el historial completo |
| GET | `/api/v1/admin/ventas/export.csv` | CSV (`;`, BOM UTF-8) |
| GET | `/api/v1/admin/ventas/export.pdf` | PDF (binario) |

Reglas:

- `cancelado` es terminal: intentar reabrirlo responde `409 VENTA_CANCELADA`.
- Cancelar devuelve stock (`restaurar_stock_venta`) y registra el motivo
  (`registrar_cancelacion('otro', 'Cancelado por administrador', 'admin')`).
- Por defecto se ocultan las ventas con `oculta=1`; `ocultas=1` las incluye.
- El `meta` de la lista incluye `gran_total` (entregadas), `monto_en_espera` y
  `ocultas`, todos en string y `*_centimos`.
- El PDF de la Fase 4 es un listado simple con los filtros aplicados (resumen
  por venta); no reproduce el selector de periodo mes/semana de la web.

### 10.3 Clientes y permisos

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/admin/clientes` | `buscar`, `filtro` (estado), `page`, `per_page` |
| GET | `/api/v1/admin/clientes/{id}` | Detalle con compras no canceladas |
| PATCH | `/api/v1/admin/clientes/{id}/estado` | `{ "estado", "motivo" }` |
| GET | `/api/v1/admin/clientes/{id}/historial` | Últimos 50 cambios |
| GET | `/api/v1/admin/usuarios` | Usuarios y rol (orden admin primero) |
| PATCH | `/api/v1/admin/usuarios/{id}/rol` | `{ "rol": "admin" | "cliente" }` |

Reglas:

- Estados válidos: `activo`, `inactivo`, `bloqueado`,
  `pendiente_verificacion`, `moroso`.
- No puedes bloquearte/inactivarte a ti mismo (`409 AUTO_BLOQUEO`).
- Solo el administrador principal puede cambiar el estado de otro
  administrador o cambiar roles (`403 SIN_PERMISO`).
- Cada cambio de estado se registra en `historial_estado_cliente`.

### 10.4 Archivos

```
monolito/
├── services/
│   ├── admin_productos_service.py
│   ├── admin_ventas_service.py
│   └── admin_clientes_service.py
├── api/v1/
│   ├── admin_productos.py
│   ├── admin_ventas.py
│   └── admin_clientes.py
└── tests/api/
    └── test_admin.py
```

### 10.5 Pruebas

`monolito/tests/api/test_admin.py` agrega 43 casos: permisos (403 de cliente,
401 sin token), productos (listar/buscar, crear, validar, ver, actualizar,
activar, desactivar, borrado definitivo, subir imagen válida/principal/inválida,
tope de 4 con y sin principal, borrar galería y quitar principal promoviendo),
ventas (listar y contar, filtro de ocultas y estado, detalle, cambio de estado,
cancelación con devolución de stock, ya cancelada, visibilidad, CSV/PDF y
limpieza) y clientes (listar, ver, cambiar estado, auto-bloqueo, estado de otro
admin, historial, listar usuarios y cambiar rol).

Total acumulado tras la Fase 4: **132 pruebas**.

---

## 11. Fase 5 implementada (admin: proveedores, ingresos, salidas, entregas e inventario)

### 11.1 Alcance

Cierra el bloque de almacén del panel: proveedores y pedidos a proveedor
(`productos_para_pedir`), ingresos con anulación y PDFs, salidas con
eliminación que revierte stock, entregas/seguimiento con cancelación que
devuelve stock, y la verificación de inventario por tramos.

### 11.2 Endpoints

Los tres bloques del inventario (3.9, 3.10 y `GET /admin/inventario/verificacion`
de 3.11) quedan implementados. Los PDF (`ingresos.pdf`, `ingresos/{id}.pdf` y
`salidas/{id}.pdf`) devuelven `application/pdf` binario con
`Content-Disposition: attachment`; el resto son JSON bajo el envelope
`{ok, data, meta}`.

### 11.3 Reglas replicadas

- **Proveedores:** DNI (nombres + apellidos, solo letras) o RUC (razón social);
  documento de 8/11 dígitos; celular de 9 dígitos; documento + categoría
  únicos (`409 PROVEEDOR_DUPLICADO`). `nombre` se guarda ya armado.
- **Pedidos a proveedor:** solo `estado='pendiente'`; un proveedor borrado
  cuenta como “sin asignar”; asignar exige la misma categoría del producto
  (`409 CATEGORIA_DISTINTA`); `marcar-enviado` cierra todos los pendientes del
  proveedor y la respuesta trae los grupos con `whatsapp_url`.
- **Ingresos:** sube stock, actualiza `precio` y reactiva el producto; guarda la
  “foto” del proveedor y de cada producto; `anular` es terminal (`409
  INGRESO_ANULADO`), revierte el stock y reevalúa pedidos; el PDF semanal/mensual
  excluye los anulados de los totales (`404 SIN_DATOS` si el periodo no tiene
  ingresos, `422` si el periodo es inválido).
- **Salidas:** descuenta stock sin bajar de 0 (`409 STOCK_INSUFICIENTE` con el
  detalle por producto); eliminar devuelve el stock y reevalúa pedidos; el PDF
  es nuevo respecto a la web.
- **Entregas:** `cancelado` es terminal también en la venta (`409
  ENTREGA_CANCELADA`); el estado se replica a `ventas.estado`
  (en espera/entregado/cancelado); cancelar como admin devuelve el stock y
  registra el motivo vía `registrar_cancelacion('otro', ...)`.
- **Inventario:** tramos con una sola fuente de umbrales
  (`STOCK_CRITICO`/`STOCK_NORMAL`): sin_stock, crítico, bajo, normal e
  inactivos; `meta` incluye los contadores y los umbrales usados.

### 11.4 Archivos

```
monolito/
├── services/
│   ├── admin_proveedores_service.py
│   ├── admin_ingresos_service.py
│   ├── admin_salidas_service.py
│   ├── admin_entregas_service.py
│   └── admin_inventario_service.py
├── api/v1/
│   ├── admin_proveedores.py
│   ├── admin_ingresos.py
│   ├── admin_salidas.py
│   ├── admin_entregas.py
│   └── admin_inventario.py
├── api/
│   ├── serializers.py   (+ serializadores de proveedor/pedido/ingreso/salida/entrega/inventario)
│   └── openapi.py       (+ esquemas de la Fase 5 y `proveedor_id`/`pedido_id` enteros)
└── tests/api/
    ├── conftest.py      (resetea el contador de rate limit entre pruebas)
    └── test_almacen.py
```

### 11.5 Pruebas

`monolito/tests/api/test_almacen.py` agrega **59 casos** con una BD simulada:
permisos (403 de cliente, 401 sin token), proveedores (listar/filtrar, crear
DNI/RUC, documento/celular inválidos, duplicado, ver, actualizar, eliminar),
pedidos (listar, sin_asignar, cantidad, asignar/quitar proveedor, categoría
distinta, marcar enviado), ingresos (listar, crear y subir stock, sin ítems,
producto repetido, proveedor inexistente, detalle, anular y revertir stock,
motivo inválido, PDF de ingreso y de periodo), salidas (listar, crear y
descontar stock, stock insuficiente, venta inexistente, detalle, eliminar y
revertir stock, PDF), entregas (listar, crear, duplicada, detalle, pasar a
entregado, cancelar y devolver stock, cancelada terminal, eliminar, productos,
asignar) e inventario (contadores y filtros).

`tests/api/conftest.py` limpia `app._RATE` antes de cada prueba (el límite POST
global es por IP y acumulaba entre archivos dentro de la ventana de 60 s).

Total acumulado tras la Fase 5: **191 pruebas**.

---

## 12. Cómo ejecutar y probar

```powershell
# Desde la raíz del repo
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r monolito\requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest monolito\tests\api -q

# Servidor local
cd monolito
..\.venv\Scripts\python.exe app.py
```

- Swagger UI: <http://localhost:5000/api/v1/docs>
- OpenAPI JSON: <http://localhost:5000/api/v1/openapi.json>
- Health: <http://localhost:5000/api/v1/health>

### Variables de entorno nuevas

| Variable | Default | Uso |
|---|---|---|
| `API_V1_ENABLED` | `1` | Monta o no el blueprint `/api/v1` |
| `JWT_SECRET_KEY` | `SECRET_KEY` | Firma de los JWT; usar valor distinto en producción |
| `JWT_ACCESS_MINUTOS` | `30` | Vida del access token |
| `JWT_REFRESH_DIAS` | `30` | Vida del refresh token |
| `PUBLIC_BASE_URL` | `request.host_url` | Base del enlace de recuperación enviado por correo |
| `RECAPTCHA_SITE_KEY` / `RECAPTCHA_SECRET_KEY` | vacío | Si están, el registro API exige captcha |
| `RECAPTCHA_VALIDAR_HOST` | `0` | `1` para exigir que el captcha se resolvió en `RECAPTCHA_HOST` |
| `SUPERADMIN_CORREO` | `admin@mail.com` | Correo del administrador principal (cambia estados de otros admins y roles) |

### Pruebas

`monolito/tests/api/test_auth_contract.py` cubre 19 casos sin MySQL:

- envelope de éxito y error,
- OpenAPI y Swagger,
- 401 sin token, token inválido, expirado y revocado,
- 403 con cuenta bloqueada,
- 422 de validación,
- 404/405 con formato JSON,
- emisión de tokens y hash del refresh,
- login correcto e incorrecto con una BD simulada,
- rotación del refresh token,
- SQL de actualización del perfil.

Las pruebas de integración reales (login contra MySQL, refresh, logout,
recuperación) quedan pendientes de credenciales válidas en `.env`.

> **Verificación de solo lectura (2026-11-11):** con el `.env` de alwaysdata se
> comprobó que la conexión funciona y que **todas las consultas de lectura de
> la Fase 5** (listar/obtener y sus filtros de proveedores, pedidos, ingresos,
> salidas, entregas e inventario) se ejecutan correctamente contra el esquema
> real (5 proveedores, 35 productos, 33 pedidos, 9 ingresos, 6 ventas, 7
> seguimientos). No se escribió nada: no se importó `app.py` (su import corre
> `init_db()`) y se usó un módulo `app` falso en memoria para resolver los
> imports diferidos de los servicios.
>
> **Pendiente para el end-to-end HTTP:** la base real todavía no tiene las
> tablas propias de la API (`auth_refresh_tokens`, `api_idempotencia`) ni la
> columna `usuarios.token_version`; las crea `asegurar_esquema()` de forma
> aditiva (CREATE TABLE IF NOT EXISTS + ALTER ADD COLUMN) en el primer login o
> request autenticado. Crearlas es la única escritura necesaria para probar
> login/refresh y los endpoints autenticados con datos reales.

La Fase 1 agrega `monolito/tests/api/test_catalogo_carrito.py` con 27 casos
(catálogo, imágenes, configuración, carrito y validaciones de stock). La Fase 2
agrega `monolito/tests/api/test_pedidos_contract.py` con 23 casos (checkout,
pedidos, idempotencia, stock, cancelación y boletas). La Fase 3 agrega
`monolito/tests/api/test_direcciones_reclamos.py` con 20 casos (direcciones y
Libro de Reclamaciones). La Fase 4 agrega `monolito/tests/api/test_admin.py` con
43 casos (productos, imágenes, ventas, clientes y permisos). La Fase 5 agrega
`monolito/tests/api/test_almacen.py` con 59 casos (proveedores, pedidos a
proveedor, ingresos, salidas, entregas e inventario). Total actual: **191
pruebas**.

---

## 13. Estado y siguientes pasos

- **Fase 0:** código completo; login/refresh sin probar de punta a punta contra
  la base real (faltan las tablas de la API, ver nota en la sección 12).
- **Fase 1:** catálogo y carrito completos; lectura validada contra MySQL real.
- **Fase 2:** checkout/pedidos/boletas completos sin cobro real; falta decidir el
  flujo Culqi real.
- **Fase 3:** direcciones y reclamos completos.
- **Fase 4:** productos, ventas, clientes y permisos completos con pruebas
  simuladas.
- **Fase 5:** proveedores, pedidos a proveedor, ingresos, salidas, entregas e
  inventario completos (59 casos nuevos). Consultas de lectura validadas contra
  la base real de alwaysdata sin escribir.
- **Bloqueo actual:** para el end-to-end HTTP con datos reales hay que dejar que
  `asegurar_esquema()` cree `auth_refresh_tokens`, `api_idempotencia` y
  `usuarios.token_version` (DDL aditivo e idempotente). No se hizo porque se
  pidió no tocar la base.
- **Google OAuth:** pendiente para la fase de cuenta.
- **Siguiente:** Fase 6 — admin: `inventario/registros`, informe diario,
  reportes de cancelaciones, dashboard/KPI y ajustes.
- **Nota health:** `/api/v1/health` devuelve HTTP 503 con `ok: true` y
  `data.base_datos = "error"` cuando MySQL no responde; el estado real está en
  el código HTTP y en `data.base_datos`.

