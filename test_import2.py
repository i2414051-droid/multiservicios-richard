import sys
sys.path.insert(0, r"C:\Users\david\AppData\Local\Temp\multiservicios-richard")
try:
    from kpi import init_kpi, init_kpi_biz
    print("OK")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()