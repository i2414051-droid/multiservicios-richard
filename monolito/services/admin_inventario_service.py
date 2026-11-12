"""Verificacion de inventario (admin) — Fase 5.

Reutiliza la misma clasificacion por tramos que `/verificar-inventario` (y que
`/admin`, `/dashboard` y el auto-pedido), con una sola fuente de umbrales
(`STOCK_CRITICO` y `STOCK_NORMAL` en app.py):

  * sin_stock : stock = 0
  * critico   : 1..STOCK_CRITICO   (y se agrega solo a Pedidos)
  * bajo      : STOCK_CRITICO+1 .. STOCK_NORMAL-1
  * normal    : >= STOCK_NORMAL
  * inactivos : estado = inactivo
"""
from .tienda_service import almacen_db


def _entero(valor, defecto=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _umbrales():
    from app import STOCK_CRITICO, STOCK_NORMAL
    return STOCK_CRITICO, STOCK_NORMAL


def normalizar_filtros(args):
    filtro = str(args.get('filtro', 'todos') or 'todos').strip().lower()
    if filtro not in ('todos', 'sin_stock', 'critico', 'bajo', 'normal',
                      'inactivos'):
        filtro = 'todos'
    buscar = args.get('buscar', '')
    return {'filtro': filtro,
            'buscar': (str(buscar).strip() if buscar is not None else '')[:100]}


def listar(cur, filtros, page=1, per_page=20):
    critico, normal = _umbrales()
    where = "WHERE p.nombre LIKE %s"
    params = [f"%{filtros.get('buscar') or ''}%"]
    filtro = filtros.get('filtro')
    if filtro == 'sin_stock':
        where += " AND p.stock=0 AND p.estado='activo'"
    elif filtro == 'critico':
        where += f" AND p.stock BETWEEN 1 AND {critico} AND p.estado='activo'"
    elif filtro == 'bajo':
        where += (f" AND p.stock BETWEEN {critico + 1} AND {normal - 1} "
                  "AND p.estado='activo'")
    elif filtro == 'normal':
        where += f" AND p.stock >= {normal} AND p.estado='activo'"
    elif filtro == 'inactivos':
        where += " AND p.estado='inactivo'"

    alm = almacen_db()
    cur.execute(f"SELECT COUNT(*) AS total FROM {alm}.productos p {where}",
                tuple(params))
    total = _entero((cur.fetchone() or {}).get('total'))

    offset = max(0, (page - 1) * per_page)
    cur.execute(
        f"""SELECT p.id, p.nombre, p.stock, p.precio, p.categoria, p.imagen,
                   p.estado
            FROM {alm}.productos p {where}
            ORDER BY p.stock ASC LIMIT %s OFFSET %s""",
        tuple(params) + (per_page, offset))
    items = cur.fetchall()

    cur.execute(f"SELECT COUNT(*) AS n FROM {alm}.productos "
                "WHERE estado='activo' AND stock=0")
    sin_stock = _entero((cur.fetchone() or {}).get('n'))
    cur.execute(f"SELECT COUNT(*) AS n FROM {alm}.productos "
                "WHERE estado='activo' AND stock BETWEEN 1 AND %s", (critico,))
    n_critico = _entero((cur.fetchone() or {}).get('n'))
    cur.execute(f"SELECT COUNT(*) AS n FROM {alm}.productos "
                "WHERE estado='activo' AND stock BETWEEN %s AND %s",
                (critico + 1, normal - 1))
    n_bajo = _entero((cur.fetchone() or {}).get('n'))
    cur.execute(f"SELECT COUNT(*) AS n FROM {alm}.productos "
                "WHERE estado='activo' AND stock >= %s", (normal,))
    n_normal = _entero((cur.fetchone() or {}).get('n'))
    cur.execute(f"SELECT COUNT(*) AS n FROM {alm}.productos")
    n_todos = _entero((cur.fetchone() or {}).get('n'))

    contadores = {'sin_stock': sin_stock, 'critico': n_critico, 'bajo': n_bajo,
                  'normal': n_normal, 'todos': n_todos,
                  'umbral_critico': critico, 'umbral_normal': normal}
    return items, total, contadores
