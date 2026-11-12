import os
import sys

# Permite ejecutar `pytest` desde monolito/ o desde la raiz del repo.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
