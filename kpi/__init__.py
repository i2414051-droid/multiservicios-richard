"""
Modulo de KPIs del sistema.

Agrupado en capas con una responsabilidad cada una:

    settings      -> configuracion (retencion, lotes, rutas excluidas)
    repository    -> todo el SQL: esquema, escrituras, lecturas, purga
    collectors    -> instrumentacion: cronometra peticiones y guarda errores
    service       -> formulas de los KPIs y evaluacion de postura
    routes        -> blueprint con la API y el fragmento del panel tecnico
    routes_biz    -> blueprint con la API y el panel de VENTAS y ALMACEN
    schema        -> CREATE TABLE propias del modulo de negocio

La app no importa nada de este modulo salvo `init_kpi` y `init_kpi_biz`,
que son los unicos puntos de contacto: asi el resto del sistema ignora
que los KPIs existen.
"""
from .routes import init_kpi, kpi_bp
from .routes_biz import init_kpi_biz, kpi_biz_bp

__all__ = ['init_kpi', 'init_kpi_biz', 'kpi_bp', 'kpi_biz_bp']