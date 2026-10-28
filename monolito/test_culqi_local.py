import os,sys
from pathlib import Path
from dotenv import load_dotenv
BASE=Path(r'C:\Users\david\Downloads\P3')
load_dotenv(BASE/'.env')
os.chdir(BASE/'monolito')
sys.path.insert(0,str(BASE/'monolito'))
import app as m
m.app.config['TESTING']=True
c=m.app.test_client()
with c.session_transaction() as s:
    s['_csrf_token']='x'; s['user_id']=1; s['correo']='admin@prueba.local'; s['rol']='admin'; s['nombre']='Admin'; s['apellido']='Prueba'
r=c.get('/checkout')
h=r.get_data(as_text=True)
print(r.status_code)
print('metodo-pago:', h.count('metodo-pago'))
print('Pagar con Culqi:', 'Pagar con Culqi' in h)
print('yape:true:', 'yape:true' in h)
print('tarjeta:true:', 'tarjeta:true' in h)
