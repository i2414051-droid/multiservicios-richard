# ═══════════════════════════════════════════════════════════════════
# Prepara el entorno local: crea .venv, instala las dependencias y
# deja listo el .env. Idempotente: se puede correr las veces que sea.
# USO (desde la raiz del proyecto):
#   powershell -ExecutionPolicy Bypass -File .\arrancar\instalar.ps1
# ═══════════════════════════════════════════════════════════════════
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venv   = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

# ── 1. Entorno virtual ─────────────────────────────────────────────
if (-not (Test-Path $python)) {
    Write-Host "[Instalar] Creando entorno virtual en .venv ..." -ForegroundColor Cyan
    python -m venv $venv
} else {
    Write-Host "[Instalar] .venv ya existe, se reutiliza." -ForegroundColor Green
}

# ── 2. Dependencias ────────────────────────────────────────────────
Write-Host "[Instalar] Actualizando pip/setuptools/wheel ..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip setuptools wheel

Write-Host "[Instalar] Instalando requirements del monolito y microservicios ..." -ForegroundColor Cyan
foreach ($req in @('monolito\requirements.txt',
                   'microservicios\ms_clientes_ventas\requirements.txt',
                   'microservicios\ms_gestion_almacen\requirements.txt')) {
    & $python -m pip install -r (Join-Path $root $req)
}

# ── 3. Archivo .env ────────────────────────────────────────────────
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root '.env.example') $envFile
    Write-Host "[Instalar] Se creo .env a partir de .env.example. Completalo con tus credenciales." -ForegroundColor Yellow
} else {
    Write-Host "[Instalar] .env ya existe." -ForegroundColor Green
}

# ── 4. Verificacion final ──────────────────────────────────────────
Write-Host "[Instalar] Verificando integridad de dependencias ..." -ForegroundColor Cyan
& $python -m pip check

Write-Host ""
Write-Host "Entorno listo. Para arrancar en local:" -ForegroundColor Green
Write-Host "  .\arrancar\arrancar_monolito.ps1   -> monolito (lo de Render) en http://localhost:5000" -ForegroundColor Cyan
Write-Host "  .\arrancar\arrancar_ventas.ps1     -> micro ventas/tienda      en http://localhost:5000" -ForegroundColor Cyan
Write-Host "  .\arrancar\arrancar_almacen.ps1    -> micro almacen (API)      en http://localhost:5001" -ForegroundColor Cyan
