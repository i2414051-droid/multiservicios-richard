-- ============================================================================-
-- DIAGNÓSTICO: ¿qué tablas referencian a `productos` con una FK?
-- Ejecuta esto en phpMyAdmin (base proyecto_multiservicios_richard).
-- Si devuelve filas, esas son las FKs que impiden eliminar `productos`.
-- ============================================================================
SELECT TABLE_NAME        AS tabla_origen,
       COLUMN_NAME       AS columna,
       CONSTRAINT_NAME   AS constraint_name,
       REFERENCED_TABLE_NAME AS tabla_referenciada
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA   = 'proyecto_multiservicios_richard'
  AND REFERENCED_TABLE_SCHEMA = 'proyecto_multiservicios_richard'
  AND REFERENCED_TABLE_NAME   = 'productos';

-- ============================================================================
-- SOLUCIÓN: eliminar TODAS las FKs que apuntan a `productos` y luego dropear
-- la tabla. Ejecuta el siguiente bloque DESPUÉS de revisar el diagnóstico.
-- ============================================================================
SET @db = 'proyecto_multiservicios_richard';
SET @t  = 'productos';
SET FOREIGN_KEY_CHECKS = 0;

-- 1) Primero eliminar los constraint que referencian a productos
SET SESSION group_concat_max_len = 1000000;
SELECT GROUP_CONCAT(
    CONCAT('ALTER TABLE `', TABLE_NAME, '` DROP FOREIGN KEY `', CONSTRAINT_NAME, '`;')
    SEPARATOR '\n')
INTO @ddl
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = @db
  AND REFERENCED_TABLE_SCHEMA = @db
  AND REFERENCED_TABLE_NAME = @t
  AND CONSTRAINT_NAME <> 'PRIMARY';

SELECT IFNULL(@ddl, '-- No hay FKs que eliminar') AS 'DDL_generado';

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2) Ahora sí, eliminar la tabla
DROP TABLE IF EXISTS `proyecto_multiservicios_richard`.`productos`;

SET FOREIGN_KEY_CHECKS = 1;

-- VERIFICACIÓN (debe devolver 0 filas):
SELECT TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_SCHEMA='proyecto_multiservicios_richard'
  AND TABLE_NAME='productos';
