# Sincroniza las ventas existentes con seguimiento_entregas.
#
# Uso (desde la raiz del repo):
#   .venv\Scripts\python.exe .\arrancar\backfill_entregas.py
#   .venv\Scripts\python.exe .\arrancar\backfill_entregas.py --aplicar
#
# Sin --aplicar solo muestra que ventas se crearian (dry-run). Con --aplicar
# inserta las filas que falten en una sola transaccion. No crea tablas ni
# modifica ventas: es seguro ejecutarlo varias veces.
from pathlib import Path
import argparse
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass

try:
    import MySQLdb
    from MySQLdb import cursors as db_cursors
except ImportError:  # pragma: no cover - depende del entorno
    import pymysql
    pymysql.install_as_MySQLdb()
    import MySQLdb
    from pymysql import cursors as db_cursors

MAPEO_ESTADO = {
    'entregado': 'entregado',
    'cancelado': 'cancelado',
    'en espera': 'pendiente',
}


def conectar():
    return MySQLdb.connect(
        host=os.environ.get('MYSQL_HOST', 'localhost'),
        port=int(os.environ.get('MYSQL_PORT', 3306)),
        user=os.environ.get('MYSQL_USER', 'root'),
        passwd=os.environ.get('MYSQL_PASSWORD', ''),
        db=os.environ.get('MYSQL_DB', 'proyecto_multiservicios_richard'),
        charset=os.environ.get('MYSQL_CHARSET', 'utf8mb4'),
        cursorclass=db_cursors.DictCursor,
        connect_timeout=int(os.environ.get('MYSQL_CONNECT_TIMEOUT', 5)),
    )


def ventas_sin_seguimiento(cur):
    cur.execute("""
        SELECT v.id, v.estado, v.total, v.fecha, v.direccion_envio
        FROM ventas v
        LEFT JOIN seguimiento_entregas s ON s.venta_id = v.id
        WHERE s.id IS NULL
        ORDER BY v.id
    """)
    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(
        description='Crea el seguimiento que falte para las ventas existentes.')
    parser.add_argument(
        '--aplicar', action='store_true',
        help='inserta los seguimientos; sin esta opcion solo simula')
    args = parser.parse_args()

    conn = conectar()
    try:
        cur = conn.cursor()
        filas = ventas_sin_seguimiento(cur)
        if not filas:
            print('Todo en orden: no hay ventas sin seguimiento.')
            return 0

        print(f'Ventas sin seguimiento: {len(filas)}')
        for fila in filas:
            estado = MAPEO_ESTADO.get(
                (fila['estado'] or '').strip().lower(), 'pendiente')
            print(f"  venta #{fila['id']}: estado comercial={fila['estado']!r}"
                  f" -> seguimiento={estado!r}")

        if not args.aplicar:
            print('\nDry-run: usa --aplicar para insertar estas filas.')
            return 0

        for fila in filas:
            estado = MAPEO_ESTADO.get(
                (fila['estado'] or '').strip().lower(), 'pendiente')
            fecha_entrega = fila['fecha'] if estado == 'entregado' else None
            cur.execute("""
                INSERT INTO seguimiento_entregas
                    (venta_id, estado, direccion_envio, fecha_entrega, notas)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                fila['id'], estado, fila['direccion_envio'], fecha_entrega,
                'Seguimiento creado al sincronizar ventas existentes.',
            ))
        conn.commit()
        print(f'\nInsertados: {len(filas)} seguimiento(s).')
        return 0
    except Exception as exc:
        conn.rollback()
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
