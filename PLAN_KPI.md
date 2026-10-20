# Plan de trabajo KPI — Multiservicios Richard

Estado al cierre de la sesion del **19/10/2026**. Todo lo de aqui esta pensado
sobre el codigo real del proyecto, no sobre supuestos.

---

## YA HECHO

### Correcciones de conexion del modulo KPI (4 sectores)

| Problema | Correccion |
|---|---|
| El panel comparaba contra el reloj de Python y las ventas recientes caian fuera del periodo (mostraba 0 pedidos con 3 ventas en la BD) | `service.resolve_period` usa `SELECT NOW()`: manda el reloj de la base de datos |
| `carrito` y `bloqueos_ip` no tenian columna de fecha -> 2 consultas con error 1054 que tumbaban los sectores 3 y 4 | Columnas `created_at` anadidas (con `DEFAULT CURRENT_TIMESTAMP`, los INSERT existentes las rellenan solos) |
| El auto-refresco del panel nunca arrancaba (solo con doble clic) | Arranca al cargar, cada 30 s |
| La API de incidencias no tenia ninguna pantalla -> `kpi_incidencias` vacia para siempre | Seccion 5 "Registro de Incidencias" en el panel (alta y baja) |
| `productos` no tiene categoria "Repuestos" -> 2 indicadores sin explicacion | El panel avisa de que categoria falta y lista las que si existen |
| Los KPIs de repuestos fallaban en silencio si la categoria no existia | `KPI_CATEGORIA_REPUESTOS` / `KPI_CATEGORIA_ACCESORIOS` aceptan varias, separadas por coma |

### Fase 1 — Recoleccion del recorrido del cliente

**Tablas nuevas (ya creadas en alwaysdata):**

- `kpi_sesiones_compra` — una fila por intencion de compra: paso alcanzado, motivo de fallo, venta_id, `recordatorio_enviado_at`, `canal_aviso`.
- `kpi_busquedas` — una fila por busqueda escrita: texto, resultados, producto visto, convertido.

**Modulo nuevo:** `kpi/registro.py` (todas las escrituras van en try/except: una metrica no puede tumbar la tienda).

**5 puntos instrumentados en `app.py`:**

| Punto | Registra |
|---|---|
| `GET /agregar/<id>` | paso `carrito` |
| `GET /checkout` | paso `checkout` |
| `POST /procesar_compra` (fallo) | paso `formulario` + `motivo_fallo` |
| `POST /procesar_compra` (exito) | paso `venta`, `estado='convertida'` |
| `GET /?buscar=...` y `GET /producto/<id>` | busqueda + producto visto |

---

## BLOQUE A — Embudo de compra (KPI #2)

Objetivo: ver **en que paso se queda la gente** y **por que**.

### A1. Consultas de lectura — `kpi/repository.py`

- `fetch_embudo_compra(desde, hasta)` -> por cada paso (`carrito`, `checkout`, `formulario`, `venta`):
  - cuantos llegaron
  - cuantos pasaron al siguiente
  - **cuantos se quedaron en ese paso** (el que no paso al siguiente)
  - % de paso a paso
- `fetch_motivos_fallo(desde, hasta)` -> desglose por `motivo_fallo`.

Fuente: `kpi_sesiones_compra` (paso_alcanzado, estado, motivo_fallo, iniciada_at).

> Ojo: el paso `carrito` no sale de `kpi_sesiones_compra` sino de `carrito.created_at`.
> Hay que decidir si se cuenta desde la tabla de sesiones o desde `carrito`, y que
> los dos numeros cuadren.

### A2. Formulas — `kpi/service.py`

- `_seccion_embudo(actual, anterior)` -> estructura lista para pintar.
- Variacion frente al periodo anterior (igual que el resto de secciones).
- Semaforo en `settings.UMBRALES`: la clave `abandono_checkout` (sube = False).

### A3. UI del embudo de cuadritos

- Nueva macro en `templates/kpi_biz/macros/kpi_card.html`: `embudo(pasos)`.
- Render en `templates/kpi_biz/_bloque.html`.
- CSS en `static/css/kpi_biz.css` (`.kpi-biz-embudo`, cajas, barras, color de fuga).

Diseno acordado:

```
┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
│ 1 Carrito  │─▶│ 2 Checkout │─▶│ 3 Formulario│─▶│ 4 Válido  │─▶│ 5 Venta    │
│  48  100%  │  │  31   65%  │  │  22   71%  │  │  19   86%  │  │  19 100%  │
│            │  │ ▲17 quedan │  │ ▲3 quedan  │  │ ▲3 quedan  │  │            │
└────────────┘  └────────────┘  └────────────┘  └────────────┘  └────────────┘
                 ▲ MAYOR FUGA (35%)
```

Debajo: **tabla de motivos de fallo** ("3 se quedaron por DNI inválido").

### A4. Decidir donde va

Recomendacion: **seccion 6 propia** ("Embudo de compra"), entre la 4 (E-commerce) y
la 5 (Incidencias). El embudo es el dato estrella y se pierde si queda enterrado en
la 4.

---

## BLOQUE B — Comportamiento del buscador (KPI #3)

Objetivo: saber **que busca la gente** (y que no tenemos) y **si el buscador vende**.

### B1. Consultas — `kpi/repository.py`

- `fetch_busquedas_top(desde, hasta, limite=15)` -> texto, veces buscada, veces con
  resultado, veces convertidas, % de conversion.
- `fetch_busquedas_sin_resultado(desde, hasta, minimo=2)` -> texto y veces, **solo
  las que dio 0 resultados** y se buscaron al menos 2 veces.
- `fetch_conversion_buscador(desde, hasta)` -> % global de conversion.

### B2. Service + UI

- `_seccion_buscador(actual, anterior)` en `service.py`.
- Dos tablas en el panel: "Busquedas mas frecuentes" y "Busquedas sin resultado
  (candidatos a traer)". La segunda es la que sirve para decidir que variedad
  meter.

### B3. Decisiones pendientes

- **Como se define "conversion"**: se propone `búsquedas con resultado > 0` ->
  cuántas acabaron en compra. Conviene medir tambien `producto_visto_id`.
- **Ventana de atribucion**: hoy se marcan como convertidas las busquedas de los
  ultimos 30 dias. Hay que decidir si es la ventana correcta.

### B4. (Fase aparte, Opcional) Buscador por vehiculo — el cambio de negocio

Hoy la busqueda es `nombre LIKE '%q%'`: buscar "hilux" no encuentra nada porque no
hay ningun campo de vehiculo. Es la palanca real del bloque B.

Opciones, en este orden:
1. Medir una semana con lo de B1 (primero medir, luego tocar).
2. Ampliar los nombres de los productos ("Foco H4 Narva" -> "... (Toyota Hilux 2015-2020)").
3. Campo nuevo `vehiculos_compatibles` en `productos` + busqueda por ese campo.

---

## BLOQUE C — Recuperacion de carritos (KPI #4)

Objetivo: avisar al cliente que dejo su compra pendiente y medir si funciona.

### C1. Deteccion de candidatos

Consulta de carritos abandonados:
- `kpi_sesiones_compra` con `estado='abierta'` y `iniciada_at` de hace mas de X horas
- **y** que el carrito sigue vivo en la tabla `carrito`
- **y** que no ha comprado desde entonces
- **y** que no se le ha avisado ya (`recordatorio_enviado_at IS NULL`)

> Ojo: el carrito se borra al comprar, asi que "el carrito sigue vivo" ya es una
> senal de abandono. Pero hay que excluir a los que compraron en otra pestana.

### C2. Envio del aviso

- Usar `Mail(app)`, que ya esta configurado en `app.py`.
- **Requiere credenciales SMTP reales**: el `.env` tiene `MAIL_USERNAME=tu_correo@gmail.com`
  (valor de ejemplo). Hay que poner una cuenta real y su App Password.
- El email lleva: nombre, link a `/carrito`, lista de productos y un texto de baja.
- **Re-validar el stock antes de enviar**: si el producto se agoto, el mensaje es una
  promesa rota.
- Registrar `recordatorio_enviado_at` y `canal_aviso='email'`.

### C3. Medicion

- `kpi_sesiones_compra` con `recordatorio_enviado_at IS NOT NULL` y
  `estado='convertida'` -> recuperadas.
- KPI: recuperadas / contactadas.
- La conversion del buscador y la recuperacion se pueden cruzar: **de los que se
  avisaron, cuantos venian de una busqueda sin resultado** (dato de negocio muy
  bueno: "les dijimos que no teniamos X y lo compraron").

### C4. UI

- En el panel: lista de carritos abandonados candidatos + boton **"Enviar
  recordatorio"** (manual primero) + contador de avisados y recuperados.

---

## DECISIONES QUE ESTAN PENDIENTES (bloquean el bloque C)

1. **Canal del aviso**: email (ya hay SMTP, gratis, llega al spam) o WhatsApp
   (canal natural en Peru, pero API de pago y numero verificado).
   -> Recomendacion: **email primero**.
2. **Automatico o boton manual**: -> Recomendacion: **boton manual primero**, y
   automatizar solo lo que ya se sabe que convierte.
3. **Horas de espera antes de avisar**: 6 h (recupera mas rápido, mas riesgo de
   spamear a quien esta pagando) o 24 h (mas seguro). -> Recomendacion: **6 h**.
4. **¿Se amplia el buscador por vehiculo?** (bloque B4).

---

## PENDIENTES TECNICOS

- [ ] **Redesplegar en Render**. Hay cambios pendientes de: `app.py`, `kpi/`,
      `templates/`, `static/`. Sin esto, nada de lo nuevo esta en produccion.
- [ ] Instrumentar `ms_clientes_ventas/app.py` (opcional, solo para correr local).
      Son los mismos 5 puntos.
- [ ] `kpi/routes.py` y `kpi/retention.py` son **codigo muerto**: `kpi/__init__.py`
      solo importa `routes_biz`. O se borran o se dejan como archivo. Limpieza
      menor.
- [ ] Asignar la categoria **Repuestos** a los productos del almacen (o configurar
      `KPI_CATEGORIA_REPUESTOS`), si se quieren ver vivos esos 2 indicadores.
- [ ] Credenciales SMTP reales en `.env` (y en las variables de Render) para el
      bloque C.

---

## COMANDOS UTILES PARA MIRAR LOS DATOS

```sql
-- Recorrido de compra paso a paso
SELECT paso_alcanzado, estado, COUNT(*) AS n
FROM kpi_sesiones_compra
WHERE iniciada_at >= NOW() - INTERVAL 30 DAY
GROUP BY paso_alcanzado, estado;

-- Por que se queda la gente
SELECT motivo_fallo, COUNT(*) AS n
FROM kpi_sesiones_compra
WHERE motivo_fallo IS NOT NULL
  AND iniciada_at >= NOW() - INTERVAL 30 DAY
GROUP BY motivo_fallo ORDER BY n DESC;

-- Que busca la gente y no tenemos
SELECT texto, COUNT(*) AS veces
FROM kpi_busquedas
WHERE resultados = 0
  AND fecha >= NOW() - INTERVAL 30 DAY
GROUP BY texto ORDER BY veces DESC;

-- Cuanto convierte el buscador
SELECT COUNT(*) AS busquedas,
       SUM(resultados > 0) AS con_resultado,
       SUM(convertido) AS convertidas
FROM kpi_busquedas
WHERE fecha >= NOW() - INTERVAL 30 DAY;

-- Carritos abandonados candidatos al aviso
SELECT cs.id, cs.usuario_id, cs.paso_alcanzado, cs.iniciada_at
FROM kpi_sesiones_compra cs
WHERE cs.estado = 'abierta'
  AND cs.iniciada_at < NOW() - INTERVAL 6 HOUR
  AND cs.recordatorio_enviado_at IS NULL;
```
