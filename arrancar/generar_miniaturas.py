# Regenera las miniaturas de 400 px de los listados.
#
# Uso (desde la raiz del repo):
#   .venv\Scripts\python.exe .\arrancar\generar_miniaturas.py
#
# Toma las imagenes completas de monolito/static/imagenes/ y escribe
# static/imagenes/thumbs/ en el monolito y en los dos microservicios.
# El detalle de producto sigue usando la imagen completa.
from pathlib import Path
import io
import shutil

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "monolito" / "static" / "imagenes"
DST_DIRS = [
    ROOT / "monolito" / "static" / "imagenes" / "thumbs",
    ROOT / "microservicios" / "ms_clientes_ventas" / "static" / "imagenes" / "thumbs",
    ROOT / "microservicios" / "ms_gestion_almacen" / "static" / "imagenes" / "thumbs",
]
MAX_SIDE = 400


def main():
    if not SRC.is_dir():
        raise SystemExit(f"No existe {SRC}")

    for d in DST_DIRS:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in SRC.iterdir() if p.is_file())
    total = 0
    for p in files:
        im = Image.open(p)
        im.load()
        fmt = (im.format or "").upper()
        im = ImageOps.exif_transpose(im)
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        if fmt == "JPEG":
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(buf, format="JPEG", quality=85, optimize=True, progressive=True)
        elif fmt == "WEBP":
            im.save(buf, format="WEBP", quality=85, method=6)
        elif fmt == "AVIF":
            im.save(buf, format="AVIF", quality=80)
        elif fmt == "PNG":
            im.save(buf, format="PNG", optimize=True)
        else:
            raise SystemExit(f"Formato no soportado: {fmt} en {p.name}")

        data = buf.getvalue()
        total += len(data)
        for d in DST_DIRS:
            (d / p.name).write_bytes(data)

    print(f"Miniaturas: {len(files)} | {total/1024:.1f} KB por app")
    for d in DST_DIRS:
        print(f"  {d.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
