# ═══════════════════════════════════════════════════════════════════
# Arranca el MONOLITO desde monolito/ (lo que Render despliega) - lee el .env
# USO: PowerShell  >  .\arrancar\arrancar_monolito.ps1
# ═══════════════════════════════════════════════════════════════════
# Este script carga el .env de la raiz en el proceso y arranca la app desde
# monolito/. En Render las variables las inyecta el panel; en local, esto
# garantiza que el monolito use las credenciales reales.
# ───────────────────────────────────────────────────────────────────
$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envFile = Join-Path $root '.env'

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}
else {
    Write-Host "[Monolito] No existe .env en la raiz. Copia .env.example y completalo." -ForegroundColor Yellow
}

# Respaldos: el monolito corre en 5000 por defecto, no en 5001 como el de almacen
if (-not $env:MYSQL_DB)         { $env:MYSQL_DB         = 'proyecto_multiservicios_richard' }
if (-not $env:MYSQL_DB_ALMACEN) { $env:MYSQL_DB_ALMACEN = 'proyecto_gestion_almacen' }
if (-not $env:PORT)             { $env:PORT             = '5000' }

# Aviso si la password quedo como placeholder: la conexion va a fallar y el
# error real se ve solo con DEBUG_ERRORS=1
if ($env:MYSQL_PASSWORD -like 'PEGAR_AQUI*') {
    Write-Host "[Monolito] MYSQL_PASSWORD sigue como placeholder en .env" -ForegroundColor Red
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
    Write-Host "[Monolito] No hay .venv; se usara el python del PATH" -ForegroundColor Yellow
}

Write-Host "[Monolito] Puerto: $env:PORT"
Write-Host "[Monolito] BD ventas:  $env:MYSQL_DB"
Write-Host "[Monolito] BD almacen: $env:MYSQL_DB_ALMACEN"
Write-Host "[Monolito] DEBUG_ERRORS: $env:DEBUG_ERRORS"
Write-Host "[Monolito] http://localhost:$env:PORT"

Push-Location (Join-Path $root 'monolito')
try {
    & $python app.py
}
finally {
    Pop-Location
}
