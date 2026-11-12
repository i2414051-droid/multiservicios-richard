-- Esquema propio de la API v1.
-- La aplicación lo crea de forma perezosa al primer login/refresh
-- (api/security.py: asegurar_esquema). Este archivo sirve para migrar a mano
-- en producción o para revisar el DDL.

CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    usuario_id  INT NOT NULL,
    jti         VARCHAR(64) NOT NULL,
    token_hash  CHAR(64) NOT NULL,
    device_id   VARCHAR(120) NULL,
    user_agent  VARCHAR(255) NULL,
    ip          VARCHAR(45) NULL,
    creado_en   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expira_en   DATETIME NOT NULL,
    revocado    TINYINT DEFAULT 0,
    revocado_en DATETIME NULL,
    UNIQUE KEY uq_refresh_jti (jti),
    INDEX ix_refresh_usuario (usuario_id),
    INDEX ix_refresh_expira (expira_en)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_idempotencia (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    usuario_id   INT NOT NULL,
    clave        VARCHAR(120) NOT NULL,
    endpoint     VARCHAR(120) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    respuesta    MEDIUMTEXT NULL,
    creado_en    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_idem_usuario_clave (usuario_id, clave)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Necesaria para invalidar access tokens al cambiar la contraseña.
-- Si la columna ya existe, MySQL devuelve error 1060: se puede ignorar.
ALTER TABLE usuarios ADD COLUMN token_version INT DEFAULT 0;

-- Acelera las consultas del carrito de la API. Si el índice ya existe,
-- MySQL devuelve error 1061: se puede ignorar.
ALTER TABLE carrito ADD INDEX ix_carrito_usuario_producto (usuario_id, producto_id);
