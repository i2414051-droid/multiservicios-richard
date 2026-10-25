"""
Modulo de KPIs del sistema.

Agrupado en capas con una responsabilidad cada una:

    settings      -> configuracion del modulo
    repository    -> todo el SQL: esquema, escrituras y lecturas
    collectors    -> traza de errores no controlados (persist_error)
    service       -> formulas de los KPIs y evaluacion de postura
    routes_biz    -> blueprint con la API y el panel de VENTAS y ALMACEN
    schema        -> CREATE TABLE propias del modulo de negocio

La app no importa nada de este modulo salvo `init_kpi_biz`, que es el unico
punto de contacto: asi el resto del sistema ignora que los KPIs existen.
"""
from .routes_biz import init_kpi_biz, kpi_biz_bp

__all__ = ['init_kpi_biz', 'kpi_biz_bp']
