# ─────────────────────────────────────────────
# Arranca MS Clientes/Ventas (interfaz completa) — PUERTO 5000
# Es el micro que renderiza TODA la UI (tienda + admin ventas + panel almacén).
# USO: PowerShell  >  .\arrancar\arrancar_ventas.ps1
# ─────────────────────────────────────────────
$ErrorActionPreference = 'Stop'
$root      = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envFile   = Join-Path $root '.env'

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}

# Respaldos si no vienen del .env
if (-not $env:MYSQL_DB)            { $env:MYSQL_DB            = 'proyecto_multiservicios_richard' }
if (-not $env:MYSQL_DB_ALMACEN)    { $env:MYSQL_DB_ALMACEN    = 'proyecto_gestion_almacen' }
if (-not $env:MS_ALMACEN_URL)      { $env:MS_ALMACEN_URL      = 'http://localhost:5001' }
if (-not $env:MS_VENTAS_URL)       { $env:MS_VENTAS_URL       = 'http://localhost:5000' }
if (-not $env:INTERNAL_API_KEY)    { $env:INTERNAL_API_KEY    = 'clave-interna-ms-almacen-2026' }
if (-not $env:PORT)                { $env:PORT                = '5000' }

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

Write-Host "[MS Ventas] Puerto: $env:PORT   MS Almacen URL: $env:MS_ALMACEN_URL"
Push-Location (Join-Path $root 'ms_clientes_ventas')
try {
    & $python app.py
} finally {
    Pop-Location
}
