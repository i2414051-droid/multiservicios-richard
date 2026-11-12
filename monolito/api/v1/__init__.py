"""Paquete de la API v1."""
from flask import Blueprint

bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')

# Los modulos registran sus rutas sobre `bp`. Se importan al final para que
# `bp` ya exista cuando hacen `from . import bp`.
from . import (  # noqa: E402,F401
    admin_clientes,
    admin_entregas,
    admin_ingresos,
    admin_inventario,
    admin_productos,
    admin_proveedores,
    admin_salidas,
    admin_ventas,
    auth,
    carrito,
    catalogo,
    direcciones,
    pedidos,
    reclamos,
    sistema,
)
