-- ============================================================================
-- MIGRACIÓN EXTRA: tabla productos_para_pedir
-- ============================================================================
-- ORIGEN : proyecto_multiservicios_richard
-- DESTINO: proyecto_gestion_almacen
--
-- Copia la tabla productos_para_pedir (con sus datos) al destino.
-- Es idempotente y NO borra nada.
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;

-- Crear la tabla en destino si no existe
CREATE TABLE IF NOT EXISTS `proyecto_gestion_almacen`.`productos_para_pedir` (
  `id`              INT AUTO_INCREMENT PRIMARY KEY,
  `producto_id`     INT NOT NULL,
  `cantidad_pedido` INT DEFAULT 1,
  `proveedor_id`    INT,
  `estado`          VARCHAR(20) DEFAULT 'pendiente',
  `fecha`           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Copiar todos los datos (columnas explícitas)
INSERT INTO `proyecto_gestion_almacen`.`productos_para_pedir`
  (id, producto_id, cantidad_pedido, proveedor_id, estado, fecha)
SELECT id, producto_id, cantidad_pedido, proveedor_id, estado, fecha
FROM `proyecto_multiservicios_richard`.`productos_para_pedir`;

SET FOREIGN_KEY_CHECKS = 1;

-- VERIFICACIÓN
SELECT COUNT(*) AS registros_en_destino
FROM `proyecto_gestion_almacen`.`productos_para_pedir`;
