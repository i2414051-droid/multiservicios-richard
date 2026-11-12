"""Servicios de negocio compartidos por la web y la API.

La API v1 no debe duplicar reglas: aqui viven las validaciones y operaciones
que antes estaban dentro de las vistas de app.py. La web puede migrar a estos
servicios de forma incremental sin cambiar su comportamiento.
"""
