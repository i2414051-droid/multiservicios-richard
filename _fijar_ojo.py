# -*- coding: utf-8 -*-
import io, re, os

root = r"C:\Users\david\AppData\Local\Temp\multiservicios-richard"
css_path = os.path.join(root, "static", "css", "estilos.css")

def read_ss(p):
    return io.open(p, "r", encoding="utf-8-sig").read()

def write_ss(p, text):
    io.open(p, "w", encoding="utf-8", newline="").write(text)

css = read_ss(css_path)

NARANJA = "--color-ojo: #f39c12;"

cambios = []

# ── 1) :root (oscuro): reemplazar el valor antigüo/inexistente que sea (#8a6d1a o cualquier otro)
m = re.search(r"(:root\s*\{)([^}]*)\}", css, re.S)
if m:
    viejo = re.search(r"^--color-ojo:\s*[^;]+;", m.group(2), re.M)
    if viejo:
        css = css[:m.start(2)] + css[m.start(2):m.start(2)+viejo.start()] + NARANJA + css[m.start(2)+viejo.end():]
        cambios.append(":root: --color-ojo -> " + NARANJA.strip())
    else:
        css = css[:m.start(2)+0] + NARANJA + "\n" + css[m.start(2):] if False else css
        # insertar al inicio del bloque raiz
        inicio = m.start(2)
        css = css[:inicio] + NARANJA + "\n  " + css[inicio:]
        cambios.append(":root: --color-ojo AGREGADO -> " + NARANJA.strip())

# ── 2) [data-theme="light"]: agregar si no existe dentro del bloque
m = re.search(r'(\[data-theme="light"\]\s*\{)([^}]*)\}', css, re.S)
if m:
    bloque = m.group(2)
    if re.search(r"^--color-ojo:", bloque, re.M):
        cambios.append('[data-theme="light"]: ya tenía --color-ojo (sin cambio)')
    else:
        inicio = m.start(2)
        css = css[:inicio] + NARANJA + "\n  " + css[inicio:]
        cambios.append('[data-theme="light"]: --color-ojo AGREGADO -> ' + NARANJA.strip())
else:
    cambios.append('[data-theme="light"]: NO encontrado (revisar selector)')

write_ss(css_path, css)

print("=== cambios aplicados ===")
for c in cambios:
    print("  * " + c)

print()
print("=== verificacion final (leer contando) ===")
cnt_dark = len(re.findall(r":root\s*\{", read_ss(css_path)))
cnt_light = len(re.findall(r'\[data-theme="light"\]\s*\{', read_ss(css_path)))
total_ojos = len(re.findall(r"--color-ojo:\s*#f39c12", read_ss(css_path)))
print(f"  bloques :root = {cnt_dark} | bloques light = {cnt_light} | --color-ojo naranja totales = {total_ojos}")
print(f"  ¿se ven en ambos?  :root={total_ojos>=1}  light={total_ojos>=2}")

# comprobar también que los ojos de templates usen la variable (no text-secondary)
for tpl in ("Inicio.html", "registro.html"):
    h = read_ss(os.path.join(root, "templates", tpl))
    n_var = h.count("var(--color-ojo)")
    n_ts  = len(re.findall(r'class="[^"]*\btext-secondary\b', h))
    print(f"  {tpl}: var(--color-ojo) usos={n_var}  text-secondary resto={n_ts}")
