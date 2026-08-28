-- ============================================================================
-- MIGRACIÓN (CORREGIDA): tablas de almacén hacia proyecto_gestion_almacen
-- ============================================================================
-- ORIGEN : proyecto_multiservicios_richard
-- DESTINO: proyecto_gestion_almacen
--
-- CORRECCIÓN: la tabla 'productos' de origen NO tiene la columna 'created_at',
-- por eso fallaba el INSERT anterior. Ahora usa las columnas reales.
--
-- NOTA: como el script anterior ya creó las tablas en destino, este script solo
-- agrega la columna que falte (si aplica) y copia los datos. Es seguro.
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------------------------------------------------------
-- PASO 1: AJUSTAR ESTRUCTURA DE DESTINO (solo agrega columnas faltantes, no borra)
-- ----------------------------------------------------------------------------

-- productos (origen no tiene created_at; aseguramos la columna en destino por compatibilidad)
ALTER TABLE `proyecto_gestion_almacen`.`productos` ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

-- ----------------------------------------------------------------------------
-- PASO 2: COPIAR TODOS LOS DATOS desde la BD principal hacia el destino
-- ----------------------------------------------------------------------------

-- productos (sin created_at porque el origen no lo tiene)
INSERT INTO `proyecto_gestion_almacen`.`productos`
  (id, nombre, precio, imagen, stock, descripcion, categoria, estado)
SELECT id, nombre, precio, imagen, stock, descripcion, categoria, estado
FROM `proyecto_multiservicios_richard`.`productos`;

-- proveedores (con created_at)
INSERT INTO `proyecto_gestion_almacen`.`proveedores`
  (id, nombre, celular, correo, dni, ruc, direccion, categoria, notas, created_at)
SELECT id, nombre, celular, correo, dni, ruc, direccion, categoria, notas, created_at
FROM `proyecto_multiservicios_richard`.`proveedores`;

-- ingresos
INSERT INTO `proyecto_gestion_almacen`.`ingresos`
  (id, proveedor_id, notas, fecha)
SELECT id, proveedor_id, notas, fecha
FROM `proyecto_multiservicios_richard`.`ingresos`;

-- detalle_ingreso
INSERT INTO `proyecto_gestion_almacen`.`detalle_ingreso`
  (id, ingreso_id, producto_id, cantidad, precio_compra)
SELECT id, ingreso_id, producto_id, cantidad, precio_compra
FROM `proyecto_multiservicios_richard`.`detalle_ingreso`;

-- salidas
INSERT INTO `proyecto_gestion_almacen`.`salidas`
  (id, venta_id, notas, fecha)
SELECT id, venta_id, notas, fecha
FROM `proyecto_multiservicios_richard`.`salidas`;

-- detalle_salida
INSERT INTO `proyecto_gestion_almacen`.`detalle_salida`
  (id, salida_id, producto_id, cantidad)
SELECT id, salida_id, producto_id, cantidad
FROM `proyecto_multiservicios_richard`.`detalle_salida`;

-- ----------------------------------------------------------------------------
-- PASO 3: RE-ACTIVAR LAS CHECKS
-- ----------------------------------------------------------------------------
SET FOREIGN_KEY_CHECKS = 1;

-- VERIFICACIÓN
SELECT 'productos'       AS tabla, COUNT(*) AS registros_en_destino FROM `proyecto_gestion_almacen`.`productos`
UNION ALL
SELECT 'proveedores'     AS tabla, COUNT(*) FROM `proyecto_gestion_almacen`.`proveedores`
UNION ALL
SELECT 'ingresos'        AS tabla, COUNT(*) FROM `proyecto_gestion_almacen`.`ingresos`
UNION ALL
SELECT 'detalle_ingreso' AS tabla, COUNT(*) FROM `proyecto_gestion_almacen`.`detalle_ingreso`
UNION ALL
SELECT 'salidas'         AS tabla, COUNT(*) FROM `proyecto_gestion_almacen`.`salidas`
UNION ALL
SELECT 'detalle_salida'  AS tabla, COUNT(*) FROM `proyecto_gestion_almacen`.`detalle_salida`;
