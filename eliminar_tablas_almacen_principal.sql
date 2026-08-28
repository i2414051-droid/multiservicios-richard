-- ============================================================================
-- ELIMINACIÓN de tablas de almacén de la BD principal
-- ============================================================================
-- Estas tablas YA fueron migradas a proyecto_gestion_almacen y ahora se
-- eliminan de proyecto_multiservicios_richard para no duplicar datos.
--
-- ⚠️ ADVERTENCIAS:
--   1. ESTO ES IRREVERSIBLE. Asegúrate de haber hecho un BACKUP y de que los
--      datos estén correctos en proyecto_gestion_almacen ANTES de ejecutar.
--   2. Solo ejecuta DESPUÉS de desplegar el código actualizado de
--      ms_clientes_ventas (que ahora lee estas tablas desde
--      proyecto_gestion_almacen).
--
-- EJECUTAR de una vez en phpMyAdmin (base proyecto_multiservicios_richard).
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`detalle_salida`;
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`detalle_ingreso`;
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`salidas`;
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`ingresos`;
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`productos_para_pedir`;
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`proveedores`;
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`productos`;

SET FOREIGN_KEY_CHECKS = 1;

-- VERIFICACIÓN (debería devolver 0 filas):
SELECT TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_SCHEMA='proyecto_multiservicios_richard'
  AND TABLE_NAME IN ('productos','proveedores','productos_para_pedir','ingresos','detalle_ingreso','salidas','detalle_salida');
