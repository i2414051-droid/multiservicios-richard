# -*- coding: utf-8 -*-
import io,re,sys
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8')
root=r"C:\Users\david\AppData\Local\Temp\multiservicios-richard"
css=io.open(root+r"\static\css\estilos.css","r",encoding="utf-8-sig").read()

print("=== 1) como queda --color-ojo en :root (oscuro) ====")
m=re.search(r":root\s*\{([^}]*)\}",css,re.S)
block=m.group(1)
ojo=re.search(r"--color-ojo\s*:\s*([^;]+)",block)
print("   :root --color-ojo =", (ojo.group(1).strip() if ojo else "NO DEFINIDO"))

print("=== 2) bloque [data-theme='light']: tiene ojo? ====")
m2=re.search(r'\[data-theme="light"\]\s*\{([^}]*)\}',css,re.S)
if m2:
    b2=m2.group(1)
    o2=re.search(r"--color-ojo\s*:\s*([^;]+)",b2)
    print("   light --color-ojo =", (o2.group(1).strip() if o2 else "NO DEFINIDO (falta agregar)"))
else:
    print("   NO encontre bloque light")

print("=== 3) ojos en Inicio/registro: usan var(--color-ojo)? (count) ====")
for f in ("Inicio.html","registro.html"):
    t=io.open(root+r"\templates\"+f,"r",encoding="utf-8-sig").read()
    print("   %s: var(--color-ojo)=%d  placeholder=%s" % (f,t.count("var(--color-ojo)"),("placeholder" in t)))
