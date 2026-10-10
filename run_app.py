import os
from dotenv import load_dotenv
load_dotenv(r"C:\Users\david\AppData\Local\Temp\opencode\kpi-push\.env")
os.environ.setdefault('MYSQL_DB', 'proyecto_multiservicios_richard')
os.environ.setdefault('MYSQL_DB_ALMACEN', 'proyecto_gestion_almacen')
os.environ.setdefault('PORT', '5000')

# Monkey-patch BEFORE importing anything
import flask_mysqldb
orig_connect = flask_mysqldb.MySQL.connect.fget
def debug_connect(self):
    from flask import current_app
    print("DEBUG connect: MYSQL_HOST =", current_app.config.get('MYSQL_HOST'))
    print("DEBUG connect: MYSQL_USER =", current_app.config.get('MYSQL_USER'))
    print("DEBUG connect: MYSQL_PASSWORD =", str(current_app.config.get('MYSQL_PASSWORD'))[:5] + "...")
    return orig_connect(self)
flask_mysqldb.MySQL.connect = property(debug_connect)
print("PATCH APPLIED")

exec(open(r"C:\Users\david\AppData\Local\Temp\opencode\kpi-push\app.py", encoding='utf-8').read())