# ─────────────────────────────────────────────
# Arranca MS Gestion Almacen (API REST interna) — PUERTO 5001
# Lee el .env raíz y lanza el micro de almacén.
# Uso:  ./arrancar/arrancar_almacen.ps1
# ─────────────────────────────────────────────
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root      = Split-Path -Parent $scriptDir               # raíz del proyecto
$envFile   = Join-Path $root '.env'

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}

# Valores del MS Almacén (respaldo si el .env no los trae)
if (-not $env:MYDB)  { $env:MYDB       = $env:MYSQL_DB_ALMACEN }
if (-not $env:PORT)  { $env:PORT       = '5001' }

Write-Host "[MS Almacen] Puerto: $env:PORT  BD: $env:MYSQL_DB_ALMACEN"
Write-Host "[MS Almacen] MS Ventas URL: $env:MS_VENTAS_URL"

Push-Location (Join-Path $root 'ms_gestion_almacen')
try {
    python app.py
} finally {
    Pop-Location
}
